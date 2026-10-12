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

    def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """Several calls in one round trip. Results come back in call order; any error raises."""
        if not calls:
            return []
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        try:
            replies = json.loads(self._post(self.url, json.dumps(body).encode(), self.timeout_s))
        except (OSError, ValueError) as error:
            raise RpcError(f"batch: {error}") from error
        if not isinstance(replies, list):
            raise RpcError(f"batch not supported by this RPC: {replies}")
        by_id = {r.get("id"): r for r in replies}
        results = []
        for i, (method, _) in enumerate(calls):
            reply = by_id.get(i)
            if reply is None or "error" in reply:
                raise RpcError(f"{method}: {reply and reply.get('error')}")
            results.append(reply["result"])
        return results

    def get_account_data(self, pubkey: str, commitment: str = "confirmed", data_slice: tuple[int, int] | None = None) -> tuple[int, bytes]:
        """(slot the read reflects, raw account data). `data_slice` is (offset, length)."""
        options: dict[str, Any] = {"encoding": "base64", "commitment": commitment}
        if data_slice is not None:
            options["dataSlice"] = {"offset": data_slice[0], "length": data_slice[1]}
        result = self.call("getAccountInfo", [pubkey, options])
        if result["value"] is None:
            raise RpcError(f"account {pubkey} not found")
        return result["context"]["slot"], base64.b64decode(result["value"]["data"][0])

    def get_signatures_for_address(self, address: str, until: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        """Newest first. With `until`, only signatures newer than it."""
        options: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if until:
            options["until"] = until
        return self.call("getSignaturesForAddress", [address, options])

    @staticmethod
    def transaction_call(signature: str) -> tuple[str, list[Any]]:
        return "getTransaction", [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]

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


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58decode(text: str) -> bytes:
    n = 0
    for char in text:
        n = n * 58 + _B58_INDEX[char]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + body


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out
