"""Running the loop against a live feed, in real time, until stopped.

Dry run (the default) pairs a live feed with the paper venue: real books and real prints, simulated
fills. Live mode swaps in a real venue. Either way, on exit every resting order is cancelled.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from hftbot.loop import StepRecord, TradingLoop
from hftbot.market import Block

log = logging.getLogger("hftbot")


class Feed(Protocol):
    def next_block(self) -> Block:
        """Block until the next block of market data is available and return it."""
        ...


class FeedError(Exception):
    """The feed could not produce a block (RPC failure, unparseable data)."""


def run_live(
    feed: Feed,
    loop: TradingLoop,
    *,
    on_step: Callable[[StepRecord], None] | None = None,
    max_blocks: int | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> int:
    """Step the loop on every block from `feed`. Returns the number of blocks processed."""
    processed = 0
    last_block: Block | None = None
    try:
        while not should_stop() and (max_blocks is None or processed < max_blocks):
            try:
                block = feed.next_block()
            except FeedError as error:
                log.warning("feed error: %s", error)
                loop.risk.record_api_error(time.time())
                continue
            last_block = block
            record = loop.step(block)
            processed += 1
            if on_step:
                on_step(record)
            if record.ladder.value == "killed" and loop.portfolio.position == 0:
                log.error("kill switch tripped and position is flat: stopping (%s)", record.note)
                break
    finally:
        if last_block is not None:
            try:
                receipt = loop.venue.cancel_all(last_block)
                if receipt.gas:
                    loop.portfolio.charge_gas(receipt.gas)
            except Exception:  # noqa: BLE001 - shutdown must not mask the original error
                log.exception("could not cancel resting orders on shutdown: CHECK THE VENUE MANUALLY")
    return processed
