"""Backtesting the whole policy, not "did the model predict price".

Run every engine on identical data, features, costs and limits, and compare what the book actually
did: PnL after fees and gas, Sharpe, Sortino, drawdown, hit rate, spread capture, adverse selection,
decision cost and coverage.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from hftbot.config import BotConfig
from hftbot.execution import PaperVenue
from hftbot.features import StateEngine, estimate_tokens
from hftbot.jev import JEV_USD_PER_MILLION_INPUT_TOKENS
from hftbot.judgments import Decision, DecisionEngine, DecisionTimeout, DecisionUnavailable, Question, default_battery
from hftbot.loop import StepRecord, TradingLoop
from hftbot.market import Block
from hftbot.portfolio import Portfolio
from hftbot.risk import RiskEngine

MINUTES_PER_YEAR = 525_600


class SimulatedLatency:
    """Gives an instant engine a realistic latency distribution (and optional outages) for backtests."""

    def __init__(self, engine: DecisionEngine, median_ms: float = 90.0, sigma: float = 0.5, outage_rate: float = 0.0, seed: int = 0) -> None:
        self.engine = engine
        self.name = engine.name
        self.median_ms, self.sigma, self.outage_rate = median_ms, sigma, outage_rate
        self._rng = random.Random(seed)

    def decide(self, state: Mapping[str, Any], timeout_s: float) -> Decision:
        if self._rng.random() < self.outage_rate:
            raise DecisionUnavailable("simulated outage")
        latency = self.median_ms * math.exp(self.sigma * self._rng.gauss(0, 1))
        if latency > timeout_s * 1000:
            raise DecisionTimeout(f"simulated latency {latency:.0f} ms")
        return dataclasses.replace(self.engine.decide(state, timeout_s), latency_ms=latency)


@dataclass(frozen=True)
class Metrics:
    name: str
    blocks: int
    hours: float
    pnl: float
    pnl_per_hour: float
    fees: float
    gas: float
    decision_cost: float
    est_jev_cost: float
    """What Jev would have cost for this run's decisions at the published price."""
    fills: int
    volume: float
    sharpe: float
    sortino: float
    max_drawdown: float
    hit_rate: float
    spread_capture_bps: float
    markout_bps: float
    adverse_selection_bps: float
    coverage: float
    killed: str | None
    final_position: float
    ladder: dict[str, int] = field(default_factory=dict)


def battery_tokens(battery: Mapping[str, Question]) -> int:
    return math.ceil(len(json.dumps({k: dataclasses.asdict(q) for k, q in battery.items()})) / 4)


def run_backtest(
    blocks: Iterable[Block],
    engine: DecisionEngine,
    cfg: BotConfig | None = None,
    *,
    fallback: DecisionEngine | None = None,
    on_step: Callable[[StepRecord], None] | None = None,
    name: str | None = None,
) -> Metrics:
    cfg = cfg or BotConfig()
    venue = PaperVenue(cfg.costs, cfg.activation_delay_blocks)
    portfolio = Portfolio(cfg.starting_cash)
    loop = TradingLoop(
        engine=engine,
        fallback=fallback,
        venue=venue,
        portfolio=portfolio,
        risk=RiskEngine(cfg.risk),
        state=StateEngine(cfg.features),
        policy=cfg.policy,
        pricing=cfg.pricing,
        loop=cfg.loop,
        on_alert=lambda message: None,
    )
    question_tokens = battery_tokens(default_battery())

    mids: list[float] = []
    fills: list[tuple[int, float, Any]] = []  # (block index, mid at fill block, fill)
    minute_equity: list[float] = []
    last_minute: int | None = None
    ladder: Counter[str] = Counter()
    quoted_blocks = 0
    decision_tokens = estimated_tokens = 0
    first_ts = last_ts = None
    killed: str | None = None

    for index, block in enumerate(blocks):
        record = loop.step(block)
        if on_step:
            on_step(record)
        mids.append(record.mid)
        first_ts = record.ts if first_ts is None else first_ts
        last_ts = record.ts
        ladder[record.ladder.value] += 1
        quoted_blocks += bool(record.resting)
        for fill in record.fills:
            if fill.maker:
                fills.append((index, record.mid, fill))
        if record.decision is not None:
            if record.decision.source == "jev":
                decision_tokens += record.decision.input_tokens
            if record.snapshot is not None:
                estimated_tokens += estimate_tokens(record.snapshot) + question_tokens
        if record.ladder.value == "killed" and killed is None:
            killed = record.note
        minute = int(record.ts // 60)
        if minute != last_minute:
            minute_equity.append(record.equity)
            last_minute = minute

    n = len(mids)
    hours = ((last_ts - first_ts) / 3600) if n > 1 and first_ts is not None and last_ts is not None else 0.0
    minute_equity.append(portfolio.equity)
    returns = [(b - a) / cfg.starting_cash for a, b in zip(minute_equity, minute_equity[1:])]
    sharpe, sortino = _sharpe_sortino(returns)

    capture = markout = adverse = 0.0
    wins = total_size = 0.0
    for index, mid_at_fill, fill in fills:
        later = mids[min(index + cfg.markout_blocks, n - 1)]
        sign = fill.side.sign
        capture += sign * (mid_at_fill - fill.price) / mid_at_fill * 1e4 * fill.size
        markout += sign * (later - fill.price) / fill.price * 1e4 * fill.size
        adverse += sign * (later - mid_at_fill) / mid_at_fill * 1e4 * fill.size
        wins += fill.size if sign * (later - fill.price) > 0 else 0.0
        total_size += fill.size

    return Metrics(
        name=name or engine.name,
        blocks=n,
        hours=hours,
        pnl=portfolio.pnl,
        pnl_per_hour=portfolio.pnl / hours if hours else 0.0,
        fees=portfolio.fees,
        gas=portfolio.gas,
        decision_cost=decision_tokens * JEV_USD_PER_MILLION_INPUT_TOKENS / 1e6,
        est_jev_cost=estimated_tokens * JEV_USD_PER_MILLION_INPUT_TOKENS / 1e6,
        fills=portfolio.fill_count,
        volume=portfolio.volume,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=_max_drawdown(minute_equity),
        hit_rate=wins / total_size if total_size else 0.0,
        spread_capture_bps=capture / total_size if total_size else 0.0,
        markout_bps=markout / total_size if total_size else 0.0,
        adverse_selection_bps=adverse / total_size if total_size else 0.0,
        coverage=quoted_blocks / n if n else 0.0,
        killed=killed,
        final_position=portfolio.position,
        ladder=dict(ladder),
    )


def compare(blocks: list[Block], engines: Mapping[str, Callable[[], DecisionEngine]], cfg: BotConfig | None = None, **kwargs: Any) -> list[Metrics]:
    """Every engine on the same blocks, features, costs and limits."""
    return [run_backtest(blocks, make(), cfg, name=name, **kwargs) for name, make in engines.items()]


def _sharpe_sortino(returns: list[float]) -> tuple[float, float]:
    if len(returns) < 2:
        return 0.0, 0.0
    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns)
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))
    scale = math.sqrt(MINUTES_PER_YEAR)
    return (mean / sd * scale if sd > 0 else 0.0), (mean / downside * scale if downside > 0 else 0.0)


def _max_drawdown(equity: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e)
        worst = max(worst, peak - e)
    return worst


def format_table(results: list[Metrics]) -> str:
    rows = [
        ("PnL (after fees+gas)", lambda m: f"{m.pnl:,.2f}"),
        ("PnL / hour", lambda m: f"{m.pnl_per_hour:,.2f}"),
        ("Fees", lambda m: f"{m.fees:,.2f}"),
        ("Gas", lambda m: f"{m.gas:,.2f}"),
        ("Jev cost (est.)", lambda m: f"{m.est_jev_cost:,.4f}"),
        ("Fills", lambda m: f"{m.fills:,}"),
        ("Volume", lambda m: f"{m.volume:,.0f}"),
        ("Sharpe (ann.)", lambda m: f"{m.sharpe:.2f}"),
        ("Sortino (ann.)", lambda m: f"{m.sortino:.2f}"),
        ("Max drawdown", lambda m: f"{m.max_drawdown:,.2f}"),
        ("Hit rate", lambda m: f"{m.hit_rate:.1%}"),
        ("Spread capture bps", lambda m: f"{m.spread_capture_bps:.2f}"),
        ("Adverse selection bps", lambda m: f"{m.adverse_selection_bps:.2f}"),
        ("Markout bps", lambda m: f"{m.markout_bps:.2f}"),
        ("Coverage", lambda m: f"{m.coverage:.1%}"),
        ("Late / fallback blocks", lambda m: f"{m.ladder.get('late', 0)} / {m.ladder.get('fallback', 0)}"),
        ("Final position", lambda m: f"{m.final_position:g}"),
        ("Kill switch", lambda m: m.killed or "-"),
    ]
    header = ["", *(m.name for m in results)]
    table = [header, *([label, *(fmt(m) for m in results)] for label, fmt in rows)]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(w) if i == 0 else cell.rjust(w) for i, (cell, w) in enumerate(zip(row, widths))) for row in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    hours = results[0].hours if results else 0
    return f"{results[0].blocks if results else 0:,} blocks ({hours:.1f} h)\n" + "\n".join(lines)
