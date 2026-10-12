"""Made-up hourly prices for exercising the machinery: correlated coins, bull/bear/flat regimes, a late
listing. Results on it say nothing about real markets."""

from __future__ import annotations

import math
import random

from hftbot.swing.candles import HOUR, Candle

REGIMES = ((1.2, 0.6), (-0.6, 0.8), (0.0, 0.5))  # (annual drift, annual volatility of the market factor)


def synthetic_hourly(coins: tuple[str, ...] = ("SOL", "BTC", "ETH", "JUP"), years: float = 4.0, seed: int = 1, start_ts: int = 1_609_459_200) -> dict[str, list[Candle]]:
    rng = random.Random(seed)
    n = int(years * 8760)
    betas = {c: 0.6 + 0.8 * rng.random() for c in coins}
    idio = {c: 0.3 + 0.5 * rng.random() for c in coins}
    listed = {c: (int(n * 0.4) if i == len(coins) - 1 and len(coins) > 1 else 0) for i, c in enumerate(coins)}
    price = {c: 10.0 + 90 * rng.random() for c in coins}
    out: dict[str, list[Candle]] = {c: [] for c in coins}
    regime, remaining = rng.choice(REGIMES), 0
    for t in range(n):
        if remaining <= 0:
            regime, remaining = rng.choice(REGIMES), int(rng.uniform(0.25, 0.75) * 8760)
        remaining -= 1
        drift, vol = regime
        market = drift / 8760 + vol / math.sqrt(8760) * rng.gauss(0, 1)
        for c in coins:
            r = betas[c] * market + idio[c] / math.sqrt(8760) * rng.gauss(0, 1)
            o = price[c]
            price[c] = o * math.exp(r)
            if t >= listed[c]:
                hi = max(o, price[c]) * (1 + abs(rng.gauss(0, 0.002)))
                lo = min(o, price[c]) * (1 - abs(rng.gauss(0, 0.002)))
                out[c].append(Candle(start_ts + t * HOUR, o, hi, lo, price[c], 1000 * rng.random()))
    return out
