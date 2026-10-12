"""OHLCV candles: storage, resampling, and aligning several coins on one time index."""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

HOUR = 3600


@dataclass(frozen=True)
class Candle:
    ts: int
    """Open time, seconds since the epoch (UTC)."""
    open: float
    high: float
    low: float
    close: float
    volume: float


def save_csv(path: str | Path, candles: Iterable[Candle]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            writer.writerow([c.ts, repr(c.open), repr(c.high), repr(c.low), repr(c.close), repr(c.volume)])


def load_csv(path: str | Path) -> list[Candle]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [Candle(int(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"])) for r in rows]


def merge(old: Sequence[Candle], new: Iterable[Candle]) -> list[Candle]:
    """Union by timestamp, newer data winning, sorted."""
    by_ts = {c.ts: c for c in old}
    by_ts.update({c.ts: c for c in new})
    return [by_ts[ts] for ts in sorted(by_ts)]


def resample(candles: Sequence[Candle], hours: int, now: float | None = None) -> list[Candle]:
    """Hourly candles into `hours`-hour bars aligned to UTC midnight. A bar still in progress at `now`
    (or after the last hourly candle) is dropped: signals only ever use finished bars."""
    if hours == 1:
        bars = list(candles)
    else:
        width = hours * HOUR
        groups: dict[int, list[Candle]] = {}
        for c in candles:
            groups.setdefault(c.ts - c.ts % width, []).append(c)
        bars = [
            Candle(start, g[0].open, max(c.high for c in g), min(c.low for c in g), g[-1].close, sum(c.volume for c in g))
            for start, g in ((s, sorted(groups[s], key=lambda c: c.ts)) for s in sorted(groups))
        ]
    if not bars:
        return []
    end_of_data = (candles[-1].ts + HOUR) if now is None else now
    return [b for b in bars if b.ts + hours * HOUR <= end_of_data]


@dataclass(frozen=True)
class Panel:
    """Several coins on one time index. Values are None before a coin's first bar (not listed yet);
    gaps after that are forward-filled from the last close."""

    ts: list[int]
    open: dict[str, list[float | None]]
    close: dict[str, list[float | None]]
    bar_hours: int

    @property
    def coins(self) -> list[str]:
        return list(self.close)

    def __len__(self) -> int:
        return len(self.ts)


def align(series: Mapping[str, Sequence[Candle]], bar_hours: int) -> Panel:
    index = sorted({c.ts for candles in series.values() for c in candles})
    opens: dict[str, list[float | None]] = {}
    closes: dict[str, list[float | None]] = {}
    for coin, candles in series.items():
        by_ts = {c.ts: c for c in candles}
        o: list[float | None] = []
        cl: list[float | None] = []
        last: float | None = None
        for ts in index:
            c = by_ts.get(ts)
            if c is not None and all(math.isfinite(x) and x > 0 for x in (c.open, c.close)):
                o.append(c.open)
                cl.append(c.close)
                last = c.close
            else:
                o.append(last)
                cl.append(last)
        opens[coin], closes[coin] = o, cl
    return Panel(index, opens, closes, bar_hours)
