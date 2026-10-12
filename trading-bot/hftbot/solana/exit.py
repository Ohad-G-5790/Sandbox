"""Flattening through Jupiter when the kill switch trips.

The swap is confirmed before it counts: a fill is reported only once the transaction has landed
without error. Anything else is an error receipt, and the loop retries on the next block.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from hftbot.execution import TxReceipt
from hftbot.market import Block, Side
from hftbot.portfolio import Fill
from hftbot.solana.jupiter import JupiterClient, JupiterError, sign_versioned_transaction
from hftbot.solana.rpc import RpcError, SolanaRpc

LAMPORTS_PER_SOL = 1_000_000_000
BASE_FEE_LAMPORTS = 5_000


@dataclass(frozen=True)
class TokenPair:
    base_mint: str
    quote_mint: str
    base_decimals: int
    quote_decimals: int


class JupiterExit:
    def __init__(
        self,
        jupiter: JupiterClient,
        rpc: SolanaRpc,
        keypair: Any,
        pair: TokenPair,
        *,
        slippage_bps: int = 100,
        max_price_impact_pct: float = 1.0,
        max_priority_lamports: int = 2_000_000,
        confirm_timeout_s: float = 30.0,
        sleep=time.sleep,
    ) -> None:
        self.jupiter, self.rpc, self.keypair, self.pair = jupiter, rpc, keypair, pair
        self.slippage_bps = slippage_bps
        self.max_price_impact_pct = max_price_impact_pct
        self.max_priority_lamports = max_priority_lamports
        self.confirm_timeout_s = confirm_timeout_s
        self._sleep = sleep

    def market_order(self, side: Side, size: float, block: Block) -> TxReceipt:
        p = self.pair
        if side is Side.SELL:
            input_mint, output_mint, amount = p.base_mint, p.quote_mint, round(size * 10**p.base_decimals)
        else:  # spend enough quote to buy `size` base at the ask plus the slippage allowance
            spend = size * block.book.best_ask * (1 + self.slippage_bps / 1e4)
            input_mint, output_mint, amount = p.quote_mint, p.base_mint, round(spend * 10**p.quote_decimals)
        # Fees are charged in SOL; in quote currency they are worth lamports * (SOL price).
        gas_cap = (BASE_FEE_LAMPORTS + self.max_priority_lamports) / LAMPORTS_PER_SOL * block.book.mid
        try:
            quote = self.jupiter.quote(input_mint, output_mint, amount, self.slippage_bps)
            if side is Side.SELL:
                base, quote_amount = quote.in_amount / 10**p.base_decimals, quote.out_amount / 10**p.quote_decimals
            else:
                base, quote_amount = quote.out_amount / 10**p.base_decimals, quote.in_amount / 10**p.quote_decimals
            if base <= 0:
                return TxReceipt(gas=0.0, error="empty quote")
            price = quote_amount / base
            # Measured against our own book rather than trusting the API's impact field.
            impact_pct = side.sign * (price / block.book.mid - 1) * 100
            if impact_pct > self.max_price_impact_pct:
                return TxReceipt(gas=0.0, error=f"swap price {price:.6g} is {impact_pct:.2f}% worse than mid: above the {self.max_price_impact_pct}% cap")
            unsigned = self.jupiter.swap_transaction(quote, str(self.keypair.pubkey()), self.max_priority_lamports)
            signature = self.rpc.send_transaction(sign_versioned_transaction(unsigned, self.keypair))
            failure = self._confirm(signature)
        except (JupiterError, RpcError) as error:
            return TxReceipt(gas=0.0, error=str(error))
        if failure:
            return TxReceipt(gas=gas_cap, error=failure)
        # Quoted amounts: the landed swap can differ within the slippage cap. Reconcile balances after a kill.
        fill = Fill(0, side, price, base, 0.0, block.ts, block.number, maker=False)
        return TxReceipt(gas=gas_cap, fills=(fill,))

    def _confirm(self, signature: str) -> str | None:
        """None once confirmed without error; otherwise why not."""
        deadline = time.monotonic() + self.confirm_timeout_s
        while time.monotonic() < deadline:
            (status,) = self.rpc.get_signature_statuses([signature])
            if status is not None:
                if status.get("err") is not None:
                    return f"swap {signature} failed: {status['err']}"
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return None
            self._sleep(0.5)
        return f"swap {signature} not confirmed within {self.confirm_timeout_s:.0f}s"
