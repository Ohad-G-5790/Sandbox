"""The gas honesty check: the half spread a quote needs just to break even. Run it before any live order.

Per block, a maker earns on the size that fills and pays gas on every requote whether anything fills
or not. Break-even, in bps of notional:

    half_spread >= maker_fee + adverse_selection + gas_per_block / (filled_notional_per_block) * 1e4

Gas is a fixed cost per transaction, so it shrinks with order size and requote interval. Spread is a
percentage. That is why a tiny bot requoting every 300 ms block bleeds: the fixed cost swamps the bps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BreakevenInputs:
    price: float
    order_size: float
    """Per side, in base units."""
    fill_rate: float
    """Expected fraction of one order's size filled per block (from paper trading or the backtester)."""
    gas_per_tx: float
    """In quote currency."""
    maker_fee_bps: float = 0.0
    adverse_selection_bps: float = 0.0
    """How far mid moves against a fill, on average (positive = against you). Measured, not guessed."""
    txs_per_requote: float = 1.0
    requote_every_blocks: float = 1.0
    block_interval_s: float = 0.3


@dataclass(frozen=True)
class Breakeven:
    half_spread_bps: float
    gas_per_block: float
    gas_per_hour: float
    gas_bps_per_fill: float
    """The gas share of the break-even, in bps of filled notional."""


def breakeven(i: BreakevenInputs) -> Breakeven:
    gas_per_block = i.gas_per_tx * i.txs_per_requote / i.requote_every_blocks
    filled_notional_per_block = i.fill_rate * i.order_size * i.price
    gas_bps = math.inf if filled_notional_per_block <= 0 else gas_per_block / filled_notional_per_block * 1e4
    return Breakeven(
        half_spread_bps=i.maker_fee_bps + i.adverse_selection_bps + gas_bps,
        gas_per_block=gas_per_block,
        gas_per_hour=gas_per_block * 3600 / i.block_interval_s,
        gas_bps_per_fill=gas_bps,
    )
