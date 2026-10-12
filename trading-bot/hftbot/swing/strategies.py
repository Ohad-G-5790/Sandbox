"""Strategies: target portfolio weights per coin per bar, using only closes up to and including that bar.

All are long-only spot (the rest sits in USDC) and well known, with small parameter grids. Lookbacks
are given in hours and converted to bars, so the same grid works on 1h or 4h bars.

Single-coin families give each coin an equal sleeve (1/N of the portfolio) and switch it between the
coin and USDC. `rotation` holds the strongest coins of the basket. `hold` is the benchmark.
"""

from __future__ import annotations

import itertools
import math
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from hftbot.swing.candles import Panel

Series = Sequence[float | None]


def _from_first_valid(x: Series, fn: Callable[[list[float]], list[float | None]]) -> list[float | None]:
    """Apply an indicator to the listed part of a series (None before the coin's first price)."""
    first = next((i for i, v in enumerate(x) if v is not None), len(x))
    return [None] * first + fn([float(v) for v in x[first:]])  # type: ignore[arg-type]


def sma(x: Series, n: int) -> list[float | None]:
    def calc(v: list[float]) -> list[float | None]:
        out: list[float | None] = []
        total = 0.0
        for i, value in enumerate(v):
            total += value
            if i >= n:
                total -= v[i - n]
            out.append(total / n if i >= n - 1 else None)
        return out

    return _from_first_valid(x, calc)


def prev_extreme(x: Series, n: int, highest: bool) -> list[float | None]:
    """Max (or min) of the previous n values, excluding the current one."""

    def calc(v: list[float]) -> list[float | None]:
        out: list[float | None] = []
        window: deque[int] = deque()
        for i, value in enumerate(v):
            out.append(v[window[0]] if i >= n else None)
            while window and (v[window[-1]] <= value if highest else v[window[-1]] >= value):
                window.pop()
            window.append(i)
            if window[0] <= i - n:
                window.popleft()
        return out

    return _from_first_valid(x, calc)


def rsi(x: Series, n: int = 14) -> list[float | None]:
    """Wilder's RSI."""

    def calc(v: list[float]) -> list[float | None]:
        out: list[float | None] = [None] * len(v)
        gain = loss = 0.0
        for i in range(1, len(v)):
            change = v[i] - v[i - 1]
            up, down = max(change, 0.0), max(-change, 0.0)
            if i <= n:
                gain += up / n
                loss += down / n
            else:
                gain = (gain * (n - 1) + up) / n
                loss = (loss * (n - 1) + down) / n
            if i >= n:
                out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
        return out

    return _from_first_valid(x, calc)


def realized_vol(x: Series, n: int, bars_per_year: float) -> list[float | None]:
    """Annualized standard deviation of log returns over the last n bars."""

    def calc(v: list[float]) -> list[float | None]:
        out: list[float | None] = [None] * len(v)
        rets: deque[float] = deque()
        s = s2 = 0.0
        for i in range(1, len(v)):
            r = math.log(v[i] / v[i - 1])
            rets.append(r)
            s, s2 = s + r, s2 + r * r
            if len(rets) > n:
                old = rets.popleft()
                s, s2 = s - old, s2 - old * old
            if len(rets) == n:
                var = max(s2 / n - (s / n) ** 2, 0.0)
                out[i] = math.sqrt(var * bars_per_year)
        return out

    return _from_first_valid(x, calc)


# --- Single-coin signals in [0, 1] -----------------------------------------------------------------


def signal_trend(close: Series, fast: int, slow: int, **_: Any) -> list[float]:
    f, s = sma(close, fast), sma(close, slow)
    return [1.0 if a is not None and b is not None and a > b else 0.0 for a, b in zip(f, s)]


def signal_breakout(close: Series, entry: int, exit: int, **_: Any) -> list[float]:
    """Donchian channel: in on a close above the previous `entry`-bar high, out below the `exit`-bar low."""
    hi, lo = prev_extreme(close, entry, True), prev_extreme(close, exit, False)
    out, inside = [], False
    for c, h, l in zip(close, hi, lo):
        if c is None:
            inside = False
        elif not inside and h is not None and c > h:
            inside = True
        elif inside and l is not None and c < l:
            inside = False
        out.append(1.0 if inside else 0.0)
    return out


def signal_momentum(close: Series, lookback: int, vol_target: float | None = None, vol_window: int = 42, bars_per_year: float = 2190.0, **_: Any) -> list[float]:
    """In while the return over `lookback` bars is positive; optionally scaled down when volatility is high."""
    vol = realized_vol(close, vol_window, bars_per_year) if vol_target else [None] * len(close)
    out = []
    for i, c in enumerate(close):
        past = close[i - lookback] if i >= lookback else None
        if c is None or past is None or c <= past:
            out.append(0.0)
        elif vol_target:
            v = vol[i]
            out.append(min(1.0, vol_target / v) if v else 0.0)
        else:
            out.append(1.0)
    return out


def signal_dip(close: Series, buy_below: float, sell_above: float, trend: int, rsi_n: int = 14, **_: Any) -> list[float]:
    """Buy oversold dips while above the long-term average; sell when the bounce is done or the trend breaks."""
    r, avg = rsi(close, rsi_n), sma(close, trend)
    out, inside = [], False
    for c, ri, a in zip(close, r, avg):
        if c is None or ri is None or a is None:
            inside = False
        elif not inside and ri < buy_below and c > a:
            inside = True
        elif inside and (ri > sell_above or c < a * 0.97):
            inside = False
        out.append(1.0 if inside else 0.0)
    return out


SIGNALS: dict[str, Callable[..., list[float]]] = {
    "trend": signal_trend,
    "breakout": signal_breakout,
    "momentum": signal_momentum,
    "dip": signal_dip,
}


@dataclass(frozen=True)
class Strategy:
    family: str
    params: dict[str, Any] = field(default_factory=dict)
    """Lookback-type parameters are in hours (keys ending in _h); the rest are unitless."""

    @property
    def label(self) -> str:
        return self.family + ("(" + ", ".join(f"{k}={v}" for k, v in self.params.items()) + ")" if self.params else "")

    def __hash__(self) -> int:
        return hash(self.label)

    def weights(self, panel: Panel) -> dict[str, list[float]]:
        """Target weight of each coin at each bar's close. Weights sum to at most 1; the rest is USDC."""
        coins = panel.coins
        n = len(panel)
        listed = {c: [v is not None for v in panel.close[c]] for c in coins}
        if self.family == "hold":
            return {c: [1 / len(coins) if listed[c][i] else 0.0 for i in range(n)] for c in coins}
        bars = self._bars(panel.bar_hours)
        if self.family == "rotation":
            return _rotation(panel, bars["lookback_h"], int(self.params["top_k"]), bars["rebalance_h"])
        signal = SIGNALS[self.family]
        bars_per_year = 8760 / panel.bar_hours
        return {c: [s / len(coins) for s in signal(panel.close[c], bars_per_year=bars_per_year, **self._signal_kwargs(bars))] for c in coins}

    def _bars(self, bar_hours: int) -> dict[str, int]:
        return {k: max(1, round(v / bar_hours)) for k, v in self.params.items() if k.endswith("_h")}

    def _signal_kwargs(self, bars: dict[str, int]) -> dict[str, Any]:
        kwargs = {k[:-2]: v for k, v in bars.items()}
        kwargs.update({k: v for k, v in self.params.items() if not k.endswith("_h")})
        return kwargs


def _rotation(panel: Panel, lookback: int, top_k: int, rebalance: int) -> dict[str, list[float]]:
    """Every `rebalance` bars, hold the `top_k` coins with the best positive return over `lookback` bars."""
    coins, n = panel.coins, len(panel)
    out = {c: [0.0] * n for c in coins}
    current: dict[str, float] = {}
    for i in range(n):
        if i % rebalance == 0:
            scores = []
            for c in coins:
                now, past = panel.close[c][i], panel.close[c][i - lookback] if i >= lookback else None
                if now is not None and past is not None and now > past:
                    scores.append((now / past, c))
            winners = [c for _, c in sorted(scores, reverse=True)[:top_k]]
            current = {c: 1 / top_k for c in winners}
        for c, w in current.items():
            if panel.close[c][i] is not None:
                out[c][i] = w
    return out


def _grid(family: str, **axes: Sequence[Any]) -> list[Strategy]:
    keys = list(axes)
    return [Strategy(family, dict(zip(keys, combo))) for combo in itertools.product(*axes.values())]


def candidate_grid() -> dict[str, list[Strategy]]:
    """Every configuration the walk-forward test may pick from. Kept small on purpose: the more
    combinations tried, the more likely the winner is luck."""
    breakout = [s for s in _grid("breakout", entry_h=(120, 240, 480), exit_h=(60, 120, 240)) if s.params["exit_h"] < s.params["entry_h"]]
    return {
        "trend": _grid("trend", fast_h=(24, 48, 96), slow_h=(168, 336, 720)),
        "breakout": breakout,
        "momentum": _grid("momentum", lookback_h=(168, 336, 720), vol_target=(None, 0.6)),
        "dip": _grid("dip", buy_below=(25, 30), sell_above=(50, 60), trend_h=(336, 720)),
        "rotation": _grid("rotation", lookback_h=(168, 336, 720), top_k=(1, 2), rebalance_h=(24, 168)),
    }
