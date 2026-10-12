"""Calibration: do 80%-tagged events happen 80% of the time on your venue?

The model is calibrated against its training distribution, not your market. Score its logged
probabilities against what actually happened, and if the reliability curve bends, correct it here
(Platt scaling) instead of moving thresholds by feel.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

EPS = 1e-6


def brier(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes, strict=True)) / len(probs)


def log_loss(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    total = 0.0
    for p, y in zip(probs, outcomes, strict=True):
        p = min(max(p, EPS), 1 - EPS)
        total -= y * math.log(p) + (1 - y) * math.log(1 - p)
    return total / len(probs)


@dataclass(frozen=True)
class Bin:
    lower: float
    upper: float
    mean_predicted: float
    observed_frequency: float
    count: int


def reliability(probs: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> list[Bin]:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in zip(probs, outcomes, strict=True):
        buckets[min(int(p * bins), bins - 1)].append((p, y))
    return [
        Bin(k / bins, (k + 1) / bins, sum(p for p, _ in b) / len(b), sum(y for _, y in b) / len(b), len(b))
        for k, b in enumerate(buckets)
        if b
    ]


def expected_calibration_error(probs: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> float:
    n = len(probs)
    return sum(b.count / n * abs(b.mean_predicted - b.observed_frequency) for b in reliability(probs, outcomes, bins))


class PlattScaler:
    """p' = sigmoid(a * logit(p) + b), fit by Newton's method on log loss. Apply in the policy layer."""

    def __init__(self, a: float = 1.0, b: float = 0.0) -> None:
        self.a, self.b = a, b

    def fit(self, probs: Sequence[float], outcomes: Sequence[int], iterations: int = 50, l2: float = 1e-6) -> PlattScaler:
        xs = [_logit(p) for p in probs]

        def loss(a: float, b: float) -> float:
            return log_loss([_sigmoid(a * x + b) for x in xs], outcomes) * len(xs) + l2 * a * a / 2

        a, b = 1.0, 0.0
        current = loss(a, b)
        for _ in range(iterations):
            ga = gb = haa = hab = hbb = 0.0
            for x, y in zip(xs, outcomes, strict=True):
                q = _sigmoid(a * x + b)
                w = q * (1 - q)
                ga += (q - y) * x
                gb += q - y
                haa += w * x * x
                hab += w * x
                hbb += w
            ga, haa, hbb = ga + l2 * a, haa + l2, hbb + l2
            det = haa * hbb - hab * hab
            if abs(det) < 1e-12:
                break
            da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
            # Full Newton steps overshoot when the starting point is far off; halve until the loss drops.
            step = 1.0
            while step > 1e-8 and (trial := loss(a - step * da, b - step * db)) > current:
                step /= 2
            if step <= 1e-8:
                break
            a, b, current = a - step * da, b - step * db, trial
            if abs(step * da) < 1e-10 and abs(step * db) < 1e-10:
                break
        self.a, self.b = a, b
        return self

    def transform(self, p: float) -> float:
        return _sigmoid(self.a * _logit(p) + self.b)


def _logit(p: float) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(min(x, 50.0), -50.0)))


# --- Labeling logged decisions with what happened next ---------------------------------------


@dataclass(frozen=True)
class Report:
    question: str
    label: str
    n: int
    base_rate: float
    brier: float
    log_loss: float
    ece: float
    curve: list[Bin]


def label_outcomes(records: Sequence[Mapping[str, Any]], horizon_blocks: int = 10, toxic_move_bps: float = 2.0) -> dict[str, tuple[list[float], list[int]]]:
    """Pair each logged probability with its outcome `horizon_blocks` later.

    direction:  up / down / neutral, neutral meaning within half the spread at decision time.
    toxic_flow: 1 if mid moved at least `toxic_move_bps` in the direction of the dominant taker flow.
    """
    pairs: dict[str, tuple[list[float], list[int]]] = {}

    def add(key: str, p: float, y: int) -> None:
        probs, outcomes = pairs.setdefault(key, ([], []))
        probs.append(p)
        outcomes.append(y)

    # A journal appended to across restarts holds several runs, each numbering blocks from the start:
    # an outcome must come from the same run as its decision.
    run_of: list[int] = []
    for i, r in enumerate(records):
        run_of.append(run_of[-1] + (r["block"] <= records[i - 1]["block"]) if i else 0)
    by_block = {(run, r["block"]): r for run, r in zip(run_of, records)}

    for run, r in zip(run_of, records):
        decision, snap = r.get("decision"), r.get("snapshot")
        later = by_block.get((run, r["block"] + horizon_blocks))
        if not decision or not snap or later is None:
            continue
        move_bps = (later["mid"] / r["mid"] - 1) * 1e4
        answers = decision["answers"]
        if "direction" in answers:
            band = snap["book"]["spread_bps"] / 2
            outcome = "up" if move_bps > band else "down" if move_bps < -band else "neutral"
            for label, p in answers["direction"]["probabilities"].items():
                add(f"direction={label}", p, int(label == outcome))
        if "toxic_flow" in answers:
            flow_sign = 1 if snap["flow_1m"]["buy_ratio"] >= 0.5 else -1
            add("toxic_flow", answers["toxic_flow"]["p"], int(flow_sign * move_bps >= toxic_move_bps))
    return pairs


def calibration_report(records: Iterable[Mapping[str, Any]], horizon_blocks: int = 10, bins: int = 10) -> list[Report]:
    pairs = label_outcomes(list(records), horizon_blocks)
    reports = []
    for key, (probs, outcomes) in sorted(pairs.items()):
        question, _, label = key.partition("=")
        reports.append(
            Report(
                question,
                label or "yes",
                len(probs),
                sum(outcomes) / len(outcomes),
                brier(probs, outcomes),
                log_loss(probs, outcomes),
                expected_calibration_error(probs, outcomes, bins),
                reliability(probs, outcomes, bins),
            )
        )
    return reports
