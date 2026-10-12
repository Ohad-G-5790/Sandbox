"""Avellaneda-Stoikov quoting: the deterministic half of market making.

    reservation r      = mid - q * gamma * sigma^2 * tau
    total spread       = gamma * sigma^2 * tau + (2 / gamma) * ln(1 + gamma / kappa)
    half spread (each) = gamma * sigma^2 * tau / 2 + (1 / gamma) * ln(1 + gamma / kappa)

(The article quotes the total-spread formula as the "half spread"; that doubles every quote's distance.)
q is inventory in base units, sigma is mid volatility in price units per sqrt(second), tau is the
horizon in seconds (a rolling horizon, since a 24/7 book has no close). The half spread is floored at
the break-even distance from `hftbot.costs`: quoting tighter than fees + gas + adverse selection loses
money on every fill, whatever the model says.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hftbot.features import MarketFeatures
from hftbot.market import Side
from hftbot.policy import PolicyAction


@dataclass(frozen=True)
class PricingConfig:
    gamma: float = 0.3
    """Risk aversion. Scales how hard inventory pushes the reservation price."""
    kappa: float = 50.0
    """Fill-intensity decay per unit of price distance (units: 1/price). Fit it from your venue's fills:
    with fills mostly within 0.02 of mid, kappa ~ 50. ln(1 + gamma/kappa)/gamma is the base half spread."""
    horizon_s: float = 30.0
    tick_size: float = 0.01
    lot_size: float = 1.0
    order_size: float = 10.0
    min_half_spread_bps: float = 2.0
    max_half_spread_bps: float = 100.0


@dataclass(frozen=True)
class Quote:
    side: Side
    price: float
    size: float


def reservation_price(mid: float, inventory: float, gamma: float, sigma: float, tau: float) -> float:
    return mid - inventory * gamma * sigma**2 * tau


def optimal_half_spread(gamma: float, sigma: float, tau: float, kappa: float) -> float:
    return gamma * sigma**2 * tau / 2 + math.log1p(gamma / kappa) / gamma


def make_quotes(features: MarketFeatures, inventory: float, action: PolicyAction, cfg: PricingConfig) -> list[Quote]:
    if not action.posts_quotes:
        return []
    mid, tick = features.mid, cfg.tick_size
    reservation = reservation_price(mid, inventory, cfg.gamma, features.sigma_price, cfg.horizon_s)
    half = optimal_half_spread(cfg.gamma, features.sigma_price, cfg.horizon_s, cfg.kappa)
    half = min(max(half, mid * cfg.min_half_spread_bps / 1e4), mid * cfg.max_half_spread_bps / 1e4) * action.spread_mult
    center = reservation - action.skew * half

    # Post-only: a quote that would cross the touch is rejected by the venue, so never send one.
    bid = min(round_down(center - half, tick), round_down(features.best_ask - tick, tick))
    ask = max(round_up(center + half, tick), round_up(features.best_bid + tick, tick))
    size = round_down(cfg.order_size * action.size_mult, cfg.lot_size)
    if size <= 0 or bid <= 0 or bid >= ask:
        return []
    return [Quote(side, bid if side is Side.BUY else ask, size) for side in (Side.BUY, Side.SELL) if side in action.sides]


def round_down(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 12)


def round_up(x: float, step: float) -> float:
    return round(math.ceil(x / step - 1e-9) * step, 12)
