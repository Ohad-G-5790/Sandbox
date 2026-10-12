"""Independent accounting from fills. Risk limits are checked against this, never against a model's claim."""

from __future__ import annotations

from dataclasses import dataclass

from hftbot.features import AccountState
from hftbot.market import Side

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class Fill:
    order_id: int
    side: Side
    price: float
    size: float
    fee: float
    """Paid in quote currency; negative for a maker rebate."""
    ts: float
    block: int
    maker: bool = True


class Portfolio:
    def __init__(self, cash: float) -> None:
        self.cash = cash
        self.position = 0.0
        self.avg_entry = 0.0
        self.realized_pnl = 0.0
        self.fees = 0.0
        self.gas = 0.0
        self.volume = 0.0
        self.fill_count = 0
        self.position_opened_ts: float | None = None
        self.mid: float | None = None
        self.initial_equity: float | None = None
        self.peak_equity = 0.0
        self._day: int | None = None
        self.day_start_equity = 0.0

    def apply_fill(self, fill: Fill) -> None:
        signed = fill.side.sign * fill.size
        self.cash -= signed * fill.price + fill.fee
        self.fees += fill.fee
        self.volume += fill.size * fill.price
        self.fill_count += 1

        before = self.position
        after = before + signed
        if before == 0 or (before > 0) == (signed > 0):
            # Opening or adding: average the entry price.
            self.avg_entry = (self.avg_entry * abs(before) + fill.price * fill.size) / abs(after)
            if before == 0:
                self.position_opened_ts = fill.ts
        else:
            closed = min(abs(signed), abs(before))
            self.realized_pnl += closed * (fill.price - self.avg_entry) * (1 if before > 0 else -1)
            if abs(signed) > abs(before):  # flipped through flat: the remainder opens a new position
                self.avg_entry = fill.price
                self.position_opened_ts = fill.ts
        self.position = 0.0 if abs(after) < 1e-12 else after
        if self.position == 0:
            self.avg_entry = 0.0
            self.position_opened_ts = None

    def charge_gas(self, amount: float) -> None:
        self.cash -= amount
        self.gas += amount

    def mark(self, mid: float, ts: float) -> None:
        self.mid = mid
        equity = self.equity
        if self.initial_equity is None:
            self.initial_equity = equity
            self.peak_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        day = int(ts // SECONDS_PER_DAY)
        if day != self._day:
            self._day = day
            self.day_start_equity = equity

    @property
    def equity(self) -> float:
        return self.cash + self.position * (self.mid or 0.0)

    @property
    def pnl(self) -> float:
        return self.equity - (self.initial_equity if self.initial_equity is not None else self.equity)

    @property
    def drawdown(self) -> float:
        return max(0.0, self.peak_equity - self.equity)

    @property
    def daily_pnl(self) -> float:
        return self.equity - self.day_start_equity

    @property
    def unrealized_pnl(self) -> float:
        return 0.0 if self.mid is None else self.position * (self.mid - self.avg_entry)

    def position_age_s(self, now: float) -> float:
        return 0.0 if self.position_opened_ts is None else max(0.0, now - self.position_opened_ts)

    def account_state(self, now: float) -> AccountState:
        return AccountState(
            inventory=self.position,
            unrealized_pnl=self.unrealized_pnl,
            drawdown_pct=self.drawdown / self.peak_equity * 100 if self.peak_equity > 0 else 0.0,
            position_age_s=self.position_age_s(now),
        )
