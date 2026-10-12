"""The state engine: deterministic features computed every block, and the compact snapshot sent to Jev.

Everything computable lives here, never in a model call. Every feature at block N uses only data
observed at or before block N, so a decision can never see the future it is judged against.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from hftbot.market import Block, Side


class TimeWindow:
    """Values pushed over the last `span_s` seconds, with a running total."""

    def __init__(self, span_s: float) -> None:
        self.span_s = span_s
        self._items: deque[tuple[float, float]] = deque()
        self.total = 0.0

    def push(self, ts: float, value: float) -> None:
        self._items.append((ts, value))
        self.total += value
        self.advance(ts)

    def advance(self, now: float) -> None:
        while self._items and now - self._items[0][0] > self.span_s:
            self.total -= self._items.popleft()[1]
        if not self._items:
            self.total = 0.0  # drop accumulated float error whenever the window empties

    @property
    def oldest(self) -> tuple[float, float] | None:
        return self._items[0] if self._items else None

    def __len__(self) -> int:
        return len(self._items)


class Ewma:
    """Exponentially weighted mean with a time-based half-life, for slow baselines like "the 24h norm"."""

    def __init__(self, half_life_s: float) -> None:
        self.half_life_s = half_life_s
        self.value: float | None = None
        self._last_ts: float | None = None

    def update(self, ts: float, x: float) -> float:
        if self.value is None or self._last_ts is None:
            self.value = x
        else:
            alpha = 1 - 0.5 ** (max(ts - self._last_ts, 0.0) / self.half_life_s)
            self.value += alpha * (x - self.value)
        self._last_ts = ts
        return self.value


@dataclass(frozen=True)
class FeatureConfig:
    return_windows_s: tuple[float, float, float] = (60.0, 300.0, 1800.0)
    flow_window_s: float = 60.0
    rv_short_s: float = 300.0
    rv_medium_s: float = 1800.0
    baseline_half_life_s: float = 86_400.0
    depth_levels: int = 3


@dataclass(frozen=True)
class MarketFeatures:
    """Unrounded features. Pricing and risk use these; Jev sees the rounded snapshot."""

    ts: float
    mid: float
    microprice: float
    best_bid: float
    best_ask: float
    spread_bps: float
    ret_bps: tuple[float, float, float]
    depth_bid: float
    depth_ask: float
    imbalance: float
    buy_volume: float
    sell_volume: float
    aggr_buy_ratio: float
    trades_per_s: float
    rv_short_bps: float
    rv_medium_bps: float
    vol_vs_baseline: float
    sigma_price: float
    """Volatility of mid in price units per sqrt(second): the sigma in Avellaneda-Stoikov."""
    depth_vs_baseline: float
    ref_divergence_bps: float | None


@dataclass(frozen=True)
class AccountState:
    inventory: float = 0.0
    unrealized_pnl: float = 0.0
    drawdown_pct: float = 0.0
    position_age_s: float = 0.0


@dataclass(frozen=True)
class HealthState:
    fill_ratio: float = 0.0
    reject_rate: float = 0.0
    latency_p50_ms: float = 0.0
    latency_max_ms: float = 0.0
    late_ratio: float = 0.0


class StateEngine:
    def __init__(self, config: FeatureConfig | None = None) -> None:
        self.config = config or FeatureConfig()
        c = self.config
        self._mids = [TimeWindow(span) for span in c.return_windows_s]
        self._buy_volume = TimeWindow(c.flow_window_s)
        self._sell_volume = TimeWindow(c.flow_window_s)
        self._trade_count = TimeWindow(c.flow_window_s)
        self._rv_short = TimeWindow(c.rv_short_s)
        self._rv_medium = TimeWindow(c.rv_medium_s)
        self._dmid_sq = TimeWindow(c.rv_short_s)
        self._dt = TimeWindow(c.rv_short_s)
        self._var_rate_baseline = Ewma(c.baseline_half_life_s)
        self._depth_baseline = Ewma(c.baseline_half_life_s)
        self._prev: tuple[float, float] | None = None  # (ts, mid) of the previous block
        self._first_ts: float | None = None
        self.features: MarketFeatures | None = None

    def update(self, block: Block) -> MarketFeatures:
        c, book, ts = self.config, block.book, block.ts
        mid = book.mid
        if self._first_ts is None:
            self._first_ts = ts

        for window in self._mids:
            window.push(ts, mid)

        for trade in block.trades:
            volume = self._buy_volume if trade.aggressor is Side.BUY else self._sell_volume
            volume.push(trade.ts, trade.size)
            self._trade_count.push(trade.ts, 1.0)
        for window in (self._buy_volume, self._sell_volume, self._trade_count):
            window.advance(ts)

        if self._prev is not None and ts > self._prev[0]:
            prev_ts, prev_mid = self._prev
            dt = ts - prev_ts
            log_ret = math.log(mid / prev_mid)
            self._rv_short.push(ts, log_ret**2)
            self._rv_medium.push(ts, log_ret**2)
            self._dmid_sq.push(ts, (mid - prev_mid) ** 2)
            self._dt.push(ts, dt)
            self._var_rate_baseline.update(ts, log_ret**2 / dt)
        self._prev = (ts, mid)

        depth_bid, depth_ask = book.depth(Side.BUY, c.depth_levels), book.depth(Side.SELL, c.depth_levels)
        depth_baseline = self._depth_baseline.update(ts, depth_bid + depth_ask)

        buy, sell = self._buy_volume.total, self._sell_volume.total
        covered = max(min(c.flow_window_s, ts - self._first_ts), 1e-9)
        short_seconds = self._dt.total
        short_var_rate = self._rv_short.total / short_seconds if short_seconds > 0 else 0.0
        baseline_var_rate = self._var_rate_baseline.value or 0.0

        self.features = MarketFeatures(
            ts=ts,
            mid=mid,
            microprice=book.microprice,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            spread_bps=book.spread_bps,
            ret_bps=tuple(_ret_bps(window, mid) for window in self._mids),  # type: ignore[arg-type]
            depth_bid=depth_bid,
            depth_ask=depth_ask,
            imbalance=book.imbalance(c.depth_levels),
            buy_volume=buy,
            sell_volume=sell,
            aggr_buy_ratio=buy / (buy + sell) if buy + sell > 0 else 0.5,
            trades_per_s=self._trade_count.total / covered if ts > self._first_ts else 0.0,
            rv_short_bps=math.sqrt(max(self._rv_short.total, 0.0)) * 1e4,
            rv_medium_bps=math.sqrt(max(self._rv_medium.total, 0.0)) * 1e4,
            vol_vs_baseline=math.sqrt(short_var_rate / baseline_var_rate) if baseline_var_rate > 0 else 1.0,
            sigma_price=math.sqrt(max(self._dmid_sq.total, 0.0) / short_seconds) if short_seconds > 0 else 0.0,
            depth_vs_baseline=(depth_bid + depth_ask) / depth_baseline if depth_baseline > 0 else 1.0,
            ref_divergence_bps=(mid / block.reference_mid - 1) * 1e4 if block.reference_mid else None,
        )
        return self.features


def _ret_bps(window: TimeWindow, mid: float) -> float:
    oldest = window.oldest
    return 0.0 if oldest is None else math.log(mid / oldest[1]) * 1e4


def snapshot(f: MarketFeatures, account: AccountState, health: HealthState) -> dict[str, Any]:
    """The compact, numeric state Jev sees. Keep it dense: input tokens are the whole cost."""
    state: dict[str, Any] = {
        "price": {
            "mid": _sig(f.mid, 7),
            "micro_vs_mid_bps": _sig((f.microprice / f.mid - 1) * 1e4),
            "ret_bps": {"1m": _sig(f.ret_bps[0]), "5m": _sig(f.ret_bps[1]), "30m": _sig(f.ret_bps[2])},
        },
        "book": {
            "spread_bps": _sig(f.spread_bps),
            "depth3": {"bid": _sig(f.depth_bid), "ask": _sig(f.depth_ask)},
            "imbalance": _sig(f.imbalance, 3),
            "depth_vs_24h": _sig(f.depth_vs_baseline, 3),
        },
        "flow_1m": {
            "taker_buy": _sig(f.buy_volume),
            "taker_sell": _sig(f.sell_volume),
            "buy_ratio": _sig(f.aggr_buy_ratio, 3),
            "trades_per_s": _sig(f.trades_per_s, 3),
        },
        "vol": {"rv_5m_bps": _sig(f.rv_short_bps), "rv_30m_bps": _sig(f.rv_medium_bps), "vs_24h": _sig(f.vol_vs_baseline, 3)},
        "position": {
            "inventory": _sig(account.inventory),
            "upnl": _sig(account.unrealized_pnl),
            "drawdown_pct": _sig(account.drawdown_pct, 3),
            "age_s": round(account.position_age_s),
        },
        "health": {
            "fill_ratio": _sig(health.fill_ratio, 3),
            "reject_rate": _sig(health.reject_rate, 3),
            "latency_ms": {"p50": round(health.latency_p50_ms), "max": round(health.latency_max_ms)},
            "late_ratio": _sig(health.late_ratio, 3),
        },
    }
    if f.ref_divergence_bps is not None:
        state["cross"] = {"ref_divergence_bps": _sig(f.ref_divergence_bps)}
    return state


def estimate_tokens(state: dict[str, Any]) -> int:
    """Rough input-token count (~4 characters per token for compact JSON)."""
    return math.ceil(len(json.dumps(state, separators=(",", ":"))) / 4)


def _sig(x: float, digits: int = 4) -> float:
    """Round to significant digits so the snapshot stays short without losing scale."""
    if x == 0 or not math.isfinite(x):
        return 0.0
    return round(x, digits - 1 - int(math.floor(math.log10(abs(x)))))

