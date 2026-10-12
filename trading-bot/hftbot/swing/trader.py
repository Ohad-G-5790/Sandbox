"""The swing trader: once per bar, update prices, re-check the strategy, and trade toward its targets.

Paper mode (the default) keeps a simulated balance in a state file and, when Jupiter is reachable,
fills at real Jupiter quotes. Live mode reads the wallet's balances from chain and swaps through
Jupiter, refusing any swap priced too far from the reference price.

Safety: per-coin weight cap, a drawdown kill switch that sells everything to USDC and stays latched
across restarts until `hftbot swing reset-kill`, and no trading on stale data.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from hftbot.swing import data
from hftbot.swing.candles import HOUR, Candle, Panel, align, load_csv, merge, resample, save_csv
from hftbot.swing.research import Report, SwingCosts, format_report, plan_orders, walk_forward
from hftbot.swing.strategies import Strategy, candidate_grid

log = logging.getLogger("hftbot")


@dataclass(frozen=True)
class SwingConfig:
    coins: tuple[str, ...] = ("SOL", "BTC", "ETH", "JUP")
    bar_hours: int = 4
    source: str = "binance"
    history_start: str = "2021-01-01"
    strategy: str = "auto"
    """"auto" trades the best family that beats holding (or nothing); or name a family to force it."""
    params: dict[str, Any] = field(default_factory=dict)
    """With a named family: its exact parameters. Empty: chosen by the walk-forward test."""
    folds: int = 4
    fee_bps: float = 10.0
    slippage_bps: float = 10.0
    network_fee: float = 0.002
    min_trade: float = 10.0
    rebalance_band: float = 0.05
    paper_cash: float = 1000.0
    max_drawdown_pct: float = 25.0
    max_weight_per_coin: float = 0.5
    sol_fee_reserve: float = 0.05
    max_price_deviation_pct: float = 2.0
    swap_slippage_bps: int = 50
    max_data_delay_bars: float = 1.0
    retest_hours: float = 168.0
    trade_delay_s: float = 120.0

    @property
    def costs(self) -> SwingCosts:
        return SwingCosts(self.fee_bps, self.slippage_bps, self.network_fee, self.min_trade, self.rebalance_band)


def load_swing_config(path: str | Path | None) -> SwingConfig:
    if path is None:
        return SwingConfig()
    with open(path, "rb") as f:
        section = dict(tomllib.load(f).get("swing", {}))
    known = {f.name for f in dataclasses.fields(SwingConfig)}
    unknown = set(section) - known
    if unknown:
        raise ValueError(f"unknown keys in [swing]: {sorted(unknown)}")
    if "coins" in section:
        section["coins"] = tuple(section["coins"])
        missing = [c for c in section["coins"] if c not in data.COINS]
        if missing:
            raise ValueError(f"unknown coins {missing}: known are {sorted(data.COINS)}")
    return SwingConfig(**section)


class CandleStore:
    """Hourly candles per coin in CSV files, updated incrementally."""

    def __init__(self, directory: Path, cfg: SwingConfig, fetch: Callable[..., list[Candle]] = data.fetch) -> None:
        self.directory = directory
        self.cfg = cfg
        self._fetch = fetch
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, coin: str) -> Path:
        return self.directory / f"{coin}.csv"

    def load(self, coin: str) -> list[Candle]:
        return load_csv(self.path(coin)) if self.path(coin).exists() else []

    def update(self, coin: str) -> list[Candle]:
        old = self.load(coin)
        if old:
            since = old[-1].ts - 2 * HOUR  # refetch the last hours: the newest candle may have been partial
        else:
            since = int(dt.datetime.fromisoformat(self.cfg.history_start).replace(tzinfo=dt.timezone.utc).timestamp())
        candles = merge(old, self._fetch(data.COINS[coin], self.cfg.source, since))
        save_csv(self.path(coin), candles)
        return candles

    def panel(self, now: float, update: bool = True) -> Panel:
        series = {}
        for coin in self.cfg.coins:
            try:
                hourly = self.update(coin) if update else self.load(coin)
            except Exception as error:  # noqa: BLE001 - one failing source must not hide the others' data
                log.warning("could not update %s: %s; using stored data", coin, error)
                hourly = self.load(coin)
            if not hourly:
                log.warning("no price history for %s from %s: leaving it out", coin, self.cfg.source)
                continue
            series[coin] = resample(hourly, self.cfg.bar_hours, now)
        if not series:
            raise RuntimeError(f"no price history for any coin from {self.cfg.source}: check the network or try the other source")
        return align(series, self.cfg.bar_hours)


@dataclass(frozen=True)
class SwapResult:
    coin: str
    side: str
    units: float
    usdc: float
    price: float
    cost: float
    source: str
    """How it was priced: "model", "jupiter-quote" (paper) or "jupiter" (live)."""


class Executor(Protocol):
    mode: str

    def balances(self) -> tuple[float, dict[str, float]]: ...

    def sell(self, coin: str, units: float, ref_price: float) -> SwapResult: ...

    def buy(self, coin: str, usdc: float, ref_price: float) -> SwapResult: ...


class SwapError(Exception):
    pass


class PaperExecutor:
    """Simulated balances. With `quote`, fills at what Jupiter would actually pay; otherwise at the cost model."""

    mode = "paper"

    def __init__(self, book: dict[str, Any], costs: SwingCosts, coins: tuple[str, ...], quote: Callable[[str, str, int], tuple[int, int]] | None = None) -> None:
        self.book = book
        self.costs = costs
        self.quote = quote
        book.setdefault("units", {})
        for c in coins:
            book["units"].setdefault(c, 0.0)

    def balances(self) -> tuple[float, dict[str, float]]:
        return self.book["usdc"], dict(self.book["units"])

    def sell(self, coin: str, units: float, ref_price: float) -> SwapResult:
        usdc, source = self._fill(coin, data.COINS[coin].mint, data.USDC_MINT, units, data.COINS[coin].decimals, data.USDC_DECIMALS)
        if usdc is None:
            usdc = units * ref_price - self.costs.cost(units * ref_price) + self.costs.network_fee
        usdc -= self.costs.network_fee
        self.book["units"][coin] = max(0.0, self.book["units"][coin] - units)
        self.book["usdc"] += usdc
        return SwapResult(coin, "sell", units, usdc, usdc / units, units * ref_price - usdc, source)

    def buy(self, coin: str, usdc: float, ref_price: float) -> SwapResult:
        usdc = min(usdc, self.book["usdc"])
        units, source = self._fill(coin, data.USDC_MINT, data.COINS[coin].mint, usdc - self.costs.network_fee, data.USDC_DECIMALS, data.COINS[coin].decimals)
        if units is None:
            units = (usdc - self.costs.cost(usdc)) / ref_price
        self.book["units"][coin] += units
        self.book["usdc"] -= usdc
        return SwapResult(coin, "buy", units, usdc, usdc / units, usdc - units * ref_price, source)

    def _fill(self, coin: str, in_mint: str, out_mint: str, amount: float, in_dec: int, out_dec: int) -> tuple[float | None, str]:
        if self.quote is None or amount <= 0:
            return None, "model"
        try:
            _, out_atoms = self.quote(in_mint, out_mint, round(amount * 10**in_dec))
        except Exception as error:  # noqa: BLE001 - quotes are a nicety in paper mode
            log.info("paper %s: no Jupiter quote (%s), using the cost model", coin, error)
            return None, "model"
        return out_atoms / 10**out_dec, "jupiter-quote"


class SwingTrader:
    def __init__(self, cfg: SwingConfig, store: CandleStore, executor: Executor, state_path: Path, *, clock: Callable[[], float] = time.time, journal_path: Path | None = None) -> None:
        self.cfg, self.store, self.executor = cfg, store, executor
        self.state_path = state_path
        self.journal_path = journal_path
        self._clock = clock
        self.state = load_state(state_path)
        self.report: Report | None = None

    def tick(self) -> dict[str, Any]:
        now = self._clock()
        panel = self.store.panel(now)
        width = self.cfg.bar_hours * HOUR
        bar_end = panel.ts[-1] + width
        record: dict[str, Any] = {"ts": now, "bar": panel.ts[-1], "mode": self.executor.mode, "trades": []}
        prices = {c: panel.close[c][-1] for c in panel.coins}
        record["prices"] = prices
        if now - bar_end > self.cfg.max_data_delay_bars * width:
            record["note"] = f"stale data: last finished bar closed {(now - bar_end) / 3600:.1f} h ago; not trading"
            log.warning(record["note"])
            return self._finish(record)

        strategy = self._strategy(panel, now)
        usdc, units = self.executor.balances()
        equity = usdc + sum(u * (prices.get(c) or 0.0) for c, u in units.items())  # a coin without data counts as 0
        peak = max(self.state.get("peak_equity", equity), equity)
        self.state["peak_equity"] = peak
        drawdown = 1 - equity / peak if peak > 0 else 0.0
        record.update(equity=equity, drawdown=drawdown, strategy=strategy.label if strategy else None)
        if not self.state.get("killed") and drawdown * 100 > self.cfg.max_drawdown_pct:
            self.state["killed"] = f"drawdown {drawdown:.1%} from peak {peak:.2f} exceeded {self.cfg.max_drawdown_pct}%"
            log.error("KILL SWITCH: %s; selling everything to USDC", self.state["killed"])

        if self.state.get("killed"):
            targets: dict[str, float | None] = {c: 0.0 for c in panel.coins}
            record["note"] = f"kill switch: {self.state['killed']} (reset with `hftbot swing reset-kill`)"
        elif strategy is None:
            targets = {c: 0.0 for c in panel.coins}
            record["note"] = "no strategy beats holding: staying in USDC"
        else:
            w = strategy.weights(panel)
            targets = {c: min(w[c][-1], self.cfg.max_weight_per_coin) for c in panel.coins}
        record["targets"] = targets

        for coin, diff, closing in plan_orders(targets, units, usdc, prices, self.cfg.costs):
            price = prices[coin]
            assert price is not None
            try:
                if diff < 0:
                    result = self.executor.sell(coin, units[coin] if closing else min(-diff / price, units[coin]), price)
                else:
                    available, _ = self.executor.balances()
                    if min(diff, available) < self.cfg.min_trade:
                        continue
                    result = self.executor.buy(coin, min(diff, available), price)
            except SwapError as error:
                log.error("swap %s failed: %s", coin, error)
                record.setdefault("errors", []).append(f"{coin}: {error}")
                continue
            record["trades"].append(dataclasses.asdict(result))
            log.info("%s %s %.6g %s for %.2f USDC at %.6g (%s)", self.executor.mode, result.side, result.units, coin, result.usdc, result.price, result.source)
        return self._finish(record)

    def run_forever(self, should_stop: Callable[[], bool] = lambda: False, sleep: Callable[[float], None] = time.sleep, retry_s: float = 300.0) -> None:
        width = self.cfg.bar_hours * HOUR
        while not should_stop():
            now = self._clock()
            wake = (now // width + 1) * width + self.cfg.trade_delay_s
            try:
                record = self.tick()
                log.info("bar %s: equity %.2f %s", dt.datetime.fromtimestamp(record["bar"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M"), record.get("equity", float("nan")), record.get("note", ""))
            except Exception:  # noqa: BLE001 - one bad bar must not stop a 24/7 service; it is logged and retried
                log.exception("tick failed; retrying in %.0f s", retry_s)
                wake = min(wake, self._clock() + retry_s)
            sleep(max(wake - self._clock(), 1.0))

    def _strategy(self, panel: Panel, now: float) -> Strategy | None:
        cfg = self.cfg
        if cfg.strategy != "auto" and cfg.params:
            return Strategy(cfg.strategy, dict(cfg.params))
        tested_at = self.state.get("tested_at", 0)
        if self.report is None or now - tested_at > cfg.retest_hours * HOUR or "pick" not in self.state:
            grid = candidate_grid() if cfg.strategy == "auto" else {cfg.strategy: candidate_grid()[cfg.strategy]}
            self.report = walk_forward(panel, cfg.costs, cfg.folds, grid)
            text = format_report(self.report)
            log.info("walk-forward test:\n%s", text)
            (self.state_path.parent / "report.txt").write_text(text + "\n")
            if cfg.strategy == "auto":
                best = self.report.best()
                pick = best.final_pick if best else None
            else:
                result = self.report.families[0]
                pick = result.final_pick
                if not self.report.passes(result):
                    log.warning("%s does not beat holding out of sample, but it was named in the config: trading it anyway", cfg.strategy)
            self.state["pick"] = None if pick is None else {"family": pick.family, "params": pick.params}
            self.state["tested_at"] = now
        pick = self.state.get("pick")
        return None if pick is None else Strategy(pick["family"], pick["params"])

    def _finish(self, record: dict[str, Any]) -> dict[str, Any]:
        if isinstance(self.executor, PaperExecutor):
            self.state["paper"] = self.executor.book
        save_state(self.state_path, self.state)
        if self.journal_path:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        return record


def load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(path)  # atomic: a crash mid-write cannot corrupt the kill-switch latch
