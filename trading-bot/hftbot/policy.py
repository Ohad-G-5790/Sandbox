"""The policy engine: turns judgments into an action. Thresholds live here, in code, never in a prompt.

Each threshold is per action and scaled to what being wrong costs: pulling quotes on a false toxic
signal costs one block of spread, so it fires early; quoting full size needs a confident read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hftbot.judgments import Decision
from hftbot.market import Side

BOTH_SIDES = frozenset({Side.BUY, Side.SELL})


class ActionKind(str, Enum):
    QUOTE = "quote"
    QUOTE_WIDE = "quote_wide"
    WIDEN = "widen"
    REDUCE_ONLY = "reduce_only"
    PULL_QUOTES = "pull_quotes"
    STAND_DOWN = "stand_down"


@dataclass(frozen=True)
class PolicyAction:
    kind: ActionKind
    spread_mult: float = 1.0
    size_mult: float = 1.0
    skew: float = 0.0
    """Shift of the quote center as a fraction of the half spread; positive leans toward selling."""
    sides: frozenset[Side] = BOTH_SIDES
    low_confidence: bool = False
    reason: str = ""

    @property
    def posts_quotes(self) -> bool:
        return self.kind not in (ActionKind.PULL_QUOTES, ActionKind.STAND_DOWN) and bool(self.sides) and self.size_mult > 0


@dataclass(frozen=True)
class PolicyConfig:
    toxic_pull: float = 0.6
    stressed_widen: float = 0.7
    widen_mult: float = 2.0
    quote_min_score: float = 2.0
    quote_min_confidence: float = 0.8
    wide_min_score: float = 1.0
    wide_mult: float = 1.6
    confidence_gating: bool = True
    """Shrink size when Jev is unsure. Comparing gated against ungated is the core research question."""
    low_confidence_size_mult: float = 0.5
    degraded_spread_mult: float = 2.0
    degraded_size_mult: float = 0.5
    inventory_skew_per_level: float = 0.3
    reduce_only_pressure: float = 2.5
    direction_kelly: bool = False
    """Lean quotes toward the predicted direction by fractional Kelly. Only after calibration is verified on your venue."""
    kelly_cap: float = 0.25
    direction_max_skew: float = 0.5


def fractional_kelly(p: float, cap: float = 0.25) -> float:
    """Kelly fraction for an even-odds bet won with probability p, capped. Meaningless unless p is calibrated."""
    return max(0.0, min(2 * p - 1, cap))


def compose_action(decision: Decision, inventory: float, cfg: PolicyConfig, *, degraded: bool = False) -> PolicyAction:
    toxic = decision.noul("toxic_flow")
    if toxic > cfg.toxic_pull:
        return PolicyAction(ActionKind.PULL_QUOTES, reason=f"toxic flow p={toxic:.2f}")

    pressure = decision.score("inventory_pressure").score
    skew = _inventory_skew(inventory, pressure, cfg) + _direction_skew(decision, cfg)
    skew = max(-1.0, min(1.0, skew))
    reducing = frozenset({Side.SELL if inventory > 0 else Side.BUY}) if inventory else frozenset()
    sides = reducing if pressure >= cfg.reduce_only_pressure and reducing else BOTH_SIDES

    stressed = decision.noul("liquidity_stressed")
    quote_env = decision.score("quote_environment")
    if stressed > cfg.stressed_widen:
        action = PolicyAction(ActionKind.WIDEN, spread_mult=cfg.widen_mult, skew=skew, sides=sides, reason=f"liquidity stressed p={stressed:.2f}")
    elif quote_env.score >= cfg.quote_min_score:
        unsure = cfg.confidence_gating and quote_env.confidence < cfg.quote_min_confidence
        action = PolicyAction(
            ActionKind.QUOTE,
            size_mult=cfg.low_confidence_size_mult if unsure else 1.0,
            skew=skew,
            sides=sides,
            low_confidence=unsure,
            reason=f"quote env {quote_env.score:.2f} conf {quote_env.confidence:.2f}",
        )
    elif quote_env.score >= cfg.wide_min_score:
        action = PolicyAction(ActionKind.QUOTE_WIDE, spread_mult=cfg.wide_mult, skew=skew, sides=sides, reason=f"quote env {quote_env.score:.2f}")
    elif reducing and pressure >= cfg.reduce_only_pressure:
        action = PolicyAction(ActionKind.REDUCE_ONLY, spread_mult=cfg.wide_mult, skew=skew, sides=reducing, reason=f"inventory pressure {pressure:.2f}")
    else:
        return PolicyAction(ActionKind.STAND_DOWN, reason=f"quote env {quote_env.score:.2f}")

    if degraded:
        action = PolicyAction(
            action.kind,
            spread_mult=action.spread_mult * cfg.degraded_spread_mult,
            size_mult=action.size_mult * cfg.degraded_size_mult,
            skew=action.skew,
            sides=action.sides,
            low_confidence=action.low_confidence,
            reason=action.reason + " (degraded: deterministic fallback)",
        )
    return action


def _inventory_skew(inventory: float, pressure: float, cfg: PolicyConfig) -> float:
    if inventory == 0:
        return 0.0
    strength = min(1.0, pressure * cfg.inventory_skew_per_level)
    return strength if inventory > 0 else -strength


def _direction_skew(decision: Decision, cfg: PolicyConfig) -> float:
    if not cfg.direction_kelly:
        return 0.0
    probs = decision.choice("direction").probabilities
    up, down = probs.get("up", 0.0), probs.get("down", 0.0)
    if up + down <= 0:
        return 0.0
    p_up = up / (up + down)
    lean = fractional_kelly(max(p_up, 1 - p_up), cfg.kelly_cap) / cfg.kelly_cap * cfg.direction_max_skew
    return -lean if p_up > 0.5 else lean
