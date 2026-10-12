"""The per-block loop and its fallback ladder.

    block -> fills -> features -> account kill checks -> snapshot -> judgments -> policy
          -> A-S quotes -> risk veto -> requote (only if the quotes changed) -> record

Fallback ladder:
    healthy + confident        -> normal operation
    healthy + low confidence   -> reduced size (policy)
    decision late for block    -> cancel resting quotes, post nothing: never quote on stale state
    primary engine unavailable -> deterministic fallback engine, wider and smaller (degraded)
    stale market data          -> cancel resting quotes, post nothing
    hard limit breached        -> kill switch: cancel, flatten, alert, stay down
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from hftbot.execution import RestingOrder, TxReceipt, Venue
from hftbot.features import HealthState, StateEngine, snapshot
from hftbot.judgments import Decision, DecisionEngine, DecisionTimeout, DecisionUnavailable
from hftbot.market import Block, Side
from hftbot.policy import PolicyAction, PolicyConfig, compose_action
from hftbot.portfolio import Fill, Portfolio
from hftbot.pricing import PricingConfig, Quote, make_quotes
from hftbot.risk import RiskEngine

log = logging.getLogger("hftbot")


class Ladder(str, Enum):
    NORMAL = "normal"
    LOW_CONFIDENCE = "low_confidence"
    LATE = "late"
    FALLBACK = "fallback"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    KILLED = "killed"


@dataclass(frozen=True)
class LoopConfig:
    block_interval_s: float = 0.3
    deadline_fraction: float = 0.8
    """The decision must land within this fraction of a block, leaving the rest for the order to go out."""
    requote_ticks: int = 0
    """Keep resting quotes when every target is within this many ticks of them: each requote costs gas."""


@dataclass(frozen=True)
class StepRecord:
    block: int
    ts: float
    mid: float
    ladder: Ladder
    snapshot: dict[str, Any] | None
    decision: Decision | None
    action: PolicyAction | None
    resting: tuple[Quote, ...]
    fills: tuple[Fill, ...]
    vetoed: tuple[tuple[Quote, str], ...]
    gas: float
    position: float
    equity: float
    note: str = ""


@dataclass
class _Health:
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=10))
    outcomes: deque[bool] = field(default_factory=lambda: deque(maxlen=100))  # True = late

    def state(self, venue: Venue) -> HealthState:
        s = venue.stats
        return HealthState(
            fill_ratio=s.orders_filled / s.orders_posted if s.orders_posted else 0.0,
            reject_rate=s.orders_rejected / s.orders_posted if s.orders_posted else 0.0,
            latency_p50_ms=statistics.median(self.latencies) if self.latencies else 0.0,
            latency_max_ms=max(self.latencies, default=0.0),
            late_ratio=sum(self.outcomes) / len(self.outcomes) if self.outcomes else 0.0,
        )


class TradingLoop:
    def __init__(
        self,
        *,
        engine: DecisionEngine,
        venue: Venue,
        portfolio: Portfolio,
        risk: RiskEngine,
        state: StateEngine | None = None,
        fallback: DecisionEngine | None = None,
        policy: PolicyConfig | None = None,
        pricing: PricingConfig | None = None,
        loop: LoopConfig | None = None,
        on_alert: Callable[[str], None] | None = None,
    ) -> None:
        self.engine = engine
        self.fallback = fallback
        self.venue = venue
        self.portfolio = portfolio
        self.risk = risk
        self.state = state or StateEngine()
        self.policy = policy or PolicyConfig()
        self.pricing = pricing or PricingConfig()
        self.config = loop or LoopConfig()
        self.on_alert = on_alert or (lambda message: log.error("ALERT: %s", message))
        self._health = _Health()

    @property
    def deadline_s(self) -> float:
        budget_ms = self.config.block_interval_s * 1000 * self.config.deadline_fraction
        return min(budget_ms, self.risk.limits.max_decision_latency_ms) / 1000

    def step(self, block: Block) -> StepRecord:
        fills = list(self.venue.match(block))
        for fill in fills:
            self.portfolio.apply_fill(fill)
        features = self.state.update(block)
        self.portfolio.mark(features.mid, block.ts)
        gas = 0.0

        if self.risk.killed:
            return self._record(block, Ladder.KILLED, fills, gas, note=self.risk.kill_reason or "")
        kill_reason = self.risk.check_account(self.portfolio, block.ts)
        if kill_reason:
            gas, flatten_fills = self._kill(block, kill_reason)
            return self._record(block, Ladder.KILLED, fills + flatten_fills, gas, note=kill_reason)
        if self.risk.is_stale(block.book.ts, block.ts):
            gas = self._apply(self.venue.cancel_all(block))
            return self._record(block, Ladder.STALE, fills, gas, note=f"book is {block.ts - block.book.ts:.1f}s old")

        snap = snapshot(features, self.portfolio.account_state(block.ts), self._health.state(self.venue))
        decision, ladder, note = self._decide(snap)
        if decision is None:
            gas = self._apply(self.venue.cancel_all(block))
            return self._record(block, ladder, fills, gas, snap=snap, note=note)

        action = compose_action(decision, self.portfolio.position, self.policy, degraded=ladder is Ladder.FALLBACK)
        if ladder is Ladder.NORMAL and action.low_confidence:
            ladder = Ladder.LOW_CONFIDENCE
        verdict = self.risk.check_quotes(make_quotes(features, self.portfolio.position, action, self.pricing), self.portfolio, block.ts)
        targets = list(verdict.approved)
        if not self._close_enough(targets, self.venue.open_orders()):
            gas = self._apply(self.venue.replace_quotes(targets, block) if targets else self.venue.cancel_all(block))
        return self._record(block, ladder, fills, gas, snap=snap, decision=decision, action=action, vetoed=verdict.vetoed, note=note)

    def _decide(self, snap: dict[str, Any]) -> tuple[Decision | None, Ladder, str]:
        deadline = self.deadline_s
        try:
            decision = self.engine.decide(snap, deadline)
        except DecisionTimeout as error:
            self._health.outcomes.append(True)
            return None, Ladder.LATE, str(error)
        except DecisionUnavailable as error:
            if self.fallback is None:
                return None, Ladder.UNAVAILABLE, str(error)
            log.warning("primary engine unavailable, using fallback: %s", error)
            return self.fallback.decide(snap, deadline), Ladder.FALLBACK, str(error)
        self._health.latencies.append(decision.latency_ms)
        late = decision.latency_ms > deadline * 1000
        self._health.outcomes.append(late)
        if late:
            return None, Ladder.LATE, f"decision took {decision.latency_ms:.0f} ms > {deadline * 1000:.0f} ms deadline"
        return decision, Ladder.NORMAL, ""

    def _kill(self, block: Block, reason: str) -> tuple[float, list[Fill]]:
        self.on_alert(f"kill switch at block {block.number}: {reason}")
        gas = self._apply(self.venue.cancel_all(block))
        fills: list[Fill] = []
        if self.portfolio.position:
            side = Side.SELL if self.portfolio.position > 0 else Side.BUY
            receipt = self.venue.market_order(side, abs(self.portfolio.position), block)
            gas += self._apply(receipt)
            fills = list(receipt.fills)
            if self.portfolio.position:
                self.on_alert(f"flatten incomplete: {self.portfolio.position} still open (book too thin)")
        return gas, fills

    def _apply(self, receipt: TxReceipt) -> float:
        for fill in receipt.fills:
            self.portfolio.apply_fill(fill)
        if receipt.gas:
            self.portfolio.charge_gas(receipt.gas)
        if receipt.error:
            self.risk.record_api_error(self._now())
        return receipt.gas

    def _now(self) -> float:
        return self.state.features.ts if self.state.features else 0.0

    def _close_enough(self, targets: list[Quote], resting: list[RestingOrder]) -> bool:
        """True when the resting orders already are the targets (within `requote_ticks`), so no tx is needed."""
        if len(targets) != len(resting):
            return False
        tolerance = self.config.requote_ticks * self.pricing.tick_size + 1e-12
        by_side = {o.side: o for o in resting}
        for quote in targets:
            order = by_side.get(quote.side)
            if order is None or abs(order.price - quote.price) > tolerance or abs(order.remaining - quote.size) > 1e-12:
                return False
        return True

    def _record(
        self,
        block: Block,
        ladder: Ladder,
        fills: list[Fill],
        gas: float,
        *,
        snap: dict[str, Any] | None = None,
        decision: Decision | None = None,
        action: PolicyAction | None = None,
        vetoed: tuple[tuple[Quote, str], ...] = (),
        note: str = "",
    ) -> StepRecord:
        resting = tuple(Quote(o.side, o.price, o.remaining) for o in self.venue.open_orders())
        return StepRecord(
            block=block.number,
            ts=block.ts,
            mid=block.book.mid,
            ladder=ladder,
            snapshot=snap,
            decision=decision,
            action=action,
            resting=resting,
            fills=tuple(fills),
            vetoed=vetoed,
            gas=gas,
            position=self.portfolio.position,
            equity=self.portfolio.equity,
            note=note,
        )
