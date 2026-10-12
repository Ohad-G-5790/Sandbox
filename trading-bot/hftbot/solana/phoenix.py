"""Phoenix (Ellipsis Labs' Solana limit order book): market account parsing, instructions, fill events.

Layouts follow the on-chain program (github.com/Ellipsis-Labs/phoenix-v1) and the official Python SDK
(phoenixpy). All integers are little-endian. Reading needs only the standard library; building
instructions needs `solders`.

Market account:  MarketHeader (576 bytes) | 256 padding | FIFO fields (48 bytes, from 832)
                 | bids tree | asks tree | traders tree           (trees start at 880)
Tree:            16 header | u64 allocator size | u32 bump_index | u32 free_list_head | nodes
Node:            16 bytes of registers | key | value; addresses are 1-based, 0 is the sentinel
Order key:       u64 price_in_ticks, u64 order_sequence_number (bids store !seq: top bit set)
Order value:     u64 trader_index, u64 num_base_lots, u64 last_valid_slot, u64 last_valid_unix_ts
Trader key:      32-byte pubkey; value: u64 quote_lots_locked, quote_lots_free, base_lots_locked,
                 base_lots_free, then 64 bytes of padding. Trader index = the node's address.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from hftbot.market import Book, Level, Side, Trade

PHOENIX_PROGRAM_ID = "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY"
PHOENIX_LOG_AUTHORITY = "7aDTsspkQNGKmrexAN7FLx9oxU3iPczSSvHNggyuqYkR"
SEAT_MANAGER_PROGRAM_ID = "PSMxQbAoDWDbvd9ezQJgARyq6R9L5kJAasaLDVcZwf1"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"

SOL_USDC_MAINNET = "4DoNfFBfF7UokCC2FQzriy7yHK6DY6NVdYpuekQ5pRgg"
SOL_USDC_DEVNET = "CS2H8nbAVVEUHWPF5extCSymqheQdkd4d7thik6eet9N"

MARKET_DISCRIMINANT = 8167313896524341111
HEADER_SIZE = 576
TREES_OFFSET = 880
ORDER_NODE_SIZE = 16 + 16 + 32
TRADER_NODE_SIZE = 16 + 32 + 96
TREE_HEADER_SIZE = 32
BID_BIT = 1 << 63
U64 = (1 << 64) - 1

STATUS = {0: "uninitialized", 1: "active", 2: "post_only", 3: "paused", 4: "closed", 5: "tombstoned"}

# Instruction tags (phoenix-v1 src/program/instruction.rs)
IX_CANCEL_ALL_ORDERS = 6
IX_WITHDRAW_FUNDS = 12
IX_LOG = 15
IX_PLACE_MULTIPLE_POST_ONLY_ORDERS = 16
SEAT_MANAGER_IX_CLAIM_SEAT = 1
# FailedMultipleLimitOrderBehavior: skip a side we cannot fund, and amend (do not fail) an order that
# would cross. A failed placement fails the whole transaction, including the cancel bundled with it.
SKIP_ON_INSUFFICIENT_FUNDS_AND_AMEND_ON_CROSS = 2


@dataclass(frozen=True)
class MarketHeader:
    status: int
    bids_size: int
    asks_size: int
    num_seats: int
    base_decimals: int
    base_mint: bytes
    base_vault: bytes
    base_lot_size: int
    """Base atoms per base lot."""
    quote_decimals: int
    quote_mint: bytes
    quote_vault: bytes
    quote_lot_size: int
    """Quote atoms per quote lot."""
    tick_size_in_quote_atoms_per_base_unit: int
    market_sequence_number: int
    raw_base_units_per_base_unit: int
    base_lots_per_base_unit: int
    tick_size_in_quote_lots_per_base_unit: int
    taker_fee_bps: int

    @property
    def bids_offset(self) -> int:
        return TREES_OFFSET

    @property
    def asks_offset(self) -> int:
        return self.bids_offset + TREE_HEADER_SIZE + ORDER_NODE_SIZE * self.bids_size

    @property
    def traders_offset(self) -> int:
        return self.asks_offset + TREE_HEADER_SIZE + ORDER_NODE_SIZE * self.asks_size

    @property
    def books_end(self) -> int:
        """Bytes needed to read both order trees (enough for a book, not for trader balances)."""
        return self.traders_offset

    @property
    def account_size(self) -> int:
        return self.traders_offset + TREE_HEADER_SIZE + TRADER_NODE_SIZE * self.num_seats

    @property
    def tick_price(self) -> float:
        """One tick, in quote units per base unit."""
        return self.ticks_to_price(1)

    @property
    def lot_units(self) -> float:
        """One base lot, in base units."""
        return self.lots_to_units(1)

    def ticks_to_price(self, ticks: int) -> float:
        return ticks * self.tick_size_in_quote_atoms_per_base_unit / (10**self.quote_decimals * self.raw_base_units_per_base_unit)

    def price_to_ticks(self, price: float, side: Side) -> int:
        """Bids round down and asks round up: never quote more aggressively than asked."""
        exact = price / self.tick_price
        return math.floor(exact + 1e-9) if side is Side.BUY else math.ceil(exact - 1e-9)

    def lots_to_units(self, lots: int) -> float:
        return lots * self.base_lot_size / 10**self.base_decimals

    def units_to_lots(self, units: float) -> int:
        return math.floor(units / self.lot_units + 1e-9)


def parse_header(data: bytes) -> MarketHeader:
    if len(data) < TREES_OFFSET:
        raise ValueError(f"market account too short: {len(data)} bytes")
    discriminant, status, bids_size, asks_size, num_seats = struct.unpack_from("<5Q", data, 0)
    if discriminant != MARKET_DISCRIMINANT:
        raise ValueError("not a Phoenix market account (bad discriminant)")
    base_lots_per_base_unit, tick_quote_lots, _, taker_fee_bps = struct.unpack_from("<4Q", data, 832)
    return MarketHeader(
        status=status,
        bids_size=bids_size,
        asks_size=asks_size,
        num_seats=num_seats,
        base_decimals=struct.unpack_from("<I", data, 40)[0],
        base_mint=bytes(data[48:80]),
        base_vault=bytes(data[80:112]),
        base_lot_size=struct.unpack_from("<Q", data, 112)[0],
        quote_decimals=struct.unpack_from("<I", data, 120)[0],
        quote_mint=bytes(data[128:160]),
        quote_vault=bytes(data[160:192]),
        quote_lot_size=struct.unpack_from("<Q", data, 192)[0],
        tick_size_in_quote_atoms_per_base_unit=struct.unpack_from("<Q", data, 200)[0],
        market_sequence_number=struct.unpack_from("<Q", data, 272)[0],
        raw_base_units_per_base_unit=max(struct.unpack_from("<I", data, 312)[0], 1),
        base_lots_per_base_unit=base_lots_per_base_unit,
        tick_size_in_quote_lots_per_base_unit=tick_quote_lots,
        taker_fee_bps=taker_fee_bps,
    )


def _live_nodes(data: bytes, offset: int, node_size: int, capacity: int) -> Iterator[int]:
    """Byte offsets of the keys of allocated, non-free nodes."""
    bump, free_head = struct.unpack_from("<II", data, offset + 24)
    if bump > capacity + 1:
        raise ValueError(f"corrupt tree: bump index {bump} beyond capacity {capacity}")
    nodes = offset + TREE_HEADER_SIZE
    free: set[int] = set()
    pointer, steps = free_head, 0
    while 0 < pointer < bump:  # the free list ends at bump_index
        free.add(pointer)
        pointer = struct.unpack_from("<I", data, nodes + (pointer - 1) * node_size)[0]
        steps += 1
        if steps > bump:
            raise ValueError("corrupt tree: free-list loop")
    for address in range(1, bump):
        if address not in free:
            yield nodes + (address - 1) * node_size + 16


@dataclass(frozen=True)
class RestingOrder:
    side: Side
    price_in_ticks: int
    sequence_number: int
    """As stored (inverted for bids): with the price, it identifies the order."""
    trader_index: int
    base_lots: int
    last_valid_slot: int
    last_valid_ts: int

    def expired(self, slot: int, now: float) -> bool:
        return (self.last_valid_slot != 0 and self.last_valid_slot < slot) or (self.last_valid_ts != 0 and self.last_valid_ts < now)


@dataclass(frozen=True)
class TraderState:
    index: int
    quote_lots_locked: int
    quote_lots_free: int
    base_lots_locked: int
    base_lots_free: int


@dataclass(frozen=True)
class PhoenixMarket:
    header: MarketHeader
    bids: tuple[RestingOrder, ...]
    """Best first, then time priority."""
    asks: tuple[RestingOrder, ...]
    traders: dict[bytes, TraderState] | None
    """None when only the order trees were read."""

    def book(self, slot: int, now: float, levels: int = 10) -> Book:
        """Aggregated L2 book without expired orders. Raises if a side is empty."""
        h = self.header

        def aggregate(orders: tuple[RestingOrder, ...]) -> tuple[Level, ...]:
            out: list[Level] = []
            last_tick, size = None, 0
            for o in orders:
                if o.expired(slot, now):
                    continue
                if o.price_in_ticks != last_tick:
                    if last_tick is not None:
                        out.append(Level(h.ticks_to_price(last_tick), h.lots_to_units(size)))
                        if len(out) == levels:
                            return tuple(out)
                    last_tick, size = o.price_in_ticks, 0
                size += o.base_lots
            if last_tick is not None and len(out) < levels:
                out.append(Level(h.ticks_to_price(last_tick), h.lots_to_units(size)))
            return tuple(out)

        return Book(now, aggregate(self.bids), aggregate(self.asks))

    def orders_of(self, trader_index: int) -> list[RestingOrder]:
        return [o for o in self.bids + self.asks if o.trader_index == trader_index]


def parse_market(data: bytes, header: MarketHeader | None = None) -> PhoenixMarket:
    """Parse the order trees, and the traders tree too when `data` holds the whole account."""
    header = header or parse_header(data)
    if len(data) < header.books_end:
        raise ValueError(f"need {header.books_end} bytes for the order trees, got {len(data)}")

    def orders(offset: int, capacity: int) -> list[RestingOrder]:
        out = []
        for key in _live_nodes(data, offset, ORDER_NODE_SIZE, capacity):
            price, seq = struct.unpack_from("<QQ", data, key)
            trader_index, lots, lvs, lvt = struct.unpack_from("<4Q", data, key + 16)
            out.append(RestingOrder(Side.BUY if seq & BID_BIT else Side.SELL, price, seq, trader_index, lots, lvs, lvt))
        return out

    # Bids: highest price first; among equal prices the larger stored (inverted) number is older.
    bids = sorted(orders(header.bids_offset, header.bids_size), key=lambda o: (o.price_in_ticks, o.sequence_number), reverse=True)
    asks = sorted(orders(header.asks_offset, header.asks_size), key=lambda o: (o.price_in_ticks, o.sequence_number))
    if any(o.side is not Side.BUY for o in bids) or any(o.side is not Side.SELL for o in asks):
        raise ValueError("corrupt book: order side does not match its tree")

    traders = None
    if len(data) >= header.account_size:
        traders = {}
        nodes = header.traders_offset + TREE_HEADER_SIZE
        for key in _live_nodes(data, header.traders_offset, TRADER_NODE_SIZE, header.num_seats):
            index = (key - 16 - nodes) // TRADER_NODE_SIZE + 1
            ql, qf, bl, bf = struct.unpack_from("<4Q", data, key + 32)
            traders[bytes(data[key : key + 32])] = TraderState(index, ql, qf, bl, bf)
    return PhoenixMarket(header, tuple(bids), tuple(asks), traders)


# --- Events -------------------------------------------------------------------------------------

_EVENT_SIZES = {2: 67, 3: 43, 4: 35, 5: 59, 6: 43, 7: 11, 8: 27, 9: 59}
_LOG_HEADER_SIZE = 93


@dataclass(frozen=True)
class FillEvent:
    maker: bytes
    maker_side: Side
    sequence_number: int
    price_in_ticks: int
    base_lots_filled: int
    base_lots_remaining: int
    timestamp: int
    slot: int


def parse_log_events(data: bytes, market: bytes) -> list[FillEvent]:
    """Fill events from one Phoenix Log instruction's data, for `market` only."""
    if len(data) < _LOG_HEADER_SIZE or data[0] != IX_LOG or data[1] != 1:
        return []
    timestamp, slot = struct.unpack_from("<qQ", data, 11)
    if bytes(data[27:59]) != market:
        return []
    (count,) = struct.unpack_from("<H", data, 91)
    fills, offset = [], _LOG_HEADER_SIZE
    for _ in range(count):
        tag = data[offset]
        if tag not in _EVENT_SIZES:
            raise ValueError(f"unknown Phoenix event tag {tag}")
        if tag == 2:
            maker = bytes(data[offset + 3 : offset + 35])
            seq, price, filled, remaining = struct.unpack_from("<4Q", data, offset + 35)
            fills.append(FillEvent(maker, Side.BUY if seq & BID_BIT else Side.SELL, seq, price, filled, remaining, timestamp, slot))
        offset += _EVENT_SIZES[tag]
    return fills


def fills_from_transaction(tx: dict[str, Any], market: bytes) -> list[FillEvent]:
    """Fill events in a getTransaction (encoding "json") result. Failed transactions have none."""
    from hftbot.solana.rpc import b58decode

    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    loaded = meta.get("loadedAddresses") or {}
    keys = tx["transaction"]["message"]["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
    fills: list[FillEvent] = []
    for group in meta.get("innerInstructions") or []:
        for ix in group["instructions"]:
            if keys[ix["programIdIndex"]] == PHOENIX_PROGRAM_ID:
                fills.extend(parse_log_events(b58decode(ix["data"]), market))
    return fills


def trade_from_fill(fill: FillEvent, header: MarketHeader, ts: float) -> Trade:
    """The taker's print: a filled resting bid means the taker sold."""
    return Trade(ts, header.ticks_to_price(fill.price_in_ticks), header.lots_to_units(fill.base_lots_filled), fill.maker_side.opposite)


# --- Instructions (need solders) ----------------------------------------------------------------


def pda(seeds: list[bytes], program: str) -> Any:
    from solders.pubkey import Pubkey

    return Pubkey.find_program_address(seeds, Pubkey.from_string(program))[0]


def seat_address(market: Any, trader: Any) -> Any:
    return pda([b"seat", bytes(market), bytes(trader)], PHOENIX_PROGRAM_ID)


def associated_token_address(owner: Any, mint: Any) -> Any:
    from solders.pubkey import Pubkey

    return pda([bytes(owner), bytes(Pubkey.from_string(TOKEN_PROGRAM_ID)), bytes(mint)], ASSOCIATED_TOKEN_PROGRAM_ID)


@dataclass(frozen=True)
class TraderAccounts:
    market: Any
    trader: Any
    seat: Any
    base_account: Any
    quote_account: Any
    base_vault: Any
    quote_vault: Any

    @classmethod
    def derive(cls, market: Any, trader: Any, header: MarketHeader) -> TraderAccounts:
        from solders.pubkey import Pubkey

        base_mint, quote_mint = Pubkey.from_bytes(header.base_mint), Pubkey.from_bytes(header.quote_mint)
        return cls(
            market=market,
            trader=trader,
            seat=seat_address(market, trader),
            base_account=associated_token_address(trader, base_mint),
            quote_account=associated_token_address(trader, quote_mint),
            base_vault=Pubkey.from_bytes(header.base_vault),
            quote_vault=Pubkey.from_bytes(header.quote_vault),
        )


def _metas(a: TraderAccounts, with_seat: bool) -> list[Any]:
    from solders.instruction import AccountMeta
    from solders.pubkey import Pubkey

    metas = [
        AccountMeta(Pubkey.from_string(PHOENIX_PROGRAM_ID), is_signer=False, is_writable=False),
        AccountMeta(Pubkey.from_string(PHOENIX_LOG_AUTHORITY), is_signer=False, is_writable=False),
        AccountMeta(a.market, is_signer=False, is_writable=True),
        AccountMeta(a.trader, is_signer=True, is_writable=False),
    ]
    if with_seat:
        metas.append(AccountMeta(a.seat, is_signer=False, is_writable=False))
    metas += [
        AccountMeta(a.base_account, is_signer=False, is_writable=True),
        AccountMeta(a.quote_account, is_signer=False, is_writable=True),
        AccountMeta(a.base_vault, is_signer=False, is_writable=True),
        AccountMeta(a.quote_vault, is_signer=False, is_writable=True),
        AccountMeta(Pubkey.from_string(TOKEN_PROGRAM_ID), is_signer=False, is_writable=False),
    ]
    return metas


def cancel_all_orders_ix(a: TraderAccounts) -> Any:
    """Cancel every resting order and return the released funds to the wallet's token accounts."""
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    return Instruction(Pubkey.from_string(PHOENIX_PROGRAM_ID), bytes([IX_CANCEL_ALL_ORDERS]), _metas(a, with_seat=False))


def withdraw_funds_ix(a: TraderAccounts) -> Any:
    """Move every free balance (fill proceeds included) from the market back to the wallet's token accounts."""
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    # WithdrawFunds { quote_lots: Option<u64>, base_lots: Option<u64> }: None means everything.
    return Instruction(Pubkey.from_string(PHOENIX_PROGRAM_ID), bytes([IX_WITHDRAW_FUNDS, 0, 0]), _metas(a, with_seat=False))


def place_post_only_orders_data(bids: list[tuple[int, int]], asks: list[tuple[int, int]]) -> bytes:
    """PlaceMultiplePostOnlyOrders data for (price_in_ticks, base_lots) orders, no expiry, no client id."""

    def orders(side: list[tuple[int, int]]) -> bytes:
        # CondensedOrder: u64 price, u64 size, Option<u64> last_valid_slot, Option<u64> last_valid_ts
        return struct.pack("<I", len(side)) + b"".join(struct.pack("<QQBB", price, lots, 0, 0) for price, lots in side)

    return bytes([IX_PLACE_MULTIPLE_POST_ONLY_ORDERS]) + orders(bids) + orders(asks) + bytes([0, SKIP_ON_INSUFFICIENT_FUNDS_AND_AMEND_ON_CROSS])


def place_post_only_orders_ix(a: TraderAccounts, bids: list[tuple[int, int]], asks: list[tuple[int, int]]) -> Any:
    """Post-only orders funded from free balance first, then the wallet's token accounts."""
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    if bids and asks and max(p for p, _ in bids) >= min(p for p, _ in asks):
        raise ValueError("bids and asks in one packet must not cross")
    return Instruction(Pubkey.from_string(PHOENIX_PROGRAM_ID), place_post_only_orders_data(bids, asks), _metas(a, with_seat=True))


def claim_seat_ix(market: Any, trader: Any) -> Any:
    """Seat Manager ClaimSeat: creates and approves this wallet's seat. Costs a refundable deposit of
    two token accounts' rent (about 0.004 SOL)."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    return Instruction(
        Pubkey.from_string(SEAT_MANAGER_PROGRAM_ID),
        bytes([SEAT_MANAGER_IX_CLAIM_SEAT]),
        [
            AccountMeta(Pubkey.from_string(PHOENIX_PROGRAM_ID), is_signer=False, is_writable=False),
            AccountMeta(Pubkey.from_string(PHOENIX_LOG_AUTHORITY), is_signer=False, is_writable=False),
            AccountMeta(market, is_signer=False, is_writable=True),
            AccountMeta(pda([bytes(market)], SEAT_MANAGER_PROGRAM_ID), is_signer=False, is_writable=True),
            AccountMeta(pda([bytes(market), b"deposit"], SEAT_MANAGER_PROGRAM_ID), is_signer=False, is_writable=True),
            AccountMeta(trader, is_signer=False, is_writable=False),
            AccountMeta(trader, is_signer=True, is_writable=True),
            AccountMeta(seat_address(market, trader), is_signer=False, is_writable=True),
            AccountMeta(Pubkey.from_string(SYSTEM_PROGRAM_ID), is_signer=False, is_writable=False),
        ],
    )
