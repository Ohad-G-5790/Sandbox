import pytest
from helpers import block, buy, sell

from hftbot.costs import BreakevenInputs, breakeven
from hftbot.execution import CostModel, PaperVenue
from hftbot.market import Side
from hftbot.portfolio import Fill, Portfolio
from hftbot.pricing import Quote
from hftbot.risk import RiskEngine, RiskLimits


def test_post_only_rejects_crossing_orders_but_still_pays_gas():
    venue = PaperVenue(CostModel(gas_per_tx=0.5))
    receipt = venue.replace_quotes([Quote(Side.BUY, 100.01, 1.0), Quote(Side.SELL, 100.03, 1.0)], block(0))
    assert [q.side for q in receipt.rejected] == [Side.BUY]
    assert receipt.gas == 0.5
    assert [o.side for o in venue.open_orders()] == [Side.SELL]


def test_orders_are_not_live_in_the_block_they_were_posted():
    venue = PaperVenue()
    venue.replace_quotes([Quote(Side.BUY, 99.98, 1.0)], block(0))
    assert venue.match(block(0, trades=(sell(99.97, 5.0),))) == []
    assert len(venue.match(block(1, trades=(sell(99.97, 5.0),)))) == 1


def test_joining_a_level_waits_behind_its_queue():
    venue = PaperVenue(CostModel(maker_fee_bps=1.0))
    venue.replace_quotes([Quote(Side.BUY, 99.99, 2.0)], block(0, size=10.0))  # 10 already resting at 99.99
    assert venue.match(block(1, trades=(sell(99.99, 6.0),))) == []
    (fill,) = venue.match(block(2, trades=(sell(99.99, 5.0),)))  # 4 left ahead, then 1 for us
    assert fill.size == 1.0 and fill.price == 99.99
    assert fill.fee == pytest.approx(99.99 * 1.0 * 1e-4)


def test_print_through_our_price_fills_us():
    venue = PaperVenue()
    venue.replace_quotes([Quote(Side.SELL, 100.02, 3.0)], block(0))
    (fill,) = venue.match(block(1, trades=(buy(100.03, 10.0),)))
    assert fill.side is Side.SELL and fill.size == 3.0 and fill.price == 100.02
    assert venue.open_orders() == []


def test_same_side_prints_do_not_fill():
    venue = PaperVenue()
    venue.replace_quotes([Quote(Side.BUY, 100.0, 1.0)], block(0))
    assert venue.match(block(1, trades=(buy(99.9, 5.0),))) == []


def test_market_order_sweeps_levels_and_pays_taker_fee():
    venue = PaperVenue(CostModel(taker_fee_bps=10.0))
    receipt = venue.market_order(Side.SELL, 15.0, block(0, size=10.0))
    assert [(f.price, f.size) for f in receipt.fills] == [(99.99, 10.0), (99.98, 5.0)]
    assert all(not f.maker for f in receipt.fills)


def test_portfolio_accounting_round_trip():
    p = Portfolio(cash=1000.0)
    p.mark(100.0, 0.0)
    p.apply_fill(Fill(1, Side.BUY, 99.0, 2.0, 0.1, 1.0, 1))
    p.apply_fill(Fill(2, Side.SELL, 101.0, 2.0, 0.1, 5.0, 2))
    p.charge_gas(0.3)
    p.mark(100.0, 6.0)
    assert p.position == 0.0
    assert p.realized_pnl == pytest.approx(4.0)
    assert p.pnl == pytest.approx(4.0 - 0.2 - 0.3)
    assert p.position_opened_ts is None


def test_portfolio_tracks_drawdown_age_and_flips():
    p = Portfolio(cash=1000.0)
    p.mark(100.0, 0.0)
    p.apply_fill(Fill(1, Side.BUY, 100.0, 5.0, 0.0, 10.0, 1))
    p.mark(98.0, 40.0)
    assert p.drawdown == pytest.approx(10.0)
    assert p.position_age_s(40.0) == 30.0
    p.apply_fill(Fill(2, Side.SELL, 98.0, 8.0, 0.0, 50.0, 2))  # flip to short 3
    assert p.position == -3.0 and p.avg_entry == 98.0 and p.position_opened_ts == 50.0


def _portfolio(position=0.0, mid=100.0, age_ts=None):
    p = Portfolio(cash=10_000.0)
    p.mark(mid, 0.0)
    if position:
        side = Side.BUY if position > 0 else Side.SELL
        p.apply_fill(Fill(1, side, mid, abs(position), 0.0, age_ts or 0.0, 0))
    return p


def test_order_size_and_position_limits_veto_growing_quotes_only():
    risk = RiskEngine(RiskLimits(max_position=10.0, max_order_size=5.0))
    p = _portfolio(position=8.0)
    verdict = risk.check_quotes([Quote(Side.BUY, 99.9, 3.0), Quote(Side.SELL, 100.1, 3.0), Quote(Side.SELL, 100.2, 6.0)], p, 1.0)
    assert verdict.approved == (Quote(Side.SELL, 100.1, 3.0),)
    assert {why.split()[0] for _, why in verdict.vetoed} == {"position", "order"}


def test_old_inventory_is_reduce_only():
    risk = RiskEngine(RiskLimits(max_inventory_age_s=60.0))
    p = _portfolio(position=5.0, age_ts=0.0)
    verdict = risk.check_quotes([Quote(Side.BUY, 99.9, 1.0), Quote(Side.SELL, 100.1, 1.0)], p, 61.0)
    assert [q.side for q in verdict.approved] == [Side.SELL]


def test_leverage_limit():
    risk = RiskEngine(RiskLimits(max_position=1e9, max_order_size=1e9, max_leverage=1.0))
    verdict = risk.check_quotes([Quote(Side.BUY, 100.0, 150.0)], _portfolio(), 0.0)
    assert verdict.approved == ()


def test_account_limits_latch_the_kill_switch():
    risk = RiskEngine(RiskLimits(max_daily_loss=50.0))
    p = _portfolio(position=10.0)
    assert risk.check_account(p, 1.0) is None
    p.mark(94.0, 2.0)  # -60
    assert "daily loss" in risk.check_account(p, 2.0)
    p.mark(200.0, 3.0)
    assert risk.killed and risk.check_account(p, 3.0)  # stays down even after recovering
    assert risk.check_quotes([Quote(Side.SELL, 200.1, 1.0)], p, 3.0).approved == ()
    risk.reset()
    assert not risk.killed


def test_api_error_burst_kills():
    risk = RiskEngine(RiskLimits(max_api_errors=2, api_error_window_s=10.0))
    for ts in (0.0, 1.0, 2.0):
        risk.record_api_error(ts)
    assert "API errors" in risk.check_account(_portfolio(), 3.0)


def test_breakeven_gas_dominates_small_orders():
    # Roughly jev-trader's live numbers: ~$0.0008 gas per batch tx, 200 MON orders at $0.0226.
    small = breakeven(BreakevenInputs(price=0.0226, order_size=200, fill_rate=0.05, gas_per_tx=0.0008))
    assert small.half_spread_bps > 30  # vs a ~7 bps book: every-block requoting at this size cannot pay
    big = breakeven(BreakevenInputs(price=0.0226, order_size=20_000, fill_rate=0.05, gas_per_tx=0.0008, requote_every_blocks=5))
    assert big.half_spread_bps < 0.1
    assert small.gas_per_hour == pytest.approx(0.0008 * 3600 / 0.3)
