"""Loading the trading keypair. The key never lives in config, journals, logs or chat."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

KEYPAIR_ENV = "HFTBOT_KEYPAIR"
"""Path to a solana-keygen JSON keypair file for a wallet dedicated to this bot."""


def load_keypair(path: str | Path | None = None) -> Any:
    from solders.keypair import Keypair

    path = path or os.environ.get(KEYPAIR_ENV)
    if not path:
        raise RuntimeError(f"set {KEYPAIR_ENV} to the path of a keypair file for a wallet funded only with what you can lose")
    file = Path(path).expanduser()
    if file.stat().st_mode & 0o077:
        raise RuntimeError(f"{file} is readable by other users: chmod 600 it first")
    secret = json.loads(file.read_text())
    if not (isinstance(secret, list) and len(secret) == 64):
        raise RuntimeError(f"{file} is not a solana-keygen keypair file (expected a JSON array of 64 bytes)")
    return Keypair.from_bytes(bytes(secret))
