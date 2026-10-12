"""Hard risk limits. Deterministic, checked before every order, never delegated to a model.

Account limits (daily loss, drawdown, API errors) trip a latched kill switch: cancel, flatten, alert,
and stay down until a human calls `reset()`. Order limits veto individual quotes. A quote that only
reduces exposure is never vetoed by an exposure limit, so the bot can always work its way back.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from hftbot.portfolio import Portfolio
from hftbot.pricing import Quote


@dataclass(frozen=True)
class RiskLimits:
    max_position: float = 100.0
    max_order_size: float = 20.0
    max_daily_loss: float = 50.0
    max_drawdown: float = 100.0
    max_inventory_age_s: float = 900.0
    max_stale_data_s: float = 2.0
    max_leverage: float = 3.0
    max_api_errors: int = 5
    api_error_window_s: float = 60.0
    max_decision_latency_ms: float = 250.0


@dataclass(frozen=True)
class RiskVerdict:
    approved: tuple[Quote, ...]
    vetoed: tuple[tuple[Quote, str], ...] = ()


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.killed = False
        self.kill_reason: str | None = None
        self._errors: deque[float] = deque()

    def record_api_error(self, ts: float) -> None:
        self._errors.append(ts)

    def api_errors(self, now: float) -> int:
        while self._errors and now - self._errors[0] > self.limits.api_error_window_s:
            self._errors.popleft()
        return len(self._errors)

    def check_account(self, portfolio: Portfolio, now: float) -> str | None:
        """Returns the kill reason if an account limit is breached (and latches the kill switch)."""
        if self.killed:
            return self.kill_reason
        limits = self.limits
        reason = None
        if portfolio.daily_pnl < -limits.max_daily_loss:
            reason = f"daily loss {-portfolio.daily_pnl:.2f} > {limits.max_daily_loss}"
        elif portfolio.drawdown > limits.max_drawdown:
            reason = f"drawdown {portfolio.drawdown:.2f} > {limits.max_drawdown}"
        elif self.api_errors(now) > limits.max_api_errors:
            reason = f"{self.api_errors(now)} API errors in {limits.api_error_window_s:.0f}s > {limits.max_api_errors}"
        if reason:
            self.killed, self.kill_reason = True, reason
        return reason

    def is_stale(self, data_ts: float, now: float) -> bool:
        return now - data_ts > self.limits.max_stale_data_s

    def check_quotes(self, quotes: list[Quote], portfolio: Portfolio, now: float) -> RiskVerdict:
        if self.killed:
            return RiskVerdict((), tuple((q, "kill switch") for q in quotes))
        limits, position, mid, equity = self.limits, portfolio.position, portfolio.mid or 0.0, portfolio.equity
        approved: list[Quote] = []
        vetoed: list[tuple[Quote, str]] = []
        for quote in quotes:
            after = position + quote.side.sign * quote.size
            grows = abs(after) > abs(position)
            if quote.size > limits.max_order_size:
                vetoed.append((quote, f"order size {quote.size} > {limits.max_order_size}"))
            elif grows and abs(after) > limits.max_position:
                vetoed.append((quote, f"position {after} would exceed {limits.max_position}"))
            elif grows and (equity <= 0 or abs(after) * mid / equity > limits.max_leverage):
                vetoed.append((quote, f"leverage would exceed {limits.max_leverage}"))
            elif grows and portfolio.position_age_s(now) > limits.max_inventory_age_s:
                vetoed.append((quote, f"inventory older than {limits.max_inventory_age_s:.0f}s: reduce only"))
            else:
                approved.append(quote)
        return RiskVerdict(tuple(approved), tuple(vetoed))

    def reset(self) -> None:
        """Manual re-arm after a human has looked at why the kill switch tripped."""
        self.killed, self.kill_reason = False, None
        self._errors.clear()
