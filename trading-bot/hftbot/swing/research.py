"""Cost-aware portfolio simulation and the walk-forward test that decides whether anything gets traded.

Timing: weights decided at a bar's close are traded at the next bar's open. Every swap pays the pool
fee and slippage (in bps of the amount) plus a fixed network fee. A position is only adjusted when it
is more than `rebalance_band` of equity away from target, so price drift alone does not churn fees.

Walk-forward: the first 40% of history is the initial training window; the rest is cut into `folds`
test windows. For each window, every family picks its best configuration on all data before the
window (by Sharpe), then trades the window with it. Chaining the windows gives an out-of-sample record
that no parameter choice ever saw. The benchmark buys the same coins once, equal weight, and holds.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hftbot.swing.candles import Panel
from hftbot.swing.strategies import Strategy, candidate_grid


@dataclass(frozen=True)
class SwingCosts:
    fee_bps: float = 10.0
    """Pool and route fees per swap. Big Solana pools charge 1-30 bps; 10 is a cautious middle."""
    slippage_bps: float = 10.0
    """Price impact and quote drift per swap at small size."""
    network_fee: float = 0.002
    """USD per swap: Solana base fee plus a priority fee."""
    min_trade: float = 10.0
    """USD; smaller adjustments are skipped."""
    rebalance_band: float = 0.05
    """Fraction of equity a position may drift from target before it is traded back."""

    def cost(self, notional: float) -> float:
        return notional * (self.fee_bps + self.slippage_bps) / 1e4 + self.network_fee


@dataclass(frozen=True)
class Metrics:
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    trades: int
    trades_per_year: float
    costs: float
    exposure: float
    years: float


@dataclass
class Book:
    cash: float
    units: dict[str, float]


def simulate(
    panel: Panel,
    weights: Mapping[str, Sequence[float]],
    costs: SwingCosts,
    start: int,
    end: int,
    cash: float = 1000.0,
    entry_only: bool = False,
    book: Book | None = None,
) -> tuple[list[float], int, float, float, Book]:
    """Trade `weights` over bars [start, end), starting from `book` (or all cash). Returns (equity at each
    close, trades, costs paid, average exposure, final book). `entry_only` buys each coin once when it
    first has a target and never trades it again (buy and hold)."""
    book = book or Book(cash, {c: 0.0 for c in panel.coins})
    equity: list[float] = []
    trades, paid, exposure_sum = 0, 0.0, 0.0
    bought: set[str] = {c for c, u in book.units.items() if u > 0}
    for i in range(start, end):
        if i >= 1:
            prices = {c: panel.open[c][i] for c in panel.coins}
            targets = {c: weights[c][i - 1] for c in panel.coins}
            if entry_only:
                targets = {c: (w if c not in bought else None) for c, w in targets.items()}
            t, p = _trade_to(book, targets, prices, costs)
            trades, paid = trades + t, paid + p
            if entry_only:
                bought |= {c for c in panel.coins if book.units[c] > 0}
        closes = {c: panel.close[c][i] for c in panel.coins}
        value = book.cash + sum(u * (closes[c] or 0.0) for c, u in book.units.items())
        equity.append(value)
        if value > 0:
            exposure_sum += (value - book.cash) / value
    return equity, trades, paid, exposure_sum / max(len(equity), 1), book


def plan_orders(targets: Mapping[str, float | None], units: Mapping[str, float], cash: float, prices: Mapping[str, float | None], costs: SwingCosts) -> list[tuple[str, float, bool]]:
    """(coin, signed USD amount, closes the whole position) for every coin that needs trading, sells
    first. Shared by the backtest and the live trader, so both trade exactly the same way."""
    total = cash + sum(u * (prices.get(c) or 0.0) for c, u in units.items())
    if total <= 0:
        return []
    orders = []
    for c, target in targets.items():
        price = prices.get(c)
        if target is None or price is None:
            continue
        diff = target * total - units[c] * price
        closing = target == 0 and units[c] > 0
        if closing or (abs(diff) >= costs.min_trade and abs(diff) > costs.rebalance_band * total):
            orders.append((diff, c, closing))
    return [(c, diff, closing) for diff, c, closing in sorted(orders)]


def _trade_to(book: Book, targets: Mapping[str, float | None], prices: Mapping[str, float | None], costs: SwingCosts) -> tuple[int, float]:
    trades, paid = 0, 0.0
    for c, diff, closing in plan_orders(targets, book.units, book.cash, prices, costs):
        price = prices[c]
        assert price is not None
        if diff < 0:
            notional = book.units[c] * price if closing else -diff
            fee = costs.cost(notional)
            book.units[c] = 0.0 if closing else book.units[c] - notional / price
            book.cash += notional - fee
        else:
            notional = min(diff, book.cash)
            fee = costs.cost(notional)
            if notional <= fee:
                continue
            book.units[c] += (notional - fee) / price
            book.cash -= notional
        trades, paid = trades + 1, paid + fee
    return trades, paid


def metrics(equity: Sequence[float], initial: float, trades: int, paid: float, exposure: float, bar_hours: int) -> Metrics:
    years = len(equity) * bar_hours / 8760
    final = equity[-1] if equity else initial
    rets = [b / a - 1 for a, b in zip([initial, *equity], equity) if a > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) if rets else 0.0
    peak, worst = initial, 0.0
    for e in equity:
        peak = max(peak, e)
        worst = max(worst, 1 - e / peak if peak > 0 else 0.0)
    return Metrics(
        total_return=final / initial - 1,
        cagr=(final / initial) ** (1 / years) - 1 if years > 0 and final > 0 else -1.0,
        sharpe=mean / sd * math.sqrt(8760 / bar_hours) if sd > 0 else 0.0,
        max_drawdown=worst,
        trades=trades,
        trades_per_year=trades / years if years else 0.0,
        costs=paid,
        exposure=exposure,
        years=years,
    )


def evaluate(panel: Panel, strategy: Strategy, costs: SwingCosts, start: int, end: int, cash: float = 1000.0, weights=None) -> Metrics:
    w = weights if weights is not None else strategy.weights(panel)
    equity, trades, paid, exposure, _ = simulate(panel, w, costs, start, end, cash, entry_only=strategy.family == "hold")
    return metrics(equity, cash, trades, paid, exposure, panel.bar_hours)


# --- Walk-forward ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyResult:
    family: str
    picks: tuple[Strategy, ...]
    """The configuration chosen for each test window."""
    oos: Metrics
    coin_wins: int | None
    """Coins where the family, traded alone on that coin, beat holding that coin out of sample (None for rotation)."""
    final_pick: Strategy
    """Chosen on all history: what would be traded from now on."""


@dataclass(frozen=True)
class Report:
    families: tuple[FamilyResult, ...]
    hold: Metrics
    hold_by_coin: dict[str, Metrics]
    configs_tried: int
    test_start_ts: int
    end_ts: int
    coins: tuple[str, ...]

    def passes(self, r: FamilyResult) -> bool:
        """Beats holding out of sample: better risk-adjusted return, a smaller worst loss, and on most coins."""
        coins_ok = r.coin_wins is None or r.coin_wins * 2 >= len(self.coins)
        return r.oos.sharpe > self.hold.sharpe and r.oos.max_drawdown < self.hold.max_drawdown and coins_ok

    def best(self) -> FamilyResult | None:
        passing = [r for r in self.families if self.passes(r)]
        return max(passing, key=lambda r: r.oos.sharpe) if passing else None


def _boundaries(n: int, folds: int, train_fraction: float = 0.4) -> list[int]:
    first = int(n * train_fraction)
    return [first + k * (n - first) // folds for k in range(folds + 1)]


def walk_forward(panel: Panel, costs: SwingCosts, folds: int = 4, grid: Mapping[str, list[Strategy]] | None = None, cash: float = 1000.0) -> Report:
    grid = grid or candidate_grid()
    n = len(panel)
    if n < 50 * (folds + 1):
        raise ValueError(f"only {n} bars: not enough history for {folds} test windows")
    bounds = _boundaries(n, folds)
    weights = {s: s.weights(panel) for configs in grid.values() for s in configs}
    single = {c: Panel(panel.ts, {c: panel.open[c]}, {c: panel.close[c]}, panel.bar_hours) for c in panel.coins}

    def chain(p: Panel, picks: Sequence[Strategy], w_for) -> Metrics:
        equity_all: list[float] = []
        trades = 0
        paid = exposure_weighted = 0.0
        book = None  # positions carry from one test window into the next, as they would live
        for k, s in enumerate(picks):
            equity, t, c, exposure, book = simulate(p, w_for(s), costs, bounds[k], bounds[k + 1], cash, entry_only=s.family == "hold", book=book)
            equity_all += equity
            trades, paid, exposure_weighted = trades + t, paid + c, exposure_weighted + exposure * len(equity)
        return metrics(equity_all, cash, trades, paid, exposure_weighted / max(len(equity_all), 1), p.bar_hours)

    results = []
    for family, configs in grid.items():
        picks = []
        for k in range(folds):
            scored = [(evaluate(panel, s, costs, 0, bounds[k], cash, weights[s]).sharpe, i, s) for i, s in enumerate(configs)]
            picks.append(max(scored)[2])
        oos = chain(panel, picks, lambda s: weights[s])
        coin_wins = None
        if family != "rotation":
            hold = Strategy("hold")
            coin_wins = sum(
                chain(single[c], picks, lambda s, c=c: s.weights(single[c])).sharpe > chain(single[c], [hold] * folds, lambda s, c=c: s.weights(single[c])).sharpe
                for c in panel.coins
            )
        final = max((evaluate(panel, s, costs, 0, n, cash, weights[s]).sharpe, i, s) for i, s in enumerate(configs))[2]
        results.append(FamilyResult(family, tuple(picks), oos, coin_wins, final))

    hold = Strategy("hold")
    hold_metrics = evaluate(panel, hold, costs, bounds[0], n, cash)
    hold_by_coin = {c: evaluate(single[c], hold, costs, bounds[0], n, cash) for c in panel.coins}
    return Report(tuple(results), hold_metrics, hold_by_coin, sum(len(v) for v in grid.values()), panel.ts[bounds[0]], panel.ts[-1], tuple(panel.coins))


def format_report(report: Report) -> str:
    import datetime as dt

    def day(ts: int) -> str:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"Out-of-sample test {day(report.test_start_ts)} to {day(report.end_ts)} on {', '.join(report.coins)} (after fees).",
        f"Every number below comes from periods the strategy was not tuned on. {report.configs_tried} configurations were tried in total.",
        "",
        f"{'':<10} {'return':>9} {'per year':>9} {'Sharpe':>7} {'worst drop':>11} {'trades/yr':>10} {'fees':>8} {'in market':>10} {'coins won':>10}  verdict",
    ]

    def row(name: str, m: Metrics, wins: str, verdict: str) -> str:
        return f"{name:<10} {m.total_return:>9.1%} {m.cagr:>9.1%} {m.sharpe:>7.2f} {m.max_drawdown:>11.1%} {m.trades_per_year:>10.0f} {m.costs:>8.2f} {m.exposure:>10.0%} {wins:>10}  {verdict}"

    lines.append(row("HOLD", report.hold, "-", "the bar to beat"))
    for r in report.families:
        wins = "n/a" if r.coin_wins is None else f"{r.coin_wins}/{len(report.coins)}"
        lines.append(row(r.family, r.oos, wins, "BEATS HOLD" if report.passes(r) else "no"))
    best = report.best()
    lines.append("")
    if best is None:
        lines.append("No strategy beat holding out of sample. Nothing should be traded; holding (or not investing) wins.")
    else:
        lines.append(f"Best: {best.family}. From now on it would trade {best.final_pick.label}, chosen on all history.")
        lines.append("This is still a test result, not a promise: paper trade it before risking money.")
    lines.append("")
    lines.append("Holding each coin alone over the same period: " + ", ".join(f"{c} {m.total_return:+.0%} (worst drop {m.max_drawdown:.0%})" for c, m in report.hold_by_coin.items()))
    return "\n".join(lines)


# --- How account size changes the fee bill ------------------------------------------------------


@dataclass(frozen=True)
class SizeRow:
    cash: float
    trades_per_year: float
    fees_per_year_pct: float
    """All costs paid per year, as a percentage of the starting account."""
    fixed_fees_per_year_pct: float
    """Just the fixed network fees, as a percentage of the starting account."""
    gross_cagr: float
    net_cagr: float


def size_sweep(panel: Panel, strategy: Strategy, costs: SwingCosts, sizes: Sequence[float], start: int = 0, end: int | None = None) -> list[SizeRow]:
    """Run one strategy at several account sizes. Gross runs trade the same way with zero fees, so
    gross minus net is exactly what the fees took."""
    end = len(panel) if end is None else end
    w = strategy.weights(panel)
    hold = strategy.family == "hold"
    free = SwingCosts(0.0, 0.0, 0.0, costs.min_trade, costs.rebalance_band)
    rows = []
    for cash in sizes:
        equity, trades, paid, exposure, _ = simulate(panel, w, costs, start, end, cash, entry_only=hold)
        net = metrics(equity, cash, trades, paid, exposure, panel.bar_hours)
        g_equity, g_trades, _, g_exposure, _ = simulate(panel, w, free, start, end, cash, entry_only=hold)
        gross = metrics(g_equity, cash, g_trades, 0.0, g_exposure, panel.bar_hours)
        years = max(net.years, 1e-9)
        rows.append(
            SizeRow(
                cash=cash,
                trades_per_year=net.trades_per_year,
                fees_per_year_pct=paid / years / cash * 100,
                fixed_fees_per_year_pct=trades * costs.network_fee / years / cash * 100,
                gross_cagr=gross.cagr,
                net_cagr=net.cagr,
            )
        )
    return rows
