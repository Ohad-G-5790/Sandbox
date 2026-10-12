"""Live swing execution: wallet balances from chain, swaps through Jupiter, every swap confirmed.

Before any swap the Jupiter price is compared with the reference price from the candle feed; a gap
above `max_price_deviation_pct` refuses the trade (a thin route, a stale feed, or a wrong token address
all show up this way). At startup every token's decimals are checked on chain against the coin list.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from typing import Any

from hftbot.solana.jupiter import JupiterClient, JupiterError, sign_versioned_transaction
from hftbot.solana.rpc import RpcError, SolanaRpc
from hftbot.swing import data
from hftbot.swing.trader import SwapError, SwapResult

TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
LAMPORTS_PER_SOL = 1_000_000_000


class JupiterSwapExecutor:
    mode = "live"

    def __init__(
        self,
        rpc: SolanaRpc,
        jupiter: JupiterClient,
        keypair: Any,
        coins: tuple[str, ...],
        *,
        sol_fee_reserve: float = 0.05,
        max_price_deviation_pct: float = 2.0,
        slippage_bps: int = 50,
        max_priority_lamports: int = 1_000_000,
        confirm_timeout_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rpc, self.jupiter, self.keypair, self.coins = rpc, jupiter, keypair, coins
        self.owner = str(keypair.pubkey())
        self.sol_fee_reserve = sol_fee_reserve
        self.max_price_deviation_pct = max_price_deviation_pct
        self.slippage_bps = slippage_bps
        self.max_priority_lamports = max_priority_lamports
        self.confirm_timeout_s = confirm_timeout_s
        self._sleep = sleep

    def verify_tokens(self) -> None:
        """Refuse to start if any configured token's on-chain decimals differ from the coin list."""
        mints = {data.USDC_MINT: data.USDC_DECIMALS} | {data.COINS[c].mint: data.COINS[c].decimals for c in self.coins if c != "SOL"}
        for mint, decimals in mints.items():
            info = self.rpc.call("getAccountInfo", [mint, {"encoding": "base64"}])["value"]
            if info is None or info["owner"] not in TOKEN_PROGRAMS:
                raise SwapError(f"{mint} is not a token mint on this network")
            raw = base64.b64decode(info["data"][0])
            if len(raw) < 45 or raw[44] != decimals:
                raise SwapError(f"{mint} has {raw[44] if len(raw) > 44 else '?'} decimals on chain, expected {decimals}: wrong address?")

    def balances(self) -> tuple[float, dict[str, float]]:
        held: dict[str, int] = {}
        for program in TOKEN_PROGRAMS:
            accounts = self.rpc.call("getTokenAccountsByOwner", [self.owner, {"programId": program}, {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
            for account in accounts:
                info = account["account"]["data"]["parsed"]["info"]
                held[info["mint"]] = held.get(info["mint"], 0) + int(info["tokenAmount"]["amount"])
        units: dict[str, float] = {}
        for c in self.coins:
            coin = data.COINS[c]
            amount = held.get(coin.mint, 0) / 10**coin.decimals
            if c == "SOL":  # native SOL, minus what is kept back for fees
                amount += max(self.rpc.get_balance(self.owner) / LAMPORTS_PER_SOL - self.sol_fee_reserve, 0.0)
            units[c] = amount
        return held.get(data.USDC_MINT, 0) / 10**data.USDC_DECIMALS, units

    def sell(self, coin: str, units: float, ref_price: float) -> SwapResult:
        c = data.COINS[coin]
        return self._swap(coin, "sell", c.mint, data.USDC_MINT, units, c.decimals, data.USDC_DECIMALS, ref_price)

    def buy(self, coin: str, usdc: float, ref_price: float) -> SwapResult:
        c = data.COINS[coin]
        return self._swap(coin, "buy", data.USDC_MINT, c.mint, usdc, data.USDC_DECIMALS, c.decimals, ref_price)

    def _swap(self, coin: str, side: str, in_mint: str, out_mint: str, amount: float, in_dec: int, out_dec: int, ref_price: float) -> SwapResult:
        try:
            quote = self.jupiter.quote(in_mint, out_mint, round(amount * 10**in_dec), self.slippage_bps)
            got = quote.out_amount / 10**out_dec
            spent = quote.in_amount / 10**in_dec
            units, usdc = (spent, got) if side == "sell" else (got, spent)
            if units <= 0:
                raise SwapError("empty quote")
            price = usdc / units
            worse_pct = (ref_price / price - 1 if side == "sell" else price / ref_price - 1) * 100
            if worse_pct > self.max_price_deviation_pct:
                raise SwapError(f"Jupiter price {price:.6g} is {worse_pct:.2f}% worse than the reference {ref_price:.6g}: refusing")
            unsigned = self.jupiter.swap_transaction(quote, self.owner, self.max_priority_lamports, wrap_and_unwrap_sol=True)
            signature = self.rpc.send_transaction(sign_versioned_transaction(unsigned, self.keypair))
        except (JupiterError, RpcError) as error:
            raise SwapError(str(error)) from error
        self._confirm(signature)
        return SwapResult(coin, side, units, usdc, price, abs(units * ref_price - usdc), "jupiter")

    def _confirm(self, signature: str) -> None:
        deadline = time.monotonic() + self.confirm_timeout_s
        while time.monotonic() < deadline:
            try:
                (status,) = self.rpc.get_signature_statuses([signature])
            except RpcError:
                status = None
            if status is not None:
                if status.get("err") is not None:
                    raise SwapError(f"swap {signature} failed on chain: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            self._sleep(1.0)
        raise SwapError(f"swap {signature} not confirmed within {self.confirm_timeout_s:.0f}s: check the wallet")

