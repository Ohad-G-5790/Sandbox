"""PhoenixVenue: real post-only quotes on Phoenix, Jupiter for the emergency exit.

Every requote is one transaction: compute budget, CancelAllOrders, PlaceMultiplePostOnlyOrders. Sends
are fire and forget; until the book shows them (or `pending_slots` pass), the quotes just sent count as
resting, so the loop does not resend them every slot. Failed transactions are found off the hot path
and reported to the risk engine as API errors.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hftbot.execution import RestingOrder, TxReceipt, VenueStats
from hftbot.market import Block, Side
from hftbot.portfolio import Fill
from hftbot.pricing import Quote
from hftbot.solana.exit import BASE_FEE_LAMPORTS, LAMPORTS_PER_SOL, JupiterExit
from hftbot.solana.feed import PhoenixFeed
from hftbot.solana.jupiter import SOL_MINT
from hftbot.solana.phoenix import TraderAccounts, cancel_all_orders_ix, place_post_only_orders_ix, withdraw_funds_ix
from hftbot.solana.rpc import RpcError, SolanaRpc, b58decode

log = logging.getLogger("hftbot")


@dataclass(frozen=True)
class LiveConfig:
    compute_unit_limit: int = 200_000
    priority_micro_lamports: int = 20_000
    """Compute unit price. Total priority fee = limit * price / 1e6 lamports per transaction."""
    pending_slots: int = 4
    blockhash_max_age_s: float = 20.0
    status_check_every: int = 5
    sol_price: float | None = None
    """SOL price in quote currency, for fee accounting on markets whose base is not SOL."""
    exit_slippage_bps: int = 100
    exit_max_impact_pct: float = 1.0
    exit_max_priority_lamports: int = 2_000_000


class PhoenixVenue:
    def __init__(
        self,
        rpc: SolanaRpc,
        feed: PhoenixFeed,
        keypair: Any,
        exit: JupiterExit,
        cfg: LiveConfig | None = None,
        on_error: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        from solders.pubkey import Pubkey

        self.rpc, self.feed, self.keypair, self.exit = rpc, feed, keypair, exit
        self.cfg = cfg or LiveConfig()
        self.on_error = on_error or (lambda ts: None)
        self._clock, self._sleep = clock, sleep
        self.accounts = TraderAccounts.derive(Pubkey.from_string(feed.market), keypair.pubkey(), feed.header)
        self.stats = VenueStats()
        self._pending: tuple[tuple[Quote, ...], int] | None = None
        self._unconfirmed: dict[str, int] = {}
        self._blockhash: tuple[Any, float] | None = None
        self._matches = 0
        self._base_is_sol = feed.header.base_mint == b58decode(SOL_MINT)

    def open_orders(self) -> list[RestingOrder]:
        if self._pending is not None:
            quotes, sent_slot = self._pending
            if self.feed.slot <= sent_slot + self.cfg.pending_slots and not self._landed(quotes):
                return [RestingOrder(0, q.side, q.price, q.size, q.size, -1, 0.0) for q in quotes]
            self._pending = None
        h = self.feed.header
        return [
            RestingOrder(o.sequence_number, o.side, h.ticks_to_price(o.price_in_ticks), h.lots_to_units(o.base_lots), h.lots_to_units(o.base_lots), -1, 0.0)
            for o in self.feed.our_orders()
        ]

    def match(self, block: Block) -> list[Fill]:
        self._matches += 1
        if self._matches % self.cfg.status_check_every == 0:
            self._check_sent(block.ts)
        fills = self.feed.drain_our_fills()
        for f in fills:
            if f.order_id not in self.stats._filled_ids:
                self.stats._filled_ids.add(f.order_id)
                self.stats.orders_filled += 1
        return fills

    def replace_quotes(self, quotes: list[Quote], block: Block) -> TxReceipt:
        h = self.feed.header
        bids = [(h.price_to_ticks(q.price, Side.BUY), h.units_to_lots(q.size)) for q in quotes if q.side is Side.BUY]
        asks = [(h.price_to_ticks(q.price, Side.SELL), h.units_to_lots(q.size)) for q in quotes if q.side is Side.SELL]
        bids = [(p, lots) for p, lots in bids if p > 0 and lots > 0]
        asks = [(p, lots) for p, lots in asks if p > 0 and lots > 0]
        instructions = [cancel_all_orders_ix(self.accounts)]
        if bids or asks:
            instructions.append(place_post_only_orders_ix(self.accounts, bids, asks))
        receipt = self._send(instructions, block)
        if receipt.error is None:
            self._pending = (tuple(quotes), self.feed.slot)
            self.stats.orders_posted += len(bids) + len(asks)
        return receipt

    def cancel_all(self, block: Block) -> TxReceipt:
        if not self.feed.our_orders() and not (self._pending and self._pending[0]):
            return TxReceipt(gas=0.0)
        receipt = self._send([cancel_all_orders_ix(self.accounts)], block)
        if receipt.error is None:
            self._pending = ((), self.feed.slot)
        return receipt

    def market_order(self, side: Side, size: float, block: Block) -> TxReceipt:
        """Flatten: pull everything out of the market into the wallet, wait for it, then swap via Jupiter."""
        withdraw = self._send([cancel_all_orders_ix(self.accounts), withdraw_funds_ix(self.accounts)], block)
        if withdraw.error is None and withdraw.signature is not None:
            failure = self._confirm(withdraw.signature)
            if failure:  # the wallet may already hold enough: try the swap anyway
                log.warning("withdraw before flatten: %s", failure)
        self._pending = ((), self.feed.slot)
        swap = self.exit.market_order(side, size, block)
        return TxReceipt(gas=withdraw.gas + swap.gas, fills=swap.fills, error=swap.error)

    def fee_in_quote(self, block: Block) -> float:
        lamports = BASE_FEE_LAMPORTS + self.cfg.compute_unit_limit * self.cfg.priority_micro_lamports / 1e6
        sol_price = block.book.mid if self._base_is_sol else self.cfg.sol_price
        if sol_price is None:
            raise ValueError("set [live] sol_price: the market's base is not SOL, so fees cannot be valued")
        return lamports / LAMPORTS_PER_SOL * sol_price

    def _send(self, instructions: list[Any], block: Block) -> TxReceipt:
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        from solders.message import Message
        from solders.transaction import Transaction

        try:
            blockhash = self._recent_blockhash()
            budget = [set_compute_unit_limit(self.cfg.compute_unit_limit), set_compute_unit_price(self.cfg.priority_micro_lamports)]
            message = Message.new_with_blockhash(budget + instructions, self.keypair.pubkey(), blockhash)
            signature = self.rpc.send_transaction(bytes(Transaction([self.keypair], message, blockhash)))
        except RpcError as error:
            return TxReceipt(gas=0.0, error=str(error))
        self.stats.txs += 1
        self._unconfirmed[signature] = self.feed.slot
        return TxReceipt(gas=self.fee_in_quote(block), signature=signature)

    def _confirm(self, signature: str, timeout_s: float = 30.0) -> str | None:
        deadline = self._clock() + timeout_s
        while True:
            try:
                (status,) = self.rpc.get_signature_statuses([signature])
            except RpcError as error:
                status = None
                log.warning("status check: %s", error)
            if status is not None:
                self._unconfirmed.pop(signature, None)
                if status.get("err") is not None:
                    return f"{signature} failed: {status['err']}"
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return None
            if self._clock() > deadline:
                return f"{signature} not confirmed within {timeout_s:.0f}s"
            self._sleep(0.5)

    def _recent_blockhash(self) -> Any:
        from solders.hash import Hash

        now = self._clock()
        if self._blockhash is None or now - self._blockhash[1] > self.cfg.blockhash_max_age_s:
            text, _ = self.rpc.get_latest_blockhash()
            self._blockhash = (Hash.from_string(text), now)
        return self._blockhash[0]

    def _landed(self, quotes: tuple[Quote, ...]) -> bool:
        h = self.feed.header
        want = sorted((q.side.value, h.price_to_ticks(q.price, q.side)) for q in quotes)
        have = sorted((o.side.value, o.price_in_ticks) for o in self.feed.our_orders())
        return want == have

    def _check_sent(self, now: float) -> None:
        """Off the hot path: transactions that failed on chain are API errors; old unknown ones are dropped."""
        ready = [sig for sig, slot in self._unconfirmed.items() if self.feed.slot - slot >= 2]
        if not ready:
            return
        try:
            statuses = self.rpc.get_signature_statuses(ready[:256])
        except RpcError as error:
            log.warning("could not check transaction statuses: %s", error)
            return
        for signature, status in zip(ready, statuses):
            sent_slot = self._unconfirmed[signature]
            if status is None:
                if self.feed.slot - sent_slot > 150:  # past blockhash expiry: it will never land
                    del self._unconfirmed[signature]
                    log.warning("transaction %s was dropped", signature)
                continue
            del self._unconfirmed[signature]
            if status.get("err") is not None:
                self.stats.orders_rejected += 1
                log.warning("transaction %s failed: %s", signature, status["err"])
                self.on_error(now)
