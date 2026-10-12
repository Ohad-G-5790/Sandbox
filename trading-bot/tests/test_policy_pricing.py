import math

import pytest
from helpers import block, decision

from hftbot.features import StateEngine
from hftbot.market import Side
from hftbot.policy import ActionKind, PolicyAction, PolicyConfig, compose_action, fractional_kelly
from hftbot.pricing import PricingConfig, make_quotes, optimal_half_spread, reservation_price

CFG = PolicyConfig()


def test_toxic_flow_pulls_quotes_before_anything_else():
    assert compose_action(decision(toxic=0.61, stressed=0.9, quote_env=3.0), 0.0, CFG).kind is ActionKind.PULL_QUOTES


def test_stressed_liquidity_widens():
    action = compose_action(decision(stressed=0.75), 0.0, CFG)
    assert action.kind is ActionKind.WIDEN and action.spread_mult == CFG.widen_mult


def test_confident_good_environment_quotes_full_size():
    action = compose_action(decision(quote_env=2.4, quote_conf=0.9), 0.0, CFG)
    assert action.kind is ActionKind.QUOTE and action.size_mult == 1.0 and not action.low_confidence


def test_low_confidence_reduces_size_unless_gating_is_off():
    action = compose_action(decision(quote_env=2.4, quote_conf=0.5), 0.0, CFG)
    assert action.low_confidence and action.size_mult == CFG.low_confidence_size_mult
    ungated = compose_action(decision(quote_env=2.4, quote_conf=0.5), 0.0, PolicyConfig(confidence_gating=False))
    assert ungated.size_mult == 1.0


def test_marginal_environment_quotes_wide_and_bad_one_stands_down():
    assert compose_action(decision(quote_env=1.5), 0.0, CFG).kind is ActionKind.QUOTE_WIDE
    assert compose_action(decision(quote_env=0.5), 0.0, CFG).kind is ActionKind.STAND_DOWN


def test_inventory_pressure_skews_and_then_goes_reduce_only():
    long_mild = compose_action(decision(pressure=1.0), 40.0, CFG)
    assert long_mild.skew > 0  # lean toward selling
    assert compose_action(decision(pressure=1.0), -40.0, CFG).skew < 0
    urgent = compose_action(decision(pressure=3.0), 40.0, CFG)
    assert urgent.sides == frozenset({Side.SELL})
    # Even when the environment says stand down, urgent inventory still gets a reducing quote.
    assert compose_action(decision(pressure=3.0, quote_env=0.2), -40.0, CFG).kind is ActionKind.REDUCE_ONLY


def test_degraded_mode_quotes_wider_and_smaller():
    action = compose_action(decision(), 0.0, CFG, degraded=True)
    assert action.spread_mult == CFG.degraded_spread_mult and action.size_mult == CFG.degraded_size_mult


def test_fractional_kelly_is_capped_and_never_negative():
    assert fractional_kelly(0.55) == pytest.approx(0.10)
    assert fractional_kelly(0.9) == 0.25
    assert fractional_kelly(0.4) == 0.0


def test_direction_lean_only_when_enabled():
    bullish = decision(up=0.7, down=0.1)
    assert compose_action(bullish, 0.0, CFG).skew == 0.0
    leaning = compose_action(bullish, 0.0, PolicyConfig(direction_kelly=True))
    assert leaning.skew < 0  # expecting up: shift quotes up, more eager to buy


def test_avellaneda_stoikov_formulas():
    assert reservation_price(100.0, 10.0, 0.1, 0.02, 30.0) == pytest.approx(100.0 - 10 * 0.1 * 0.0004 * 30)
    half = optimal_half_spread(0.1, 0.02, 30.0, 50.0)
    assert half == pytest.approx(0.1 * 0.0004 * 30 / 2 + math.log(1 + 0.1 / 50) / 0.1)


def _features(bid=99.95, ask=100.05):
    engine = StateEngine()
    engine.update(block(0, bid, ask))
    return engine.update(block(1, bid, ask))


def test_quotes_are_on_ticks_symmetric_when_flat_and_respect_min_spread():
    f = _features()
    cfg = PricingConfig(min_half_spread_bps=3.0, order_size=5.0)
    bid, ask = make_quotes(f, 0.0, PolicyAction(ActionKind.QUOTE), cfg)
    assert bid.side is Side.BUY and ask.side is Side.SELL
    assert 100.0 - bid.price >= 0.03 - 1e-9 and ask.price - 100.0 >= 0.03 - 1e-9
    assert round(bid.price / 0.01) * 0.01 == pytest.approx(bid.price)
    assert bid.size == ask.size == 5.0


def test_long_inventory_shifts_both_quotes_down():
    f = _features()
    cfg = PricingConfig(gamma=50.0, min_half_spread_bps=1.0)
    flat = make_quotes(f, 0.0, PolicyAction(ActionKind.QUOTE), cfg)
    long = make_quotes(f, 50.0, PolicyAction(ActionKind.QUOTE), cfg)
    assert long[0].price <= flat[0].price and long[1].price <= flat[1].price


def test_quotes_never_cross_the_touch():
    f = _features(99.99, 100.01)
    # A huge skew wants to sell far below the bid: post-only clamps it to one tick above the best bid.
    quotes = make_quotes(f, 0.0, PolicyAction(ActionKind.QUOTE, skew=1.0, spread_mult=0.1), PricingConfig(min_half_spread_bps=50))
    ask = next(q for q in quotes if q.side is Side.SELL)
    assert ask.price >= 99.99 + 0.01 - 1e-9
    assert all(q.price < 100.01 for q in quotes if q.side is Side.BUY)


def test_pull_and_one_sided_actions():
    f = _features()
    assert make_quotes(f, 0.0, PolicyAction(ActionKind.PULL_QUOTES), PricingConfig()) == []
    sells = make_quotes(f, 10.0, PolicyAction(ActionKind.REDUCE_ONLY, sides=frozenset({Side.SELL})), PricingConfig())
    assert [q.side for q in sells] == [Side.SELL]
