"""Market data types: order book snapshots, trade prints and blocks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


def same_price(a: float, b: float) -> bool:
    """Price equality with a tolerance for float noise from tick rounding."""
    return abs(a - b) <= 1e-9 * max(1.0, abs(a))


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for buys, -1 for sells: the direction a fill moves inventory."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    """An L2 snapshot. Bids are sorted best (highest) first, asks best (lowest) first."""

    ts: float
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    def __post_init__(self) -> None:
        if not self.bids or not self.asks:
            raise ValueError("book needs at least one level on each side")
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError(f"crossed book: bid {self.bids[0].price} >= ask {self.asks[0].price}")

    @property
    def best_bid(self) -> float:
        return self.bids[0].price

    @property
    def best_ask(self) -> float:
        return self.asks[0].price

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> float:
        return self.spread / self.mid * 1e4

    @property
    def microprice(self) -> float:
        """Mid weighted toward the side with less size at the touch, where price is likelier to move."""
        bid, ask = self.bids[0], self.asks[0]
        total = bid.size + ask.size
        if total <= 0:
            return self.mid
        return (bid.price * ask.size + ask.price * bid.size) / total

    def depth(self, side: Side, levels: int = 3) -> float:
        book_side = self.bids if side is Side.BUY else self.asks
        return sum(level.size for level in book_side[:levels])

    def imbalance(self, levels: int = 3) -> float:
        """(bid depth - ask depth) / total over the top levels: +1 is all bids, -1 all asks."""
        bid, ask = self.depth(Side.BUY, levels), self.depth(Side.SELL, levels)
        total = bid + ask
        return 0.0 if total <= 0 else (bid - ask) / total

    def size_at(self, side: Side, price: float) -> float:
        """Resting size at exactly `price` on `side` (the queue a new order there would join)."""
        book_side = self.bids if side is Side.BUY else self.asks
        return sum(level.size for level in book_side if same_price(level.price, price))


@dataclass(frozen=True)
class Trade:
    """A taker print. `aggressor` is the taker's side: BUY means someone lifted the ask."""

    ts: float
    price: float
    size: float
    aggressor: Side


@dataclass(frozen=True)
class Block:
    """Everything observed at one block: the book after it, and the prints since the previous block.

    `reference_mid` is an optional price from another venue (a CEX mid, an oracle) for cross-venue features.
    """

    number: int
    ts: float
    book: Book
    trades: tuple[Trade, ...] = ()
    reference_mid: float | None = None
