"""Venues. `PaperVenue` simulates a post-only limit order book venue against real (or simulated) prints.

Fill model, deliberately conservative:
- An order posted while handling block N is live from block N + `activation_delay_blocks`.
- Joining an existing price level puts the order behind that level's resting size. Prints at our price
  eat that queue first; the queue also shrinks if later snapshots show less size there (cancels).
- A print strictly through our price fills us up to the print's size.
- Our orders do not move the market (no impact): a known optimism of every replay backtest.
- Every transaction pays gas, including rejected posts: chains like Monad charge on the gas limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from hftbot.market import Block, Side, same_price
from hftbot.portfolio import Fill
from hftbot.pricing import Quote


@dataclass(frozen=True)
class CostModel:
    maker_fee_bps: float = 0.0
    taker_fee_bps: float = 5.0
    gas_per_tx: float = 0.0
    """Per transaction, in quote currency. A cancel-all plus re-post is one batched transaction."""


@dataclass
class RestingOrder:
    id: int
    side: Side
    price: float
    size: float
    remaining: float
    placed_block: int
    queue_ahead: float


@dataclass(frozen=True)
class TxReceipt:
    gas: float
    fills: tuple[Fill, ...] = ()
    rejected: tuple[Quote, ...] = ()
    error: str | None = None


@dataclass
class VenueStats:
    txs: int = 0
    orders_posted: int = 0
    orders_rejected: int = 0
    orders_filled: int = 0
    _filled_ids: set[int] = field(default_factory=set, repr=False)


class Venue(Protocol):
    stats: VenueStats

    def open_orders(self) -> list[RestingOrder]: ...

    def match(self, block: Block) -> list[Fill]:
        """Fills of our resting orders by the prints in `block`."""
        ...

    def replace_quotes(self, quotes: list[Quote], block: Block) -> TxReceipt:
        """Cancel everything resting and post `quotes` as post-only orders, in one transaction."""
        ...

    def cancel_all(self, block: Block) -> TxReceipt: ...

    def market_order(self, side: Side, size: float, block: Block) -> TxReceipt: ...


class PaperVenue:
    def __init__(self, costs: CostModel | None = None, activation_delay_blocks: int = 1) -> None:
        self.costs = costs or CostModel()
        self.activation_delay_blocks = activation_delay_blocks
        self.stats = VenueStats()
        self._orders: dict[int, RestingOrder] = {}
        self._next_id = 1

    def open_orders(self) -> list[RestingOrder]:
        return list(self._orders.values())

    def match(self, block: Block) -> list[Fill]:
        fills: list[Fill] = []
        live = [o for o in self._orders.values() if block.number - o.placed_block >= self.activation_delay_blocks]
        for trade in block.trades:
            left = trade.size
            for order in live:
                if left <= 0 or order.remaining <= 0 or trade.aggressor is order.side:
                    continue
                at = same_price(trade.price, order.price)
                through = not at and (trade.price < order.price if order.side is Side.BUY else trade.price > order.price)
                if not (at or through):
                    continue
                if at and order.queue_ahead > 0:
                    eaten = min(order.queue_ahead, left)
                    order.queue_ahead -= eaten
                    left -= eaten
                size = min(order.remaining, left)
                if size <= 0:
                    continue
                order.remaining -= size
                left -= size
                fee = order.price * size * self.costs.maker_fee_bps / 1e4
                fills.append(Fill(order.id, order.side, order.price, size, fee, trade.ts, block.number, maker=True))
                if order.id not in self.stats._filled_ids:
                    self.stats._filled_ids.add(order.id)
                    self.stats.orders_filled += 1
        for order in list(self._orders.values()):
            if order.remaining <= 1e-12:
                del self._orders[order.id]
            else:
                order.queue_ahead = min(order.queue_ahead, block.book.size_at(order.side, order.price))
        return fills

    def replace_quotes(self, quotes: list[Quote], block: Block) -> TxReceipt:
        self._orders.clear()
        rejected: list[Quote] = []
        book = block.book
        for quote in quotes:
            self.stats.orders_posted += 1
            crosses = quote.price >= book.best_ask if quote.side is Side.BUY else quote.price <= book.best_bid
            if crosses:
                rejected.append(quote)
                self.stats.orders_rejected += 1
                continue
            order = RestingOrder(self._next_id, quote.side, quote.price, quote.size, quote.size, block.number, book.size_at(quote.side, quote.price))
            self._orders[order.id] = order
            self._next_id += 1
        return self._tx(rejected=tuple(rejected))

    def cancel_all(self, block: Block) -> TxReceipt:
        if not self._orders:
            return TxReceipt(gas=0.0)
        self._orders.clear()
        return self._tx()

    def market_order(self, side: Side, size: float, block: Block) -> TxReceipt:
        """Sweep the opposite side of the book. Used only to flatten on a kill."""
        fills: list[Fill] = []
        left = size
        for level in block.book.asks if side is Side.BUY else block.book.bids:
            if left <= 0:
                break
            take = min(left, level.size)
            fee = level.price * take * self.costs.taker_fee_bps / 1e4
            fills.append(Fill(0, side, level.price, take, fee, block.ts, block.number, maker=False))
            left -= take
        return self._tx(fills=tuple(fills))

    def _tx(self, fills: tuple[Fill, ...] = (), rejected: tuple[Quote, ...] = ()) -> TxReceipt:
        self.stats.txs += 1
        return TxReceipt(gas=self.costs.gas_per_tx, fills=fills, rejected=rejected)
