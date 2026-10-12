"""JSONL logs: every block's snapshot, decision and action (the calibration data), and block data files."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO, Any

from hftbot.loop import StepRecord
from hftbot.market import Block, Book, Level, Side, Trade


def step_to_dict(r: StepRecord) -> dict[str, Any]:
    return {
        "block": r.block,
        "ts": r.ts,
        "mid": r.mid,
        "ladder": r.ladder.value,
        "snapshot": r.snapshot,
        "decision": r.decision.to_dict() if r.decision else None,
        "action": None if r.action is None else {"kind": r.action.kind.value, "reason": r.action.reason},
        "resting": [{"side": q.side.value, "price": q.price, "size": q.size} for q in r.resting],
        "fills": [{"side": f.side.value, "price": f.price, "size": f.size, "fee": f.fee, "maker": f.maker} for f in r.fills],
        "vetoed": [{"side": q.side.value, "price": q.price, "reason": why} for q, why in r.vetoed],
        "gas": r.gas,
        "position": r.position,
        "equity": r.equity,
        "note": r.note,
    }


class Journal:
    def __init__(self, path: str | Path) -> None:
        self._file: IO[str] = open(path, "w", encoding="utf-8")

    def write(self, record: StepRecord) -> None:
        self._file.write(json.dumps(step_to_dict(record), separators=(",", ":")) + "\n")

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


# Block data format, one block per line:
#   {"number": 1, "ts": 1760000000.3, "book_ts": 1760000000.3,
#    "bids": [[price, size], ...best first], "asks": [[price, size], ...best first],
#    "trades": [[ts, price, size, "buy"|"sell" (taker side)], ...], "ref": 100.01}
# book_ts and ref are optional.


def block_to_dict(b: Block) -> dict[str, Any]:
    d: dict[str, Any] = {
        "number": b.number,
        "ts": b.ts,
        "bids": [[lvl.price, lvl.size] for lvl in b.book.bids],
        "asks": [[lvl.price, lvl.size] for lvl in b.book.asks],
        "trades": [[t.ts, t.price, t.size, t.aggressor.value] for t in b.trades],
    }
    if b.book.ts != b.ts:
        d["book_ts"] = b.book.ts
    if b.reference_mid is not None:
        d["ref"] = b.reference_mid
    return d


def block_from_dict(d: dict[str, Any]) -> Block:
    book = Book(
        d.get("book_ts", d["ts"]),
        tuple(Level(p, s) for p, s in d["bids"]),
        tuple(Level(p, s) for p, s in d["asks"]),
    )
    trades = tuple(Trade(ts, price, size, Side(side)) for ts, price, size, side in d.get("trades", ()))
    return Block(d["number"], d["ts"], book, trades, d.get("ref"))


def write_blocks(path: str | Path, blocks: Iterable[Block]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for b in blocks:
            f.write(json.dumps(block_to_dict(b), separators=(",", ":")) + "\n")


def read_blocks(path: str | Path) -> Iterator[Block]:
    return (block_from_dict(d) for d in read_jsonl(path))
