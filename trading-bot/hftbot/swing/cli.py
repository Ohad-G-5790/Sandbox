"""`hftbot swing ...`: download history, run the out-of-sample test, paper or live trade, inspect state."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from hftbot.swing.candles import align, resample
from hftbot.swing.research import format_report, walk_forward
from hftbot.swing.trader import CandleStore, PaperExecutor, SwingConfig, SwingTrader, load_state, load_swing_config, save_state

DEFAULT_DATA = Path(os.environ.get("HFTBOT_DATA", "swing-data"))


def add_commands(sub: Any) -> dict[str, Any]:
    swing = sub.add_parser("swing", help="mid-frequency trading of SOL/BTC/ETH/JUP against USDC via Jupiter (hours to days)")
    commands = swing.add_subparsers(dest="swing_command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", type=Path, help="TOML with a [swing] table (see swing.toml)")
        p.add_argument("--data", type=Path, default=DEFAULT_DATA, help="folder for prices, state and journals (or set HFTBOT_DATA)")
        p.add_argument("--source", choices=("binance", "coinbase"), default=os.environ.get("HFTBOT_SWING_SOURCE"), help="price source; overrides the config (or set HFTBOT_SWING_SOURCE). Use coinbase on US servers")

    p = commands.add_parser("download", help="download or update hourly price history")
    common(p)
    p = commands.add_parser("test", help="out-of-sample test of every strategy against holding, after fees")
    common(p)
    p.add_argument("--synthetic", action="store_true", help="run on made-up prices (to see how the report reads)")
    p = commands.add_parser("run", help="trade every bar: paper unless --live")
    common(p)
    p.add_argument("--live", action="store_true", help="swap real tokens from the wallet in $HFTBOT_KEYPAIR or $HFTBOT_KEYPAIR_JSON")
    p.add_argument("--confirm-wallet", default=os.environ.get("HFTBOT_CONFIRM_WALLET"), help="required with --live: the wallet's public key")
    p.add_argument("--rpc", default=os.environ.get("HFTBOT_RPC_URL", "https://api.mainnet-beta.solana.com"))
    p.add_argument("--no-quotes", action="store_true", help="paper mode: price fills with the cost model instead of live Jupiter quotes")
    p = commands.add_parser("status", help="show the latest state and test report")
    common(p)
    p = commands.add_parser("reset-kill", help="re-arm after the drawdown kill switch tripped (a human decision)")
    common(p)
    p.add_argument("--live", action="store_true")

    def dispatch(args: argparse.Namespace) -> int:
        return {"download": _download, "test": _test, "run": _run, "status": _status, "reset-kill": _reset_kill}[args.swing_command](args)

    return {"swing": dispatch}


def _config(args: argparse.Namespace) -> SwingConfig:
    cfg = load_swing_config(args.config)
    return dataclasses.replace(cfg, source=args.source) if args.source else cfg


def _store(args: argparse.Namespace) -> CandleStore:
    return CandleStore(args.data / "candles", _config(args))


def _download(args: argparse.Namespace) -> int:
    store = _store(args)
    for coin in store.cfg.coins:
        candles = store.update(coin)
        if candles:
            first, last = (time.strftime("%Y-%m-%d %H:%M", time.gmtime(c.ts)) for c in (candles[0], candles[-1]))
            print(f"{coin}: {len(candles):,} hourly candles, {first} to {last} UTC")
        else:
            print(f"{coin}: no data from {store.cfg.source}")
    return 0


def _test(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if args.synthetic:
        from hftbot.swing.synthetic import synthetic_hourly

        hourly = synthetic_hourly(cfg.coins)
        panel = align({c: resample(v, cfg.bar_hours) for c, v in hourly.items()}, cfg.bar_hours)
        print("MADE-UP PRICES: this shows how the report reads, nothing about real markets.\n")
    else:
        panel = CandleStore(args.data / "candles", cfg).panel(time.time(), update=False)
    print(format_report(walk_forward(panel, cfg.costs, cfg.folds)))
    return 0


def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = _config(args)
    args.data.mkdir(parents=True, exist_ok=True)
    store = CandleStore(args.data / "candles", cfg)
    from hftbot.solana.jupiter import JupiterClient

    jupiter = JupiterClient(api_key=os.environ.get("JUPITER_API_KEY"))
    if args.live:
        from hftbot.solana.keys import load_keypair
        from hftbot.solana.rpc import SolanaRpc
        from hftbot.swing.live import JupiterSwapExecutor

        keypair = load_keypair()
        if args.confirm_wallet != str(keypair.pubkey()):
            print(f"--live swaps real tokens from {keypair.pubkey()}. Pass --confirm-wallet {keypair.pubkey()} (or set HFTBOT_CONFIRM_WALLET).", file=sys.stderr)
            return 2
        executor: Any = JupiterSwapExecutor(
            SolanaRpc(args.rpc, timeout_s=10.0),
            jupiter,
            keypair,
            cfg.coins,
            sol_fee_reserve=cfg.sol_fee_reserve,
            max_price_deviation_pct=cfg.max_price_deviation_pct,
            slippage_bps=cfg.swap_slippage_bps,
        )
        executor.verify_tokens()
        state_path = args.data / "swing_state_live.json"
        print(f"LIVE: swapping real tokens from {keypair.pubkey()}")
    else:
        state_path = args.data / "swing_state_paper.json"
        state = load_state(state_path)
        book = state.get("paper") or {"usdc": cfg.paper_cash, "units": {}}

        def quote(in_mint: str, out_mint: str, amount: int) -> tuple[int, int]:
            q = jupiter.quote(in_mint, out_mint, amount, cfg.swap_slippage_bps)
            return q.in_amount, q.out_amount

        executor = PaperExecutor(book, cfg.costs, cfg.coins, None if args.no_quotes else quote)
        print(f"PAPER TRADING {', '.join(cfg.coins)} with {book['usdc']:.2f} USDC simulated: no real swaps")

    trader = SwingTrader(cfg, store, executor, state_path, journal_path=args.data / f"swing_journal_{executor.mode}.jsonl")
    stop = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())  # hosts stop services with SIGTERM: finish cleanly between bars
    try:
        trader.run_forever(should_stop=stop.is_set, sleep=stop.wait)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
    print("stopped")
    return 0


def _status(args: argparse.Namespace) -> int:
    for mode in ("paper", "live"):
        state = load_state(args.data / f"swing_state_{mode}.json")
        if not state:
            continue
        print(f"[{mode}] strategy: {state.get('pick') or 'none (staying in USDC)'}; peak equity {state.get('peak_equity', 0):.2f}; kill switch: {state.get('killed') or 'off'}")
        if mode == "paper" and state.get("paper"):
            print(f"        paper book: {state['paper']}")
    report = args.data / "report.txt"
    if report.exists():
        print("\n" + report.read_text())
    return 0


def _reset_kill(args: argparse.Namespace) -> int:
    path = args.data / f"swing_state_{'live' if args.live else 'paper'}.json"
    state = load_state(path)
    if not state.get("killed"):
        print("kill switch is not tripped")
        return 0
    print(f"re-arming after: {state['killed']}")
    state["killed"] = None
    state.pop("peak_equity", None)  # measure drawdown from the next bar, not from the old peak
    save_state(path, state)
    return 0

