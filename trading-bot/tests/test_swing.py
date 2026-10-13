import csv
import io
import json
import zipfile

import pytest

from hftbot.swing import data
from hftbot.swing.candles import HOUR, Candle, align, resample
from hftbot.swing.research import Report, SwingCosts, evaluate, metrics, plan_orders, simulate, walk_forward
from hftbot.swing.strategies import Strategy, prev_extreme, rsi, signal_breakout, signal_trend, sma
from hftbot.swing.synthetic import synthetic_hourly
from hftbot.swing.trader import CandleStore, PaperExecutor, SwingConfig, SwingTrader, load_state, load_swing_config


def candles(closes, start=0, step=HOUR):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Candle(start + i * step, prev, max(prev, c), min(prev, c), c, 1.0))
        prev = c
    return out


# --- indicators and signals ---


def test_sma_rsi_and_channel():
    assert sma([None, 1, 2, 3, 4], 2) == [None, None, 1.5, 2.5, 3.5]
    assert prev_extreme([3, 1, 2, 5, 4], 2, highest=True) == [None, None, 3, 2, 5]
    assert prev_extreme([3, 1, 2, 5, 4], 2, highest=False) == [None, None, 1, 1, 2]
    rising = rsi([float(i) for i in range(30)], 14)
    assert rising[13] is None and rising[14] == 100.0
    flat_then_drop = rsi([10.0] * 15 + [9.0], 14)
    assert flat_then_drop[15] == pytest.approx(0.0)


def test_signals_never_look_ahead():
    prices = [100 + (i % 17) - (i % 5) * 2 + i * 0.3 for i in range(400)]
    for signal, kw in ((signal_trend, {"fast": 5, "slow": 20}), (signal_breakout, {"entry": 20, "exit": 10})):
        full = signal(prices, **kw)
        for cut in (100, 250, 399):
            assert signal(prices[:cut], **kw) == full[:cut]


def test_strategy_weights_use_hours_and_split_sleeves():
    hourly = synthetic_hourly(("A", "B"), years=0.5, seed=3)
    panel = align({c: resample(v, 4) for c, v in hourly.items()}, 4)
    w = Strategy("trend", {"fast_h": 24, "slow_h": 168}).weights(panel)
    assert set(w) == {"A", "B"} and max(max(v) for v in w.values()) == 0.5  # two coins: half each at most
    hold = Strategy("hold").weights(panel)
    assert hold["A"][-1] == 0.5


# --- candles ---


def test_resample_drops_the_bar_in_progress():
    hourly = candles([1, 2, 3, 4, 5, 6], start=0)
    bars = resample(hourly, 4)
    assert len(bars) == 1 and bars[0].open == 1 and bars[0].close == 4 and bars[0].high == 4
    assert resample(hourly, 4, now=8 * HOUR)[-1].ts == 4 * HOUR  # the second bar is complete at 8h


def test_align_marks_unlisted_and_fills_gaps():
    a = candles([1, 2, 3, 4])
    b = [Candle(2 * HOUR, 9, 9, 9, 9, 1), Candle(3 * HOUR, 9, 10, 9, 10, 1)]
    gap = [a[0], a[1], a[3]]
    panel = align({"A": gap, "B": b}, 1)
    assert panel.close["B"] == [None, None, 9, 10]
    assert panel.close["A"] == [1, 2, 2, 4]  # the missing hour carries the last close


# --- simulation ---


def test_simulate_charges_costs_and_trades_next_open():
    panel = align({"A": candles([100, 100, 110, 110])}, 1)
    w = {"A": [1.0, 1.0, 1.0, 1.0]}
    costs = SwingCosts(fee_bps=10, slippage_bps=10, network_fee=0.0, min_trade=1, rebalance_band=0.05)
    equity, trades, paid, _, book = simulate(panel, w, costs, 0, 4, cash=1000.0)
    # Bought at bar 1's open (100) with 1000 minus 0.2% cost: 9.98 units, worth 1097.8 at 110.
    assert trades == 1 and paid == pytest.approx(2.0)
    assert equity[0] == 1000.0 and equity[-1] == pytest.approx(9.98 * 110)
    assert book.units["A"] == pytest.approx(9.98)


def test_plan_orders_sells_first_and_ignores_small_drift():
    costs = SwingCosts(min_trade=10, rebalance_band=0.05)
    orders = plan_orders({"A": 0.5, "B": 0.0, "C": 0.27}, {"A": 0.0, "B": 2.0, "C": 2.5}, 500.0, {"A": 100.0, "B": 100.0, "C": 100.0}, costs)
    # total 950: A wants +475, B closes (-200), C is at 250 vs 256.5 target: inside the 5% band.
    assert [(c, round(d, 1), closing) for c, d, closing in orders] == [("B", -200.0, True), ("A", 475.0, False)]


def test_hold_buys_once_and_never_rebalances():
    panel = align({"A": candles([100, 100, 200, 50, 100]), "B": candles([100, 100, 100, 100, 100])}, 1)
    m = evaluate(panel, Strategy("hold"), SwingCosts(network_fee=0.0, min_trade=1), 0, 5)
    assert m.trades == 2


def test_metrics_drawdown_and_return():
    m = metrics([1100, 880, 990], 1000.0, 1, 1.0, 1.0, 24)
    assert m.total_return == pytest.approx(-0.01) and m.max_drawdown == pytest.approx(0.2)


# --- walk-forward ---


def test_walk_forward_is_deterministic_and_judges_against_hold():
    hourly = synthetic_hourly(("A", "B"), years=2, seed=5)
    panel = align({c: resample(v, 4) for c, v in hourly.items()}, 4)
    grid = {"trend": [Strategy("trend", {"fast_h": 24, "slow_h": 168}), Strategy("trend", {"fast_h": 48, "slow_h": 336})]}
    a = walk_forward(panel, SwingCosts(), folds=3, grid=grid)
    b = walk_forward(panel, SwingCosts(), folds=3, grid=grid)
    assert a == b and a.configs_tried == 2 and len(a.families[0].picks) == 3
    r = a.families[0]
    better = type(r.oos)(**{**r.oos.__dict__, "sharpe": a.hold.sharpe + 1, "max_drawdown": a.hold.max_drawdown / 2})
    good = Report((type(r)(r.family, r.picks, better, 2, r.final_pick),), a.hold, a.hold_by_coin, 2, a.test_start_ts, a.end_ts, a.coins)
    assert good.best() is not None
    one_coin = Report((type(r)(r.family, r.picks, better, 0, r.final_pick),), a.hold, a.hold_by_coin, 2, a.test_start_ts, a.end_ts, a.coins)
    assert one_coin.best() is None  # must win on most coins, not just the basket


def test_too_little_history_is_refused():
    panel = align({"A": candles([100.0] * 100)}, 1)
    with pytest.raises(ValueError, match="not enough history"):
        walk_forward(panel, SwingCosts())


# --- data sources ---


def _zip(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        text = io.StringIO()
        csv.writer(text).writerows(rows)
        z.writestr("x.csv", text.getvalue())
    return buf.getvalue()


def test_binance_files_milliseconds_and_microseconds_and_missing_months():
    import datetime as dt

    jan = _zip([[1704067200000, "1", "2", "0.5", "1.5", "10", 0, 0, 0, 0, 0, 0]])
    feb_us = _zip([[1706745600000000, "1.5", "3", "1", "2.5", "20", 0, 0, 0, 0, 0, 0]])

    def get(url, timeout):
        if "2024-01.zip" in url:
            return jan
        if "2024-02.zip" in url:
            return feb_us
        raise data.NotFound(url)

    out = data.fetch_binance("SOLUSDT", dt.date(2023, 12, 1), dt.date(2024, 3, 15), get=get, recent_api=False)
    assert [(c.ts, c.close) for c in out] == [(1704067200, 1.5), (1706745600, 2.5)]


def test_coinbase_pages_and_sorts():
    pages = [[[2 * HOUR, 1, 3, 2, 2.5, 10], [HOUR, 1, 2, 1, 1.5, 5]], [[3 * HOUR, 2, 4, 2.5, 3.5, 7]]]
    calls = []

    def get(url, timeout):
        calls.append(url)
        return json.dumps(pages.pop(0) if pages else []).encode()

    out = data.fetch_coinbase("SOL-USD", HOUR, 302 * HOUR, get=get, pause_s=0)
    assert [c.ts for c in out] == [HOUR, 2 * HOUR, 3 * HOUR] and out[0].open == 1 and out[0].close == 1.5
    assert "granularity=3600" in calls[0] and len(calls) == 2


# --- the trader ---


def _store(tmp_path, cfg, hourly):
    def fetch(coin, source, since):
        return [c for c in hourly[coin.symbol] if c.ts >= since]

    return CandleStore(tmp_path / "candles", cfg, fetch=fetch)


def _paper_trader(tmp_path, cfg, hourly, now):
    executor = PaperExecutor({"usdc": 1000.0, "units": {}}, cfg.costs, cfg.coins)
    return SwingTrader(cfg, _store(tmp_path, cfg, hourly), executor, tmp_path / "state.json", clock=lambda: now, journal_path=tmp_path / "journal.jsonl")


def test_paper_tick_trades_toward_a_named_strategy(tmp_path):
    hourly = synthetic_hourly(("SOL", "ETH"), years=1, seed=2)
    now = hourly["SOL"][-1].ts + HOUR + 60
    cfg = SwingConfig(coins=("SOL", "ETH"), strategy="hold", params={"_": 0}, max_weight_per_coin=0.5)
    trader = _paper_trader(tmp_path, cfg, hourly, now)
    record = trader.tick()
    assert {t["coin"] for t in record["trades"]} == {"SOL", "ETH"}
    usdc, units = trader.executor.balances()
    assert usdc < 50 and all(u > 0 for u in units.values())
    assert load_state(tmp_path / "state.json")["paper"]["usdc"] == usdc  # survives a restart
    assert trader.tick()["trades"] == []  # already at target


def test_kill_switch_sells_everything_and_stays_latched(tmp_path):
    hourly = synthetic_hourly(("SOL",), years=1, seed=2)
    now = hourly["SOL"][-1].ts + HOUR + 60
    cfg = SwingConfig(coins=("SOL",), strategy="hold", params={"_": 0}, max_weight_per_coin=1.0, max_drawdown_pct=10)
    trader = _paper_trader(tmp_path, cfg, hourly, now)
    trader.tick()
    trader.state["peak_equity"] = 10_000.0  # pretend we were far higher: a 90% drawdown
    record = trader.tick()
    assert "kill switch" in record["note"] and record["trades"][0]["side"] == "sell"
    again = SwingTrader(cfg, trader.store, trader.executor, tmp_path / "state.json", clock=lambda: now)
    assert again.state["killed"] and "kill switch" in again.tick()["note"]


def test_stale_data_means_no_trading(tmp_path):
    hourly = synthetic_hourly(("SOL",), years=1, seed=2)
    now = hourly["SOL"][-1].ts + 3 * 24 * HOUR  # feed stopped three days ago
    cfg = SwingConfig(coins=("SOL",), strategy="hold", params={"_": 0})
    record = _paper_trader(tmp_path, cfg, hourly, now).tick()
    assert "stale" in record["note"] and record["trades"] == []


def test_auto_with_no_passing_strategy_stays_in_usdc(tmp_path, monkeypatch):
    import hftbot.swing.trader as trader_module

    hourly = synthetic_hourly(("SOL", "ETH"), years=2, seed=4)
    now = hourly["SOL"][-1].ts + HOUR + 60
    cfg = SwingConfig(coins=("SOL", "ETH"))
    monkeypatch.setattr(trader_module, "candidate_grid", lambda: {"trend": [Strategy("trend", {"fast_h": 24, "slow_h": 168})]})
    monkeypatch.setattr(Report, "best", lambda self: None)
    trader = _paper_trader(tmp_path, cfg, hourly, now)
    record = trader.tick()
    assert record["note"].startswith("no strategy beats holding") and record["trades"] == []
    assert (tmp_path / "report.txt").exists()


def test_paper_fills_at_jupiter_quotes_when_available():
    quotes = []

    def quote(in_mint, out_mint, amount):
        quotes.append((in_mint, out_mint, amount))
        return amount, 2_000_000  # 0.002 SOL... per the mint decimals below
    executor = PaperExecutor({"usdc": 100.0, "units": {}}, SwingCosts(network_fee=0.0), ("SOL",), quote)
    result = executor.buy("SOL", 50.0, ref_price=150.0)
    assert result.source == "jupiter-quote" and result.units == pytest.approx(0.002)
    assert quotes[0][0] == data.USDC_MINT and quotes[0][2] == 50_000_000


def test_config_file(tmp_path):
    cfg = load_swing_config(__file__.replace("tests/test_swing.py", "swing.toml"))
    assert cfg.coins == ("SOL", "BTC", "ETH", "JUP") and cfg.costs.fee_bps == 10.0
    bad = tmp_path / "bad.toml"
    bad.write_text('[swing]\ncoins = ["DOGE"]\n')
    with pytest.raises(ValueError, match="unknown coins"):
        load_swing_config(bad)


solders = pytest.importorskip("solders")


class LiveRpc:
    def __init__(self, decimals=None, statuses=None):
        self.decimals = decimals or {}
        self.statuses = list(statuses or [{"err": None, "confirmationStatus": "confirmed"}])
        self.sent = []

    def call(self, method, params):
        import base64

        if method == "getAccountInfo":
            raw = bytearray(82)
            raw[44] = self.decimals.get(params[0], {data.USDC_MINT: 6, **{c.mint: c.decimals for c in data.COINS.values()}}.get(params[0], 0))
            return {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}}
        if method == "getTokenAccountsByOwner":
            if params[1]["programId"].startswith("Tokenz"):
                return {"value": []}
            accounts = [(data.USDC_MINT, "250000000"), (data.COINS["ETH"].mint, "150000000")]  # 250 USDC, 1.5 ETH
            return {"value": [{"account": {"data": {"parsed": {"info": {"mint": m, "tokenAmount": {"amount": a}}}}}} for m, a in accounts]}
        raise AssertionError(method)

    def get_balance(self, pubkey):
        return 2_050_000_000  # 2.05 SOL

    def send_transaction(self, signed):
        self.sent.append(signed)
        return "sig"

    def get_signature_statuses(self, sigs):
        return [self.statuses.pop(0) if self.statuses else None]


class LiveJupiter:
    def __init__(self, usdc_per_eth):
        self.usdc_per_eth = usdc_per_eth

    def quote(self, in_mint, out_mint, amount, slippage_bps):
        from hftbot.solana.jupiter import SwapQuote

        if in_mint == data.COINS["ETH"].mint:  # selling ETH (8 decimals) for USDC (6)
            out = round(amount / 1e8 * self.usdc_per_eth * 1e6)
        else:
            out = round(amount / 1e6 / self.usdc_per_eth * 1e8)
        return SwapQuote({}, amount, out, out, 0.0, "test")

    def swap_transaction(self, quote, user_pubkey, max_priority_lamports, wrap_and_unwrap_sol=False):
        from solders.hash import Hash
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.signature import Signature
        from solders.system_program import TransferParams, transfer
        from solders.transaction import VersionedTransaction

        me = Pubkey.from_string(user_pubkey)
        message = MessageV0.try_compile(me, [transfer(TransferParams(from_pubkey=me, to_pubkey=me, lamports=1))], [], Hash.default())
        return bytes(VersionedTransaction.populate(message, [Signature.default()]))


def _live(rpc, jupiter):
    from solders.keypair import Keypair

    from hftbot.swing.live import JupiterSwapExecutor

    return JupiterSwapExecutor(rpc, jupiter, Keypair(), ("SOL", "ETH"), sol_fee_reserve=0.05, max_price_deviation_pct=2.0, sleep=lambda s: None)


def test_live_balances_keep_the_sol_fee_reserve():
    usdc, units = _live(LiveRpc(), LiveJupiter(3000)).balances()
    assert usdc == 250.0 and units == {"SOL": pytest.approx(2.0), "ETH": 1.5}


def test_live_refuses_tokens_with_unexpected_decimals():
    from hftbot.swing.trader import SwapError

    _live(LiveRpc(), LiveJupiter(3000)).verify_tokens()
    with pytest.raises(SwapError, match="decimals"):
        _live(LiveRpc(decimals={data.COINS["ETH"].mint: 18}), LiveJupiter(3000)).verify_tokens()


def test_live_swap_is_signed_sent_and_confirmed():
    rpc = LiveRpc(statuses=[None, {"err": None, "confirmationStatus": "confirmed"}])
    result = _live(rpc, LiveJupiter(2990)).sell("ETH", 1.0, ref_price=3000.0)
    assert len(rpc.sent) == 1 and result.usdc == pytest.approx(2990) and result.source == "jupiter"


def test_live_refuses_a_swap_priced_far_from_the_reference():
    from hftbot.swing.trader import SwapError

    rpc = LiveRpc()
    with pytest.raises(SwapError, match="worse than the reference"):
        _live(rpc, LiveJupiter(2900)).sell("ETH", 1.0, ref_price=3000.0)  # 3.3% worse
    assert rpc.sent == []


def test_live_swap_failure_on_chain_is_an_error():
    from hftbot.swing.trader import SwapError

    rpc = LiveRpc(statuses=[{"err": {"InstructionError": [2, "Custom"]}}])
    with pytest.raises(SwapError, match="failed on chain"):
        _live(rpc, LiveJupiter(3000)).buy("ETH", 100.0, ref_price=3000.0)


def test_a_coin_without_data_is_left_out(tmp_path):
    hourly = synthetic_hourly(("SOL", "ETH"), years=0.2, seed=1)
    hourly["ETH"] = []
    cfg = SwingConfig(coins=("SOL", "ETH"))
    panel = _store(tmp_path, cfg, hourly).panel(hourly["SOL"][-1].ts + HOUR)
    assert panel.coins == ["SOL"]


def test_routine_binance_update_uses_one_api_call(monkeypatch):
    import time as time_module

    now = 1_760_000_000
    monkeypatch.setattr(time_module, "time", lambda: now)
    urls = []

    def get(url, timeout):
        urls.append(url)
        return json.dumps([[(now - 2 * HOUR) * 1000, "1", "2", "0.5", "1.5", "3"]]).encode()

    out = data.fetch(data.COINS["SOL"], "binance", now - 3 * HOUR, get=get)
    assert len(urls) == 1 and "api.binance.com" in urls[0] and out[0].close == 1.5


def test_holdings_in_a_coin_without_data_do_not_break_the_tick(tmp_path):
    hourly = synthetic_hourly(("SOL", "ETH"), years=1, seed=2)
    hourly["ETH"] = []
    now = hourly["SOL"][-1].ts + HOUR + 60
    cfg = SwingConfig(coins=("SOL", "ETH"), strategy="hold", params={"_": 0})
    trader = _paper_trader(tmp_path, cfg, hourly, now)
    trader.executor.book["units"]["ETH"] = 1.0
    record = trader.tick()
    assert [t["coin"] for t in record["trades"]] == ["SOL"] and trader.executor.book["units"]["ETH"] == 1.0


def test_a_failed_bar_is_retried_soon_not_next_bar(tmp_path):
    cfg = SwingConfig(coins=("SOL",))
    trader = SwingTrader(cfg, CandleStore(tmp_path / "c", cfg, fetch=lambda *a: (_ for _ in ()).throw(OSError("offline"))), PaperExecutor({"usdc": 1.0}, cfg.costs, cfg.coins), tmp_path / "s.json", clock=lambda: 1_000_000.0)
    sleeps = []
    trader.run_forever(should_stop=lambda: len(sleeps) >= 1, sleep=sleeps.append)
    assert sleeps == [300.0]


def test_size_sweep_fixed_fees_shrink_and_tiny_accounts_cannot_trade():
    from hftbot.swing.research import size_sweep

    hourly = synthetic_hourly(("A", "B", "C", "D"), years=1, seed=6)
    panel = align({c: resample(v, 4) for c, v in hourly.items()}, 4)
    rows = size_sweep(panel, Strategy("trend", {"fast_h": 24, "slow_h": 168}), SwingCosts(network_fee=0.05), [20, 1_000, 100_000])
    tiny, mid, big = rows
    assert tiny.trades_per_year == 0  # a quarter of $20 is below the $10 minimum trade
    assert mid.fixed_fees_per_year_pct > big.fixed_fees_per_year_pct * 50
    assert abs(mid.fees_per_year_pct - mid.fixed_fees_per_year_pct - (big.fees_per_year_pct - big.fixed_fees_per_year_pct)) < 0.5
    assert all(r.gross_cagr >= r.net_cagr for r in rows)
