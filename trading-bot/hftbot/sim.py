"""A synthetic, regime-switching limit order book market for testing the machinery end to end.

It exists to exercise every code path (toxic flow, liquidity crunches, regime changes) with known
ground truth. A strategy that wins here has proven nothing about a real venue: get real data.

Informed takers trade in the direction of the move over the next `informed_horizon_blocks`, so
their flow is genuinely toxic for a maker that they fill, and flow carries real predictive signal.
Every print also has permanent price impact (more in thin books), so the average fill is followed
by a move against the maker, as on real venues.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from hftbot.market import Block, Book, Level, Side, Trade


@dataclass(frozen=True)
class Regime:
    name: str
    vol_mult: float
    drift_bps_per_s: float
    informed_share: float
    depth_mult: float
    spread_mult: float
    trade_rate_mult: float
    weight: float
    """Probability of entering this regime on a switch."""


REGIMES = (
    Regime("mean_reverting", 1.0, 0.0, 0.10, 1.0, 1.0, 1.0, 0.50),
    Regime("trending", 1.2, 0.4, 0.45, 0.9, 1.0, 1.2, 0.30),
    Regime("high_vol", 3.0, 0.0, 0.20, 0.6, 2.0, 1.5, 0.15),
    Regime("crisis", 6.0, 1.5, 0.60, 0.25, 4.0, 2.0, 0.05),
)


@dataclass(frozen=True)
class SimConfig:
    blocks: int = 20_000
    seed: int = 7
    block_interval_s: float = 0.3
    start_ts: float = 1_760_000_000.0
    start_price: float = 100.0
    tick: float = 0.01
    base_spread_ticks: int = 4
    levels: int = 5
    base_depth: float = 40.0
    vol_per_sqrt_s: float = 1.0e-4
    """Log-price volatility per sqrt(second) in the calm regime (~56% annualized)."""
    mean_reversion_per_s: float = 0.002
    trades_per_block: float = 0.8
    mean_trade_size: float = 6.0
    informed_horizon_blocks: int = 20
    impact_bps_per_unit: float = 0.05
    """Permanent log-price impact of each unit a taker trades, divided by the regime's depth multiplier."""
    mean_regime_blocks: float = 3000.0


@dataclass(frozen=True)
class SimResult:
    blocks: list[Block]
    regimes: list[str]


def simulate(cfg: SimConfig | None = None) -> SimResult:
    cfg = cfg or SimConfig()
    rng = random.Random(cfg.seed)
    dt = cfg.block_interval_s
    n = cfg.blocks + cfg.informed_horizon_blocks + 1

    # 1. The fundamental log-price path and its regimes, generated first so informed flow can look ahead.
    regime = REGIMES[0]
    drift_sign = 1.0
    anchor = log_price = math.log(cfg.start_price)
    path: list[float] = []
    regimes: list[Regime] = []
    for _ in range(n):
        if rng.random() < 1 / cfg.mean_regime_blocks:
            regime = rng.choices(REGIMES, weights=[r.weight for r in REGIMES])[0]
            drift_sign = rng.choice((-1.0, 1.0))
            anchor = log_price
        drift = drift_sign * regime.drift_bps_per_s / 1e4
        pull = -cfg.mean_reversion_per_s * (log_price - anchor) if regime.name == "mean_reverting" else 0.0
        log_price += (drift + pull) * dt + regime.vol_mult * cfg.vol_per_sqrt_s * math.sqrt(dt) * rng.gauss(0, 1)
        path.append(log_price)
        regimes.append(regime)

    # 2. Books and prints around that path.
    blocks: list[Block] = []
    impact = 0.0  # cumulative permanent impact of all prints so far, in log price
    for i in range(cfg.blocks):
        regime, ts = regimes[i], cfg.start_ts + i * dt
        price = math.exp(path[i] + impact)
        future = path[i + cfg.informed_horizon_blocks] - path[i]
        informed_side = Side.BUY if future > 0 else Side.SELL

        spread_ticks = max(1, round(cfg.base_spread_ticks * regime.spread_mult * rng.uniform(0.75, 1.25)))
        bid_ticks = math.floor(price / cfg.tick - spread_ticks / 2)
        lean = 0.25 if regime.informed_share > 0.3 else 0.0  # informed pressure shows up as book imbalance
        bid_mult = 1 + (lean if informed_side is Side.BUY else -lean)
        ask_mult = 2 - bid_mult
        bids = tuple(
            Level(round((bid_ticks - k) * cfg.tick, 10), round(cfg.base_depth * regime.depth_mult * bid_mult * rng.uniform(0.5, 1.5) * (1 + 0.3 * k), 2))
            for k in range(cfg.levels)
        )
        asks = tuple(
            Level(round((bid_ticks + spread_ticks + k) * cfg.tick, 10), round(cfg.base_depth * regime.depth_mult * ask_mult * rng.uniform(0.5, 1.5) * (1 + 0.3 * k), 2))
            for k in range(cfg.levels)
        )
        book = Book(ts, bids, asks)

        trades = []
        for _ in range(_poisson(rng, cfg.trades_per_block * regime.trade_rate_mult)):
            side = informed_side if rng.random() < regime.informed_share else rng.choice((Side.BUY, Side.SELL))
            size = round(rng.expovariate(1 / cfg.mean_trade_size), 2) or 0.01
            levels = asks if side is Side.BUY else bids
            # A taker bigger than the touch sweeps into the next level and prints there.
            print_level = levels[1] if size > levels[0].size and len(levels) > 1 else levels[0]
            trades.append(Trade(ts - dt * rng.random(), print_level.price, size, side))
            impact += side.sign * size * cfg.impact_bps_per_unit / 1e4 / regime.depth_mult
        trades.sort(key=lambda t: t.ts)
        blocks.append(Block(i, ts, book, tuple(trades)))
    return SimResult(blocks, [r.name for r in regimes[: cfg.blocks]])


def _poisson(rng: random.Random, lam: float) -> int:
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1
