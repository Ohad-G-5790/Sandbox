"""Loading the trading keypair. The key never lives in config, journals, logs or chat."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

KEYPAIR_ENV = "HFTBOT_KEYPAIR"
"""Path to a solana-keygen JSON keypair file for a wallet dedicated to this bot."""

KEYPAIR_JSON_ENV = "HFTBOT_KEYPAIR_JSON"
"""The keypair file's contents (a JSON array of 64 numbers), for cloud hosts that keep secrets as
encrypted environment variables. Set it as a secret, never in a committed file."""


def load_keypair(path: str | Path | None = None) -> Any:
    from solders.keypair import Keypair

    if path is None and os.environ.get(KEYPAIR_JSON_ENV):
        return Keypair.from_bytes(_secret_bytes(os.environ[KEYPAIR_JSON_ENV], KEYPAIR_JSON_ENV))
    path = path or os.environ.get(KEYPAIR_ENV)
    if not path:
        raise RuntimeError(f"set {KEYPAIR_ENV} (a keypair file path) or {KEYPAIR_JSON_ENV} (its contents) for a wallet funded only with what you can lose")
    file = Path(path).expanduser()
    # POSIX permission bits only mean something off Windows; there, keep the file inside your user folder.
    if os.name != "nt" and file.stat().st_mode & 0o077:
        raise RuntimeError(f"{file} is readable by other users: chmod 600 it first")
    return Keypair.from_bytes(_secret_bytes(file.read_text(), str(file)))


def _secret_bytes(text: str, source: str) -> bytes:
    try:
        secret = json.loads(text)
    except ValueError:
        secret = None
    if not (isinstance(secret, list) and len(secret) == 64 and all(isinstance(b, int) and 0 <= b < 256 for b in secret)):
        # Never echo the value: it is a private key.
        raise RuntimeError(f"{source} is not a solana-keygen keypair (expected a JSON array of 64 bytes)")
    return bytes(secret)
