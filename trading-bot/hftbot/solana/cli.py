"""Solana commands: market-info, record, run (paper by default, --live for real orders), claim-seat."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import signal
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hftbot.config import BotConfig, load_config
from hftbot.execution import PaperVenue
from hftbot.features import StateEngine
from hftbot.journal import Journal, block_to_dict
from hftbot.live import FeedError, run_live
from hftbot.loop import StepRecord, TradingLoop
from hftbot.market import Block
from hftbot.portfolio import Portfolio
from hftbot.risk import RiskEngine
from hftbot.rules import RulesEngine
from hftbot.solana.exit import BASE_FEE_LAMPORTS, LAMPORTS_PER_SOL
from hftbot.solana.phoenix import SOL_USDC_DEVNET, SOL_USDC_MAINNET, STATUS, parse_market
from hftbot.solana.rpc import PUBLIC_MAINNET_RPC, RpcError, SolanaRpc, b58encode

RPC_ENV = "HFTBOT_RPC_URL"
log = logging.getLogger("hftbot")


def add_commands(sub: Any) -> dict[str, Callable[[argparse.Namespace], int]]:
    def connection(p: argparse.ArgumentParser) -> None:
        p.add_argument("--rpc", default=os.environ.get(RPC_ENV, PUBLIC_MAINNET_RPC), help=f"Solana RPC URL (or set {RPC_ENV}); use a private one")
        p.add_argument("--market", default=SOL_USDC_MAINNET, help=f"Phoenix market (default SOL/USDC mainnet; devnet SOL/USDC is {SOL_USDC_DEVNET})")

    p = sub.add_parser("market-info", help="read a Phoenix market and print its parameters and top of book")
    connection(p)

    p = sub.add_parser("record", help="record live Phoenix blocks (book + trade prints) to a JSONL file for backtests")
    connection(p)
    p.add_argument("out", type=Path)
    p.add_argument("--minutes", type=float, help="stop after this long (default: until Ctrl+C)")
    p.add_argument("--levels", type=int, default=10)
    p.add_argument("--no-trades", action="store_true", help="book only: skip fetching transactions (far fewer RPC calls)")

    p = sub.add_parser("run", help="run the bot on live Phoenix data: paper fills unless --live")
    connection(p)
    p.add_argument("--config", type=Path, help="TOML config (see config.solana.toml)")
    p.add_argument("--journal", type=Path)
    p.add_argument("--minutes", type=float)
    p.add_argument("--engine", choices=("rules", "jev"), default="rules")
    p.add_argument("--live", action="store_true", help="send real orders from the wallet in $HFTBOT_KEYPAIR or $HFTBOT_KEYPAIR_JSON")
    p.add_argument("--confirm-wallet", default=os.environ.get("HFTBOT_CONFIRM_WALLET"), help="required with --live (or set HFTBOT_CONFIRM_WALLET): the wallet's public key, to confirm which money is at risk")

    p = sub.add_parser("claim-seat", help="claim a Phoenix trading seat for the wallet in $HFTBOT_KEYPAIR (one time, ~0.004 SOL refundable)")
    connection(p)
    p.add_argument("--confirm-wallet", default=os.environ.get("HFTBOT_CONFIRM_WALLET"), help="the wallet's public key (or set HFTBOT_CONFIRM_WALLET)")

    return {"market-info": _market_info, "record": _record, "run": _run, "claim-seat": _claim_seat}


def _market_info(args: argparse.Namespace) -> int:
    rpc = SolanaRpc(args.rpc, timeout_s=15.0)
    slot, data = rpc.get_account_data(args.market)
    market = parse_market(data)
    h = market.header
    book = market.book(slot, time.time(), levels=5)
    print(f"market      {args.market} (slot {slot}, {len(data):,} bytes)")
    print(f"status      {STATUS.get(h.status, h.status)}")
    print(f"base        {b58encode(h.base_mint)} ({h.base_decimals} decimals), lot = {h.lot_units:g}")
    print(f"quote       {b58encode(h.quote_mint)} ({h.quote_decimals} decimals), tick = {h.tick_price:g}")
    print(f"taker fee   {h.taker_fee_bps} bps (makers pay none)")
    print(f"capacity    {h.bids_size} bids, {h.asks_size} asks, {h.num_seats} seats; {len(market.traders or {})} traders seated")
    print(f"book        {len(market.bids)} bids, {len(market.asks)} asks resting; spread {book.spread_bps:.2f} bps")
    for bid, ask in zip(book.bids, book.asks):
        print(f"    {bid.size:>12.3f} @ {bid.price:<12g} | {ask.price:>12g} @ {ask.size:<12.3f}")
    return 0


def _record(args: argparse.Namespace) -> int:
    from hftbot.solana.feed import PhoenixFeed

    rpc = SolanaRpc(args.rpc)
    feed = PhoenixFeed(rpc, args.market, levels=args.levels, fetch_trades=not args.no_trades)
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    written = errors = 0
    with open(args.out, "a", encoding="utf-8") as out:
        try:
            while deadline is None or time.monotonic() < deadline:
                try:
                    block = feed.next_block()
                except FeedError as error:
                    errors += 1
                    print(f"feed error ({errors}): {error}", file=sys.stderr)
                    continue
                out.write(json.dumps(block_to_dict(block), separators=(",", ":")) + "\n")
                written += 1
                if written % 100 == 0:
                    out.flush()
                    print(f"{written:,} blocks, mid {block.book.mid:g}, spread {block.book.spread_bps:.2f} bps, {len(block.trades)} prints in the last block")
        except KeyboardInterrupt:
            pass
    print(f"recorded {written:,} blocks to {args.out} ({errors} feed errors)")
    return 0


def _run(args: argparse.Namespace) -> int:
    from hftbot.solana.feed import PhoenixFeed

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config) if args.config else BotConfig()
    rpc = SolanaRpc(args.rpc)
    keypair = None
    if args.live:
        from hftbot.solana.keys import load_keypair

        keypair = load_keypair()
        if args.confirm_wallet != str(keypair.pubkey()):
            print(f"--live trades real money from {keypair.pubkey()}. Pass --confirm-wallet {keypair.pubkey()} to proceed.", file=sys.stderr)
            return 2
    try:
        feed = PhoenixFeed(rpc, args.market, trader=str(keypair.pubkey()) if keypair else None, poll_interval_s=cfg.loop.block_interval_s)
    except (RpcError, FeedError, ValueError) as error:
        print(f"cannot read market {args.market} via {args.rpc}: {error}", file=sys.stderr)
        return 1
    h = feed.header
    # Quotes must sit on the market's own grid, or every comparison with resting orders looks like a change.
    cfg = dataclasses.replace(cfg, pricing=dataclasses.replace(cfg.pricing, tick_size=h.tick_price, lot_size=h.lot_units))
    mid = feed.state.book(0, time.time()).mid
    fee = (BASE_FEE_LAMPORTS + cfg.live.compute_unit_limit * cfg.live.priority_micro_lamports / 1e6) / LAMPORTS_PER_SOL * mid

    engine = _engine(args.engine, cfg)
    fallback = RulesEngine(cfg.risk.max_position, cfg.risk.max_inventory_age_s) if args.engine == "jev" else None
    if keypair is None:
        venue: Any = PaperVenue(dataclasses.replace(cfg.costs, gas_per_tx=fee, maker_fee_bps=0.0, taker_fee_bps=float(h.taker_fee_bps)), cfg.activation_delay_blocks)
        print(f"PAPER TRADING on live data from {args.market}: no orders are sent")
    else:
        venue = _live_venue(rpc, feed, keypair, cfg)
        if venue is None:
            return 2

    loop = TradingLoop(
        engine=engine,
        fallback=fallback,
        venue=venue,
        portfolio=Portfolio(cfg.starting_cash),
        risk=RiskEngine(cfg.risk),
        state=StateEngine(cfg.features),
        policy=cfg.policy,
        pricing=cfg.pricing,
        loop=cfg.loop,
    )
    if keypair is not None:
        venue.on_error = loop.risk.record_api_error
        leftovers = feed.our_orders()
        if leftovers:  # a previous run died without cancelling (crash, out of memory, host restart)
            print(f"cancelling {len(leftovers)} orders left by a previous run")
            venue.cancel_all(Block(0, time.time(), feed.state.book(feed.slot, time.time())))

    # Cloud hosts stop a service with SIGTERM: turn it into the same clean stop as Ctrl+C, so resting
    # orders are cancelled before the process exits.
    previous_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    journal = Journal(args.journal, append=True) if args.journal else None
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    count = 0

    def on_step(record: StepRecord) -> None:
        nonlocal count
        count += 1
        if journal:
            journal.write(record)
        if count % 50 == 0 or record.fills:
            p = loop.portfolio
            print(f"block {record.block}: mid {record.mid:g} {record.ladder.value:<14} position {p.position:+.3f} pnl {p.pnl:+.4f} fills {p.fill_count} gas {p.gas:.4f}")

    try:
        run_live(feed, loop, on_step=on_step, should_stop=lambda: deadline is not None and time.monotonic() > deadline)
    except KeyboardInterrupt:
        print("stopping: resting orders were cancelled")
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        if journal:
            journal.close()
    p = loop.portfolio
    print(f"done: {count} blocks, pnl {p.pnl:+.4f} (fees {p.fees:.4f}, gas {p.gas:.4f}), {p.fill_count} fills, position {p.position:+.3f}")
    if loop.risk.killed:
        print(f"KILL SWITCH: {loop.risk.kill_reason}")
    return 0


def _raise_keyboard_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def _engine(name: str, cfg: BotConfig) -> Any:
    if name == "jev":
        from hftbot.jev import JevEngine

        return JevEngine(model=cfg.jev_model, pinned_model=cfg.jev_pinned_model)
    return RulesEngine(cfg.risk.max_position, cfg.risk.max_inventory_age_s)


def _live_venue(rpc: SolanaRpc, feed: Any, keypair: Any, cfg: BotConfig) -> Any:
    from solders.pubkey import Pubkey

    from hftbot.solana.exit import JupiterExit, TokenPair
    from hftbot.solana.jupiter import JupiterClient
    from hftbot.solana.phoenix import TraderAccounts
    from hftbot.solana.venue import PhoenixVenue

    h = feed.header
    accounts = TraderAccounts.derive(Pubkey.from_string(feed.market), keypair.pubkey(), h)
    problems = []
    if not _seat_approved(rpc, accounts.seat):
        problems.append("no approved seat on this market: run `hftbot claim-seat` first")
    balances = {}
    for name, account, decimals in (("base", accounts.base_account, h.base_decimals), ("quote", accounts.quote_account, h.quote_decimals)):
        try:
            balances[name] = rpc.get_token_balance(str(account)) / 10**decimals
        except RpcError:
            problems.append(f"no {name} token account {account}: create and fund it (wrapped SOL for SOL)")
    lamports = rpc.get_balance(str(keypair.pubkey()))
    if lamports < 0.05 * LAMPORTS_PER_SOL:
        problems.append(f"only {lamports / LAMPORTS_PER_SOL:.4f} SOL for fees: keep at least 0.05")
    print(f"wallet {keypair.pubkey()}  base {balances.get('base', '?')}  quote {balances.get('quote', '?')}  SOL for fees {lamports / LAMPORTS_PER_SOL:.4f}")
    print(f"order size {cfg.pricing.order_size}, max position {cfg.risk.max_position}, max daily loss {cfg.risk.max_daily_loss}, max drawdown {cfg.risk.max_drawdown}")
    if problems:
        for problem in problems:
            print(f"cannot go live: {problem}", file=sys.stderr)
        return None
    pair = TokenPair(b58encode(h.base_mint), b58encode(h.quote_mint), h.base_decimals, h.quote_decimals)
    exit = JupiterExit(
        JupiterClient(api_key=os.environ.get("JUPITER_API_KEY")),
        rpc,
        keypair,
        pair,
        slippage_bps=cfg.live.exit_slippage_bps,
        max_price_impact_pct=cfg.live.exit_max_impact_pct,
        max_priority_lamports=cfg.live.exit_max_priority_lamports,
    )
    print("LIVE: sending real orders")
    return PhoenixVenue(rpc, feed, keypair, exit, cfg.live)


def _seat_approved(rpc: SolanaRpc, seat: Any) -> bool:
    try:
        _, data = rpc.get_account_data(str(seat))
    except RpcError:
        return False
    return len(data) >= 80 and struct.unpack_from("<Q", data, 72)[0] == 1


def _claim_seat(args: argparse.Namespace) -> int:
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    from hftbot.solana.keys import load_keypair
    from hftbot.solana.phoenix import claim_seat_ix, seat_address

    keypair = load_keypair()
    if args.confirm_wallet != str(keypair.pubkey()):
        print(f"--confirm-wallet must be {keypair.pubkey()}", file=sys.stderr)
        return 2
    rpc = SolanaRpc(args.rpc, timeout_s=15.0)
    market = Pubkey.from_string(args.market)
    seat = seat_address(market, keypair.pubkey())
    if _seat_approved(rpc, seat):
        print(f"seat {seat} is already approved")
        return 0
    blockhash_text, _ = rpc.get_latest_blockhash()
    from solders.hash import Hash

    blockhash = Hash.from_string(blockhash_text)
    tx = Transaction([keypair], Message.new_with_blockhash([claim_seat_ix(market, keypair.pubkey())], keypair.pubkey(), blockhash), blockhash)
    signature = rpc.send_transaction(bytes(tx), skip_preflight=False)
    print(f"sent {signature}; waiting for confirmation")
    for _ in range(60):
        (status,) = rpc.get_signature_statuses([signature])
        if status and status.get("err") is not None:
            print(f"claim failed: {status['err']}", file=sys.stderr)
            return 1
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            break
        time.sleep(0.5)
    approved = _seat_approved(rpc, seat)
    print(f"seat {seat}: {'approved' if approved else 'NOT approved yet, check the transaction'}")
    return 0 if approved else 1

