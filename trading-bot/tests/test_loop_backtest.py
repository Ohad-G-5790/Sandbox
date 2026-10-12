import json

import pytest
from helpers import FixedEngine, block, decision, sell

from hftbot.backtest import SimulatedLatency, compare, format_table, run_backtest
from hftbot.config import BotConfig, config_from_dict, load_config
from hftbot.execution import CostModel, PaperVenue
from hftbot.journal import Journal, block_from_dict, block_to_dict, read_jsonl
from hftbot.judgments import DecisionTimeout, DecisionUnavailable
from hftbot.loop import Ladder, LoopConfig, TradingLoop
from hftbot.market import Side
from hftbot.portfolio import Portfolio
from hftbot.pricing import PricingConfig
from hftbot.risk import RiskEngine, RiskLimits
from hftbot.rules import AlwaysQuoteEngine, RulesEngine
from hftbot.sim import SimConfig, simulate


def make_loop(engine, fallback=None, limits=None, gas=0.0, requote_ticks=0, alerts=None):
    return TradingLoop(
        engine=engine,
        fallback=fallback,
        venue=PaperVenue(CostModel(gas_per_tx=gas)),
        portfolio=Portfolio(10_000.0),
        risk=RiskEngine(limits or RiskLimits()),
        pricing=PricingConfig(min_half_spread_bps=1.0, order_size=5.0),
        loop=LoopConfig(requote_ticks=requote_ticks),
        on_alert=(alerts.append if alerts is not None else None),
    )


def test_normal_block_posts_two_sided_quotes():
    loop = make_loop(FixedEngine(decision()))
    record = loop.step(block(0))
    assert record.ladder is Ladder.NORMAL
    assert {q.side for q in record.resting} == {Side.BUY, Side.SELL}


def test_late_decision_cancels_and_holds():
    loop = make_loop(FixedEngine(decision()))
    loop.step(block(0))
    loop.engine = FixedEngine(decision(latency_ms=1_000.0))
    record = loop.step(block(1))
    assert record.ladder is Ladder.LATE and record.resting == ()


def test_timeout_cancels_and_holds():
    loop = make_loop(FixedEngine(decision()))
    loop.step(block(0))
    loop.engine = FixedEngine(DecisionTimeout("slow"))
    assert loop.step(block(1)).resting == ()


def test_unavailable_engine_uses_fallback_in_degraded_mode():
    loop = make_loop(FixedEngine(DecisionUnavailable("down")), fallback=FixedEngine(decision()))
    record = loop.step(block(0))
    assert record.ladder is Ladder.FALLBACK
    assert record.action is not None and record.action.size_mult < 1.0
    assert record.resting


def test_unavailable_without_fallback_stays_out():
    record = make_loop(FixedEngine(DecisionUnavailable("down"))).step(block(0))
    assert record.ladder is Ladder.UNAVAILABLE and record.resting == ()


def test_low_confidence_is_reported_on_the_ladder():
    record = make_loop(FixedEngine(decision(quote_conf=0.3))).step(block(0))
    assert record.ladder is Ladder.LOW_CONFIDENCE


def test_unchanged_quotes_are_not_resent():
    loop = make_loop(FixedEngine(decision()), gas=1.0)
    assert loop.step(block(0)).gas == 1.0
    assert loop.step(block(1)).gas == 0.0
    assert loop.venue.stats.txs == 1


def test_drawdown_trips_kill_switch_and_flattens():
    alerts: list[str] = []
    loop = make_loop(FixedEngine(decision()), limits=RiskLimits(max_drawdown=0.2, max_daily_loss=1e9), alerts=alerts)
    loop.step(block(0))
    bid = next(q for q in loop.venue.open_orders() if q.side is Side.BUY)
    loop.step(block(1, trades=(sell(round(bid.price - 0.01, 2), 5.0),)))  # a sell through our bid fills us: long 5
    assert loop.portfolio.position == 5.0
    record = loop.step(block(2, 99.79, 99.81))  # price drops 20 cents: -1.0 > 0.2 drawdown
    assert record.ladder is Ladder.KILLED
    assert loop.portfolio.position == 0.0 and loop.venue.open_orders() == []
    assert alerts and "kill switch" in alerts[0]
    assert loop.step(block(3)).ladder is Ladder.KILLED  # stays down


def test_stale_book_holds():
    loop = make_loop(FixedEngine(decision()), limits=RiskLimits(max_stale_data_s=1.0))
    b = block(0)
    stale = type(b)(b.number, b.ts + 5.0, b.book, b.trades)
    assert loop.step(stale).ladder is Ladder.STALE


def test_backtest_is_deterministic_and_reports_metrics():
    blocks = simulate(SimConfig(blocks=3000, seed=11)).blocks
    cfg = BotConfig(costs=CostModel(gas_per_tx=0.001))
    a = run_backtest(blocks, SimulatedLatency(AlwaysQuoteEngine(), seed=1), cfg)
    b = run_backtest(blocks, SimulatedLatency(AlwaysQuoteEngine(), seed=1), cfg)
    assert a == b
    assert a.fills > 0 and a.gas > 0 and a.blocks == 3000
    assert a.ladder.get("late", 0) > 0
    assert sum(a.ladder.values()) == 3000


def test_compare_and_table():
    blocks = simulate(SimConfig(blocks=1500, seed=5)).blocks
    results = compare(blocks, {"always": AlwaysQuoteEngine, "rules": lambda: RulesEngine(100.0)})
    table = format_table(results)
    assert "always" in table and "rules" in table and "Adverse selection" in table


def test_simulated_outages_go_to_fallback():
    blocks = simulate(SimConfig(blocks=500, seed=2)).blocks
    m = run_backtest(blocks, SimulatedLatency(AlwaysQuoteEngine(), outage_rate=0.5, seed=3), fallback=RulesEngine(100.0))
    assert m.ladder["fallback"] > 100


def test_journal_round_trip(tmp_path):
    blocks = simulate(SimConfig(blocks=50, seed=1)).blocks
    path = tmp_path / "journal.jsonl"
    with Journal(path) as journal:
        run_backtest(blocks, AlwaysQuoteEngine(), on_step=journal.write)
    records = list(read_jsonl(path))
    assert len(records) == 50 and records[-1]["decision"]["source"] == "always-quote"
    assert block_from_dict(json.loads(json.dumps(block_to_dict(blocks[7])))) == blocks[7]


def test_config_loading(tmp_path):
    cfg = load_config(__file__.replace("tests/test_loop_backtest.py", "config.example.toml"))
    assert cfg.loop.requote_ticks == 1 and cfg.features.return_windows_s == (60.0, 300.0, 1800.0)
    with pytest.raises(ValueError):
        config_from_dict({"risk": {"max_positon": 1}})


def test_rules_engine_flags_toxic_flow_and_stress():
    calm = {
        "price": {"ret_bps": {"1m": 0.1, "5m": 0.2, "30m": 0.0}},
        "book": {"imbalance": 0.0, "depth_vs_24h": 1.0, "spread_bps": 4.0},
        "flow_1m": {"buy_ratio": 0.5},
        "vol": {"rv_5m_bps": 10.0, "vs_24h": 1.0},
        "position": {"inventory": 0.0, "age_s": 0},
    }
    stormy = {
        **calm,
        "price": {"ret_bps": {"1m": 15.0, "5m": 40.0, "30m": 0.0}},
        "book": {"imbalance": 0.4, "depth_vs_24h": 0.3, "spread_bps": 12.0},
        "flow_1m": {"buy_ratio": 0.95},
        "vol": {"rv_5m_bps": 10.0, "vs_24h": 3.5},
    }
    engine = RulesEngine(max_position=100.0)
    c, s = engine.decide(calm, 1.0), engine.decide(stormy, 1.0)
    assert c.noul("toxic_flow") < 0.5 < s.noul("toxic_flow")
    assert c.noul("liquidity_stressed") < 0.5 < s.noul("liquidity_stressed")
    assert c.score("quote_environment").score > s.score("quote_environment").score
    assert s.choice("regime").choice in ("crisis", "trending")
