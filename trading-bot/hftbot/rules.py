"""Deterministic engines that answer the same battery as Jev.

`RulesEngine` is the hand-written-rules baseline Jev has to beat, and the fallback the loop uses when
Jev is unavailable. `AlwaysQuoteEngine` answers "quote normally" every block: pure Avellaneda-Stoikov
with no judgment layer, the baseline that shows whether judgments add anything at all.
Both report confidence 1.0: they are deterministic, so confidence gating does not apply to them.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

from hftbot.judgments import ChoiceAnswer, Decision, NoulAnswer, ScoreAnswer


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(min(x, 50.0), -50.0)))


def _softmax(scores: Mapping[str, float]) -> dict[str, float]:
    top = max(scores.values())
    exp = {k: math.exp(v - top) for k, v in scores.items()}
    total = sum(exp.values())
    return {k: v / total for k, v in exp.items()}


def _choice(scores: Mapping[str, float]) -> ChoiceAnswer:
    probs = _softmax(scores)
    best = max(probs, key=probs.__getitem__)
    return ChoiceAnswer(best, 1.0, probs)


def _score(value: float, levels: int = 4) -> ScoreAnswer:
    """A deterministic score as a distribution split between the two nearest levels."""
    value = min(max(value, 0.0), levels - 1.0)
    low = math.floor(value)
    high = min(low + 1, levels - 1)
    probs = {i: 0.0 for i in range(levels)}
    probs[low] += 1 - (value - low)
    probs[high] += value - low
    return ScoreAnswer(value, 1.0, probs)


class RulesEngine:
    name = "rules"

    def __init__(self, max_position: float, max_inventory_age_s: float = 600.0) -> None:
        self.max_position = max_position
        self.max_inventory_age_s = max_inventory_age_s

    def decide(self, state: Mapping[str, Any], timeout_s: float) -> Decision:
        started = time.perf_counter()
        price, book, flow, vol, pos = state["price"], state["book"], state["flow_1m"], state["vol"], state["position"]
        ret_1m, ret_5m = price["ret_bps"]["1m"], price["ret_bps"]["5m"]
        rv_5m = max(vol["rv_5m_bps"], 1e-6)
        vol_ratio, depth_ratio = vol["vs_24h"], book["depth_vs_24h"]
        buy_ratio, imbalance = flow["buy_ratio"], book["imbalance"]

        trend_z = abs(ret_5m) / rv_5m
        regime = _choice(
            {
                "trending": 2.0 * (trend_z - 1.0),
                "mean_reverting": 2.0 * (1.0 - trend_z),
                "high_vol": 3.0 * (vol_ratio - 1.8),
                "crisis": 3.0 * (vol_ratio - 3.0) + 4.0 * (0.5 - depth_ratio),
            }
        )

        lean = 1.5 * imbalance + 2.0 * (buy_ratio - 0.5) * 2
        p_neutral = 0.5 * math.exp(-abs(lean))
        p_up = (1 - p_neutral) * _sigmoid(2 * lean)
        direction_probs = {"up": p_up, "down": 1 - p_neutral - p_up, "neutral": p_neutral}
        direction = ChoiceAnswer(max(direction_probs, key=direction_probs.__getitem__), 1.0, direction_probs)

        one_sided = abs(buy_ratio - 0.5) * 2
        aligned = (buy_ratio - 0.5) * ret_1m > 0
        toxic = _sigmoid(5 * (one_sided - 0.5) + (0.7 if aligned else -0.7) * min(abs(ret_1m) / rv_5m * 3, 3))
        stressed = _sigmoid(6 * (0.7 - depth_ratio) + 1.5 * max(vol_ratio - 2, 0))

        quote_env = _score(3.0 - 2.0 * toxic - 1.0 * max(vol_ratio - 1.0, 0.0) - 1.5 * stressed)
        inventory_frac = abs(pos["inventory"]) / self.max_position if self.max_position > 0 else 0.0
        inventory_pressure = _score(3.0 * inventory_frac + pos["age_s"] / self.max_inventory_age_s)

        answers = {
            "regime": regime,
            "direction": direction,
            "toxic_flow": NoulAnswer(toxic),
            "liquidity_stressed": NoulAnswer(stressed),
            "quote_environment": quote_env,
            "inventory_pressure": inventory_pressure,
        }
        return Decision(answers, self.name, "rules-v1", (time.perf_counter() - started) * 1000)


class AlwaysQuoteEngine:
    """No judgment: always a calm, quotable market with no inventory urgency."""

    name = "always-quote"

    def decide(self, state: Mapping[str, Any], timeout_s: float) -> Decision:
        answers = {
            "regime": ChoiceAnswer("mean_reverting", 1.0, {"trending": 0.0, "mean_reverting": 1.0, "high_vol": 0.0, "crisis": 0.0}),
            "direction": ChoiceAnswer("neutral", 1.0, {"up": 0.0, "down": 0.0, "neutral": 1.0}),
            "toxic_flow": NoulAnswer(0.0),
            "liquidity_stressed": NoulAnswer(0.0),
            "quote_environment": _score(3.0),
            "inventory_pressure": _score(0.0),
        }
        return Decision(answers, self.name, "always-quote", 0.0)
