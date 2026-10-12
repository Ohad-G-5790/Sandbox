"""Jupiter swap API (v1): the emergency exit. When the kill switch trips, inventory is swapped back to
target through whatever route Jupiter finds, with a hard slippage cap.

Flow: GET /quote -> POST /swap (returns an unsigned versioned transaction) -> sign locally -> send via RPC.
Without an API key the free endpoint (lite-api.jup.ag) is used; with one, api.jup.ag.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FREE_BASE_URL = "https://lite-api.jup.ag/swap/v1"
KEYED_BASE_URL = "https://api.jup.ag/swap/v1"

Http = Callable[[str, bytes | None, dict[str, str], float], bytes]


def _urllib_http(url: str, body: bytes | None, headers: dict[str, str], timeout_s: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.read()


class JupiterError(Exception):
    pass


@dataclass(frozen=True)
class SwapQuote:
    raw: dict[str, Any]
    """The quote exactly as Jupiter returned it: /swap needs it back unchanged."""
    in_amount: int
    out_amount: int
    min_out_amount: int
    price_impact_pct: float
    route: str


class JupiterClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout_s: float = 5.0, http: Http | None = None) -> None:
        self.base_url = (base_url or (KEYED_BASE_URL if api_key else FREE_BASE_URL)).rstrip("/")
        self._headers = {"Content-Type": "application/json", **({"x-api-key": api_key} if api_key else {})}
        self.timeout_s = timeout_s
        self._http = http or _urllib_http

    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> SwapQuote:
        """`amount` is in the input token's raw units (lamports for SOL, micro-USDC for USDC)."""
        query = urllib.parse.urlencode(
            {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount), "slippageBps": str(slippage_bps), "swapMode": "ExactIn"}
        )
        raw = self._request(f"{self.base_url}/quote?{query}", None)
        try:
            return SwapQuote(
                raw=raw,
                in_amount=int(raw["inAmount"]),
                out_amount=int(raw["outAmount"]),
                min_out_amount=int(raw["otherAmountThreshold"]),
                price_impact_pct=float(raw.get("priceImpactPct") or 0.0),
                route=" > ".join(step["swapInfo"].get("label", "?") for step in raw.get("routePlan", [])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise JupiterError(f"unexpected quote response: {error}") from error

    def swap_transaction(self, quote: SwapQuote, user_pubkey: str, max_priority_lamports: int = 1_000_000) -> bytes:
        """The unsigned versioned transaction for `quote`, with a capped priority fee."""
        body = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {"priorityLevel": "veryHigh", "maxLamports": max_priority_lamports}},
        }
        reply = self._request(f"{self.base_url}/swap", json.dumps(body).encode())
        if "swapTransaction" not in reply:
            raise JupiterError(f"no swapTransaction in response: {sorted(reply)}")
        return base64.b64decode(reply["swapTransaction"])

    def _request(self, url: str, body: bytes | None) -> dict[str, Any]:
        try:
            reply = json.loads(self._http(url, body, self._headers, self.timeout_s))
        except (OSError, ValueError) as error:
            raise JupiterError(f"{url.split('?')[0]}: {error}") from error
        if not isinstance(reply, dict) or "error" in reply:
            raise JupiterError(f"{url.split('?')[0]}: {reply}")
        return reply


def sign_versioned_transaction(unsigned: bytes, keypair: Any) -> bytes:
    """Sign a Jupiter-built transaction locally. Refuses one whose fee payer is not our wallet."""
    from solders.transaction import VersionedTransaction

    tx = VersionedTransaction.from_bytes(unsigned)
    payer = tx.message.account_keys[0]
    if payer != keypair.pubkey():
        raise JupiterError(f"transaction fee payer {payer} is not our wallet {keypair.pubkey()}")
    return bytes(VersionedTransaction(tx.message, [keypair]))
