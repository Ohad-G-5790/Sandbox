"""A minimal Solana JSON-RPC client over the standard library. Bring your own RPC URL: the public
endpoint rate-limits hard, and a market maker reads the book every slot."""

from __future__ import annotations

import base64
import itertools
import json
import urllib.request
from collections.abc import Callable
from typing import Any

PUBLIC_MAINNET_RPC = "https://api.mainnet-beta.solana.com"

Post = Callable[[str, bytes, float], bytes]


def _urllib_post(url: str, body: bytes, timeout_s: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.read()


class RpcError(Exception):
    pass


class SolanaRpc:
    def __init__(self, url: str = PUBLIC_MAINNET_RPC, timeout_s: float = 2.0, post: Post | None = None) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self._post = post or _urllib_post
        self._ids = itertools.count(1)

    def call(self, method: str, params: list[Any]) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}).encode()
        try:
            reply = json.loads(self._post(self.url, body, self.timeout_s))
        except (OSError, ValueError) as error:
            raise RpcError(f"{method}: {error}") from error
        if "error" in reply:
            raise RpcError(f"{method}: {reply['error']}")
        return reply["result"]

    def get_account_data(self, pubkey: str, commitment: str = "confirmed") -> tuple[int, bytes]:
        """(slot the read reflects, raw account data)."""
        result = self.call("getAccountInfo", [pubkey, {"encoding": "base64", "commitment": commitment}])
        if result["value"] is None:
            raise RpcError(f"account {pubkey} not found")
        return result["context"]["slot"], base64.b64decode(result["value"]["data"][0])

    def get_slot(self, commitment: str = "confirmed") -> int:
        return self.call("getSlot", [{"commitment": commitment}])

    def get_latest_blockhash(self, commitment: str = "confirmed") -> tuple[str, int]:
        value = self.call("getLatestBlockhash", [{"commitment": commitment}])["value"]
        return value["blockhash"], value["lastValidBlockHeight"]

    def get_token_balance(self, token_account: str, commitment: str = "confirmed") -> int:
        """Raw token amount (atoms) held by an SPL token account."""
        return int(self.call("getTokenAccountBalance", [token_account, {"commitment": commitment}])["value"]["amount"])

    def get_balance(self, pubkey: str, commitment: str = "confirmed") -> int:
        """Lamports held by an account."""
        return self.call("getBalance", [pubkey, {"commitment": commitment}])["value"]

    def send_transaction(self, signed_tx: bytes, skip_preflight: bool = True) -> str:
        """Fire and forget: returns the signature as soon as the RPC node accepts the transaction.

        Preflight simulation is skipped by default: it costs a round trip the block budget cannot spare,
        and a failed transaction still costs only its fee.
        """
        encoded = base64.b64encode(signed_tx).decode()
        return self.call("sendTransaction", [encoded, {"encoding": "base64", "skipPreflight": skip_preflight, "maxRetries": 0}])

    def get_signature_statuses(self, signatures: list[str]) -> list[dict[str, Any] | None]:
        return self.call("getSignatureStatuses", [signatures, {"searchTransactionHistory": False}])["value"]
