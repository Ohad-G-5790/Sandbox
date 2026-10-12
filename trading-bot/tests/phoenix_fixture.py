"""Builds Phoenix market accounts and event logs byte by byte, following the documented layout."""

from __future__ import annotations

import struct

from hftbot.solana.phoenix import BID_BIT, MARKET_DISCRIMINANT, ORDER_NODE_SIZE, TRADER_NODE_SIZE, TREE_HEADER_SIZE, TREES_OFFSET
from hftbot.solana.rpc import b58decode

SOL = b58decode("So11111111111111111111111111111111111111112")
USDC = b58decode("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
BASE_VAULT = bytes(range(1, 33))
QUOTE_VAULT = bytes(range(33, 65))


def bid_seq(seq: int) -> int:
    return ~seq & ((1 << 64) - 1)


def _tree(nodes: list[bytes], capacity: int, node_size: int, free: list[bytes]) -> bytes:
    """Live nodes first, then `free` nodes (stale bytes) linked into the free list."""
    allocated = nodes + free
    bump = len(allocated) + 1
    free_addresses = list(range(len(nodes) + 1, bump))
    head = free_addresses[0] if free_addresses else bump
    body = bytearray()
    for address, payload in enumerate(allocated, start=1):
        nxt = 0
        if address in free_addresses:
            i = free_addresses.index(address)
            nxt = free_addresses[i + 1] if i + 1 < len(free_addresses) else bump
        body += struct.pack("<4I", nxt, 0, 0, 0) + payload
    body += bytes(node_size * (capacity - len(allocated)))
    return bytes(16) + struct.pack("<QII", 0, bump, head) + bytes(body)


def order(price_ticks: int, seq: int, trader_index: int, lots: int, last_valid_slot: int = 0, last_valid_ts: int = 0, bid: bool = False) -> bytes:
    stored = bid_seq(seq) if bid else seq
    return struct.pack("<QQ", price_ticks, stored) + struct.pack("<4Q", trader_index, lots, last_valid_slot, last_valid_ts)


def trader(pubkey: bytes, quote_locked: int = 0, quote_free: int = 0, base_locked: int = 0, base_free: int = 0) -> bytes:
    return pubkey + struct.pack("<4Q", quote_locked, quote_free, base_locked, base_free) + bytes(64)


def market_account(bids: list[bytes], asks: list[bytes], traders: list[bytes], *, capacity: int = 8, seats: int = 4, free_bids: list[bytes] = (), status: int = 1) -> bytes:
    header = bytearray(TREES_OFFSET)
    struct.pack_into("<5Q", header, 0, MARKET_DISCRIMINANT, status, capacity, capacity, seats)
    struct.pack_into("<II", header, 40, 9, 0)
    header[48:80], header[80:112] = SOL, BASE_VAULT
    struct.pack_into("<Q", header, 112, 1_000_000)  # base lot = 0.001 SOL
    struct.pack_into("<II", header, 120, 6, 0)
    header[128:160], header[160:192] = USDC, QUOTE_VAULT
    struct.pack_into("<Q", header, 192, 1)  # quote lot = 1 micro-USDC
    struct.pack_into("<Q", header, 200, 1_000)  # tick = 0.001 USDC per SOL
    struct.pack_into("<Q", header, 272, 42)
    struct.pack_into("<I", header, 312, 1)
    struct.pack_into("<4Q", header, 832, 1_000, 1_000, 99, 2)  # base lots per SOL, tick in quote lots, seq, taker fee bps
    data = bytes(header) + _tree(bids, capacity, ORDER_NODE_SIZE, list(free_bids)) + _tree(asks, capacity, ORDER_NODE_SIZE, []) + _tree(traders, seats, TRADER_NODE_SIZE, [])
    assert len(data) == TREES_OFFSET + 2 * (TREE_HEADER_SIZE + ORDER_NODE_SIZE * capacity) + TREE_HEADER_SIZE + TRADER_NODE_SIZE * seats
    return data


def log_data(market: bytes, events: list[bytes], ts: int = 1_760_000_000, slot: int = 77) -> bytes:
    header = bytes([15, 1, 2]) + struct.pack("<QqQ", 5, ts, slot) + market + bytes(32) + struct.pack("<H", len(events))
    return header + b"".join(events)


def fill_event(maker: bytes, seq: int, price_ticks: int, lots: int, remaining: int = 0, bid: bool = False) -> bytes:
    stored = bid_seq(seq) if bid else seq
    return bytes([2]) + struct.pack("<H", 0) + maker + struct.pack("<4Q", stored, price_ticks, lots, remaining)


def place_event(seq: int, price_ticks: int, lots: int) -> bytes:
    return bytes([3]) + struct.pack("<HQ", 0, seq) + bytes(16) + struct.pack("<QQ", price_ticks, lots)


def fee_event(quote_lots: int) -> bytes:
    return bytes([7]) + struct.pack("<HQ", 0, quote_lots)
