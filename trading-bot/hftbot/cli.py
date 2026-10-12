"""Command line: simulate, backtest, compare engines, break-even, calibration, Jev check, and live Solana commands."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from hftbot.backtest import SimulatedLatency, compare, format_table, run_backtest
from hftbot.calibration import calibration_report
from hftbot.config import BotConfig, load_config
from hftbot.costs import BreakevenInputs, breakeven
from hftbot.journal import Journal, read_blocks, read_jsonl, write_blocks
from hftbot.judgments import DecisionEngine
from hftbot.market import Block
from hftbot.rules import AlwaysQuoteEngine, RulesEngine
from hftbot.sim import SimConfig, simulate
from hftbot.solana import cli as solana_cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hftbot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def data_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--data", type=Path, help="blocks JSONL file (default: synthetic market)")
        p.add_argument("--blocks", type=int, default=20_000, help="synthetic blocks to generate")
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--config", type=Path, help="TOML config (see config.example.toml)")

    p = sub.add_parser("simulate", help="write a synthetic market to a blocks JSONL file")
    p.add_argument("out", type=Path)
    p.add_argument("--blocks", type=int, default=20_000)
    p.add_argument("--seed", type=int, default=7)

    p = sub.add_parser("backtest", help="run one engine and print its metrics")
    data_args(p)
    p.add_argument("--engine", choices=("rules", "always-quote", "jev"), default="rules")
    p.add_argument("--journal", type=Path, help="write every block's snapshot, decision and action here")
    p.add_argument("--latency-ms", type=float, default=90.0, help="simulated median decision latency for non-Jev engines")

    p = sub.add_parser("compare", help="run every engine on identical data, costs and limits")
    data_args(p)
    p.add_argument("--with-jev", action="store_true", help="include live Jev (needs TYPESAFE_API_KEY; calls the API every block)")

    p = sub.add_parser("breakeven", help="the half spread needed to cover fees, adverse selection and gas")
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--order-size", type=float, required=True, help="per side, base units")
    p.add_argument("--fill-rate", type=float, required=True, help="fraction of one order filled per block")
    p.add_argument("--gas-per-tx", type=float, required=True, help="quote currency")
    p.add_argument("--maker-fee-bps", type=float, default=0.0)
    p.add_argument("--adverse-bps", type=float, default=0.0)
    p.add_argument("--requote-every", type=float, default=1.0, help="blocks between requotes")
    p.add_argument("--block-interval", type=float, default=0.3)

    p = sub.add_parser("calibrate", help="score logged probabilities against what happened next")
    p.add_argument("journal", type=Path)
    p.add_argument("--horizon", type=int, default=10, help="blocks ahead (match the direction question)")

    sub.add_parser("jev-check", help="list available Jev models and time one live battery call")
    solana_commands = solana_cli.add_commands(sub)

    args = parser.parse_args(argv)
    return {
        "simulate": _simulate,
        "backtest": _backtest,
        "compare": _compare,
        "breakeven": _breakeven,
        "calibrate": _calibrate,
        "jev-check": _jev_check,
        **solana_commands,
    }[args.command](args)


def _load(args: argparse.Namespace) -> tuple[list[Block], BotConfig]:
    cfg = load_config(args.config) if args.config else BotConfig()
    blocks = list(read_blocks(args.data)) if args.data else simulate(SimConfig(blocks=args.blocks, seed=args.seed)).blocks
    return blocks, cfg


def _engines(cfg: BotConfig, latency_ms: float, with_jev: bool) -> dict[str, Callable[[], DecisionEngine]]:
    engines: dict[str, Callable[[], DecisionEngine]] = {
        "always-quote": lambda: SimulatedLatency(AlwaysQuoteEngine(), median_ms=latency_ms, seed=1),
        "rules": lambda: SimulatedLatency(RulesEngine(cfg.risk.max_position, cfg.risk.max_inventory_age_s), median_ms=latency_ms, seed=1),
    }
    if with_jev:
        from hftbot.jev import JevEngine

        engines["jev"] = lambda: JevEngine(model=cfg.jev_model, pinned_model=cfg.jev_pinned_model)
    return engines


def _simulate(args: argparse.Namespace) -> int:
    write_blocks(args.out, simulate(SimConfig(blocks=args.blocks, seed=args.seed)).blocks)
    print(f"wrote {args.blocks:,} blocks to {args.out}")
    return 0


def _backtest(args: argparse.Namespace) -> int:
    blocks, cfg = _load(args)
    engine = _engines(cfg, args.latency_ms, args.engine == "jev")[args.engine]()
    fallback = RulesEngine(cfg.risk.max_position, cfg.risk.max_inventory_age_s) if args.engine == "jev" else None
    if args.journal:
        with Journal(args.journal) as journal:
            result = run_backtest(blocks, engine, cfg, fallback=fallback, on_step=journal.write, name=args.engine)
        print(f"journal: {args.journal}")
    else:
        result = run_backtest(blocks, engine, cfg, fallback=fallback, name=args.engine)
    print(format_table([result]))
    return 0


def _compare(args: argparse.Namespace) -> int:
    blocks, cfg = _load(args)
    print(format_table(compare(blocks, _engines(cfg, 90.0, args.with_jev), cfg)))
    return 0


def _breakeven(args: argparse.Namespace) -> int:
    b = breakeven(
        BreakevenInputs(
            price=args.price,
            order_size=args.order_size,
            fill_rate=args.fill_rate,
            gas_per_tx=args.gas_per_tx,
            maker_fee_bps=args.maker_fee_bps,
            adverse_selection_bps=args.adverse_bps,
            requote_every_blocks=args.requote_every,
            block_interval_s=args.block_interval,
        )
    )
    print(f"gas per block:          {b.gas_per_block:.6f}")
    print(f"gas per hour:           {b.gas_per_hour:,.2f}")
    print(f"gas share of breakeven: {b.gas_bps_per_fill:.2f} bps")
    print(f"break-even half spread: {b.half_spread_bps:.2f} bps (quote at least this far from mid)")
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    reports = calibration_report(read_jsonl(args.journal), args.horizon)
    if not reports:
        print("no decisions with outcomes in this journal")
        return 1
    for r in reports:
        print(f"\n{r.question} [{r.label}]  n={r.n}  base rate={r.base_rate:.3f}  Brier={r.brier:.4f}  log loss={r.log_loss:.4f}  ECE={r.ece:.4f}")
        print("  predicted -> observed (count)")
        for b in r.curve:
            print(f"  {b.lower:.1f}-{b.upper:.1f}: {b.mean_predicted:.3f} -> {b.observed_frequency:.3f} ({b.count})")
    return 0


def _jev_check(args: argparse.Namespace) -> int:
    import time

    from hftbot.features import AccountState, HealthState, StateEngine, snapshot
    from hftbot.jev import JevEngine
    from hftbot.judgments import DecisionUnavailable

    try:
        engine = JevEngine()
    except Exception as error:  # missing SDK or API key: say which, without a traceback
        print(f"cannot start Jev: {error}", file=sys.stderr)
        return 1
    try:
        print("models:", ", ".join(engine.list_models()))
        state = StateEngine()
        for block in simulate(SimConfig(blocks=200, seed=1)).blocks:
            features = state.update(block)
        snap = snapshot(features, AccountState(), HealthState())
        started = time.perf_counter()
        decision = engine.decide(snap, timeout_s=5.0)
    except DecisionUnavailable as error:
        print(f"Jev unavailable: {error}", file=sys.stderr)
        return 1
    finally:
        engine.close()
    print(f"answered by {decision.model} in {decision.latency_ms:.0f} ms ({(time.perf_counter() - started) * 1000:.0f} ms wall), {decision.input_tokens} input tokens")
    for name, answer in decision.to_dict()["answers"].items():
        print(f"  {name}: {answer}")
    print(f"pin this in your config: [bot] jev_pinned_model = {decision.model!r}")
    return 0
