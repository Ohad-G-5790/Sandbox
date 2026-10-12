"""Live Phoenix market data: the book every slot, and every fill on the market from transaction events.

Each poll reads the order trees (a slice of the market account) and the transactions that touched the
market since the last poll. Fills in those transactions become the block's trade prints, and the ones
whose maker is us become our fills. Every `refresh_every` polls the whole account is read, to find our
trader index and balances.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from hftbot.live import FeedError
from hftbot.market import Block, Book
from hftbot.portfolio import Fill
from hftbot.solana.phoenix import FillEvent, PhoenixMarket, RestingOrder, TraderState, fills_from_transaction, parse_header, parse_market, trade_from_fill
from hftbot.solana.rpc import RpcError, SolanaRpc, b58decode

log = logging.getLogger("hftbot")


class PhoenixFeed:
    def __init__(
        self,
        rpc: SolanaRpc,
        market: str,
        *,
        trader: str | None = None,
        levels: int = 10,
        poll_interval_s: float = 0.4,
        fetch_trades: bool = True,
        max_transactions_per_poll: int = 200,
        refresh_every: int = 25,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rpc = rpc
        self.market = market
        self.trader = b58decode(trader) if trader else None
        self.levels = levels
        self.poll_interval_s = poll_interval_s
        self.fetch_trades = fetch_trades
        self.max_transactions_per_poll = max_transactions_per_poll
        self.refresh_every = refresh_every
        self._clock, self._sleep = clock, sleep
        self._market_bytes = b58decode(market)

        _, data = self._read(full=True)
        self.header = parse_header(data)
        if self.header.status not in (1, 2):
            raise FeedError(f"market status is {self.header.status}: not accepting orders")
        self.state: PhoenixMarket = parse_market(data, self.header)
        self.slot = 0
        self.trader_state: TraderState | None = None
        self._update_trader(self.state)
        self._our_fills: list[Fill] = []
        self._last_signature: str | None = None
        if fetch_trades:  # start from now: history before the bot started is not ours to replay
            latest = self.rpc.get_signatures_for_address(market, limit=1)
            self._last_signature = latest[0]["signature"] if latest else None
        self._polls = 0
        self._slot_advanced_at: float | None = None
        self._next_poll = self._clock()

    def next_block(self) -> Block:
        delay = self._next_poll - self._clock()
        if delay > 0:
            self._sleep(delay)
        self._next_poll = max(self._next_poll + self.poll_interval_s, self._clock())
        self._polls += 1
        try:
            full = self.trader is not None and (self.trader_state is None or self._polls % self.refresh_every == 0)
            slot, data = self._read(full=full)
            state = parse_market(data, self.header)
            now = self._clock()
            book = state.book(slot, now, self.levels)
            events = self._new_fills() if self.fetch_trades else []
        except (RpcError, ValueError) as error:
            raise FeedError(str(error)) from error
        if slot > self.slot:
            self._slot_advanced_at = now
        elif self._slot_advanced_at is not None:
            # The node is not advancing: date the book to the last new slot, so the stale-data limit trips.
            book = Book(self._slot_advanced_at, book.bids, book.asks)
        self.state, self.slot = state, slot
        if state.traders is not None:
            self._update_trader(state)
        trades = tuple(sorted((trade_from_fill(e, self.header, float(e.timestamp)) for e in events), key=lambda t: t.ts))
        if self.trader is not None:
            for e in events:
                if e.maker == self.trader:
                    self._our_fills.append(self._our_fill(e, now))
        # Blocks are numbered by poll (one per ~slot), so numbers are unique even if a slot is read twice.
        return Block(self._polls, now, book, trades)

    def drain_our_fills(self) -> list[Fill]:
        fills, self._our_fills = self._our_fills, []
        return fills

    def our_orders(self) -> list[RestingOrder]:
        if self.trader_state is None:
            return []
        return [o for o in self.state.orders_of(self.trader_state.index) if not o.expired(self.slot, self._clock())]

    def _read(self, full: bool) -> tuple[int, bytes]:
        if full:
            return self.rpc.get_account_data(self.market)
        return self.rpc.get_account_data(self.market, data_slice=(0, self.header.books_end))

    def _update_trader(self, state: PhoenixMarket) -> None:
        if self.trader is None or state.traders is None:
            return
        found = state.traders.get(self.trader)
        if found is None and self.trader_state is not None:
            log.warning("our trader seat is no longer on the market (evicted?)")
        self.trader_state = found

    def _new_fills(self) -> list[FillEvent]:
        signatures = self.rpc.get_signatures_for_address(self.market, until=self._last_signature, limit=self.max_transactions_per_poll + 1)
        if not signatures:
            return []
        if len(signatures) > self.max_transactions_per_poll:
            log.warning("more than %d market transactions since the last poll: trade prints for this block are incomplete", self.max_transactions_per_poll)
            signatures = signatures[: self.max_transactions_per_poll]
        self._last_signature = signatures[0]["signature"]
        wanted = [s["signature"] for s in reversed(signatures) if s.get("err") is None]  # oldest first
        transactions = self.rpc.batch([SolanaRpc.transaction_call(sig) for sig in wanted])
        fills: list[FillEvent] = []
        for tx in transactions:
            if tx is not None:
                fills.extend(fills_from_transaction(tx, self._market_bytes))
        return fills

    def _our_fill(self, e: FillEvent, now: float) -> Fill:
        h = self.header
        # Makers pay no fee on Phoenix.
        return Fill(e.sequence_number, e.maker_side, h.ticks_to_price(e.price_in_ticks), h.lots_to_units(e.base_lots_filled), 0.0, now, self._polls, maker=True)
