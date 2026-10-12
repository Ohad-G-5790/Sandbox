import math

import pytest
from helpers import block, book, buy, sell

from hftbot.features import AccountState, HealthState, StateEngine, TimeWindow, estimate_tokens, snapshot
from hftbot.market import Block, Book, Level, Side
from hftbot.sim import SimConfig, simulate


def test_book_derived_prices():
    b = Book(0.0, (Level(99.0, 3.0), Level(98.0, 1.0)), (Level(101.0, 1.0), Level(102.0, 1.0)))
    assert b.mid == 100.0
    assert b.spread_bps == pytest.approx(200.0)
    # More size on the bid means price is likelier to tick up: microprice sits above mid.
    assert b.microprice == pytest.approx((99.0 * 1.0 + 101.0 * 3.0) / 4.0)
    assert b.imbalance(levels=2) == pytest.approx((4 - 2) / 6)
    assert b.size_at(Side.BUY, 98.0) == 1.0
    assert b.size_at(Side.SELL, 100.0) == 0.0


def test_crossed_book_is_rejected():
    with pytest.raises(ValueError):
        Book(0.0, (Level(100.0, 1.0),), (Level(100.0, 1.0),))


def test_time_window_evicts_old_values():
    w = TimeWindow(10.0)
    w.push(0.0, 1.0)
    w.push(5.0, 2.0)
    w.push(11.0, 4.0)
    assert w.total == 6.0
    assert w.oldest == (5.0, 2.0)


def test_returns_and_flow_features():
    engine = StateEngine()
    engine.update(block(0, 99.99, 100.01))
    f = engine.update(block(1, 100.09, 100.11, trades=(buy(100.11, 3.0, 1000.2), sell(100.09, 1.0, 1000.25))))
    assert f.ret_bps[0] == pytest.approx(math.log(100.10 / 100.0) * 1e4)
    assert f.buy_volume == 3.0 and f.sell_volume == 1.0
    assert f.aggr_buy_ratio == 0.75
    assert f.sigma_price == pytest.approx(math.sqrt(0.1**2 / 0.3))


def test_features_never_use_future_blocks():
    blocks = simulate(SimConfig(blocks=400, seed=3)).blocks
    full, prefix = StateEngine(), StateEngine()
    for b in blocks:
        full.update(b)
        if b.number == 200:
            at_200 = full.features
    for b in blocks[:201]:
        prefix.update(b)
    assert prefix.features == at_200


def test_snapshot_is_compact_and_numeric():
    engine = StateEngine()
    for b in simulate(SimConfig(blocks=2000, seed=1)).blocks:
        f = engine.update(b)
    state = snapshot(f, AccountState(inventory=12.5, unrealized_pnl=-1.2), HealthState(latency_p50_ms=88.0))
    assert estimate_tokens(state) < 400

    def leaves(x):
        if isinstance(x, dict):
            for v in x.values():
                yield from leaves(v)
        else:
            yield x

    assert all(isinstance(v, (int, float)) for v in leaves(state))


def test_reference_venue_divergence():
    b = Block(0, 0.0, book(), (), reference_mid=99.0)
    f = StateEngine().update(b)
    assert f.ref_divergence_bps == pytest.approx((100.0 / 99.0 - 1) * 1e4)
