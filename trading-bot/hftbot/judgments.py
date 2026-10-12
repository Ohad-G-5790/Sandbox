"""The judgment battery: atomic questions about the state, and the typed answers any engine returns.

Questions are judgments, never arithmetic: anything computable is already a number in the snapshot.
Each question is evaluated in isolation, so one that needs another's answer must be composed in code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, Union


@dataclass(frozen=True)
class NoulQuestion:
    """Yes/no: the answer is P(yes)."""

    instructions: str


@dataclass(frozen=True)
class ChoiceQuestion:
    """Pick one label: the answer is a distribution over labels."""

    instructions: str
    labels: Mapping[str, str | None]


@dataclass(frozen=True)
class ScoreQuestion:
    """Rate on an ordered rubric: level i is described by levels[i]; the answer is an expected level."""

    instructions: str
    levels: tuple[str, ...]


Question = Union[NoulQuestion, ChoiceQuestion, ScoreQuestion]


def default_battery(direction_horizon_blocks: int = 10) -> dict[str, Question]:
    """Six judgments asked together every block, at single-question latency."""
    return {
        "regime": ChoiceQuestion(
            "Which market regime does this state describe? Use price.ret_bps, vol.rv_5m_bps against vol.rv_30m_bps, and vol.vs_24h.",
            {
                "trending": "Persistent move in one direction; returns across windows agree in sign.",
                "mean_reverting": "Price oscillates around a level; short returns revert.",
                "high_vol": "Volatility well above normal without a clear direction.",
                "crisis": "Disorderly: volatility spiking while liquidity disappears.",
            },
        ),
        "direction": ChoiceQuestion(
            f"Where will mid be {direction_horizon_blocks} blocks from now, relative to price.mid? "
            "Taker flow (flow_1m) and book imbalance are the strongest short-horizon signals.",
            {
                "up": "Higher by more than half the current spread.",
                "down": "Lower by more than half the current spread.",
                "neutral": "Within half the current spread of the current mid.",
            },
        ),
        "toxic_flow": NoulQuestion(
            "Is the aggressive (taker) flow likely informed, so that a maker filled by it will lose as price keeps moving?"
        ),
        "liquidity_stressed": NoulQuestion(
            "Are makers pulling back so the book could gap: thin depth (book.depth_vs_24h), wide spread, and one-sided pressure?"
        ),
        "quote_environment": ScoreQuestion(
            "How favorable is this state for posting passive quotes on both sides and earning the spread?",
            ("Do not quote", "Marginal", "Standard", "Excellent"),
        ),
        "inventory_pressure": ScoreQuestion(
            "Given position.inventory, its age and current conditions, how urgently should inventory be reduced?",
            ("None", "Mild", "Skew quotes hard", "Reduce now"),
        ),
    }


@dataclass(frozen=True)
class NoulAnswer:
    p: float


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: Mapping[str, float]


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    confidence: float
    probabilities: Mapping[int, float]


Answer = Union[NoulAnswer, ChoiceAnswer, ScoreAnswer]


@dataclass(frozen=True)
class Decision:
    answers: Mapping[str, Answer]
    source: str
    """Which engine answered: "jev", "rules", "always-quote"."""
    model: str
    """The exact model that answered. Thresholds are calibrated against one model: log it every time."""
    latency_ms: float
    input_tokens: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def noul(self, name: str) -> float:
        return _typed(self.answers, name, NoulAnswer).p

    def choice(self, name: str) -> ChoiceAnswer:
        return _typed(self.answers, name, ChoiceAnswer)

    def score(self, name: str) -> ScoreAnswer:
        return _typed(self.answers, name, ScoreAnswer)

    def to_dict(self) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for name, a in self.answers.items():
            if isinstance(a, NoulAnswer):
                answers[name] = {"type": "noul", "p": a.p}
            elif isinstance(a, ChoiceAnswer):
                answers[name] = {"type": "choice", "choice": a.choice, "confidence": a.confidence, "probabilities": dict(a.probabilities)}
            else:
                answers[name] = {
                    "type": "score",
                    "score": a.score,
                    "confidence": a.confidence,
                    "probabilities": {str(k): v for k, v in a.probabilities.items()},
                }
        return {"source": self.source, "model": self.model, "latency_ms": self.latency_ms, "input_tokens": self.input_tokens, "answers": answers}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Decision:
        answers: dict[str, Answer] = {}
        for name, a in d["answers"].items():
            if a["type"] == "noul":
                answers[name] = NoulAnswer(a["p"])
            elif a["type"] == "choice":
                answers[name] = ChoiceAnswer(a["choice"], a["confidence"], dict(a["probabilities"]))
            else:
                answers[name] = ScoreAnswer(a["score"], a["confidence"], {int(k): v for k, v in a["probabilities"].items()})
        return cls(answers, d["source"], d["model"], d["latency_ms"], d.get("input_tokens", 0))


def _typed(answers: Mapping[str, Answer], name: str, kind: type) -> Any:
    answer = answers[name]
    if not isinstance(answer, kind):
        raise TypeError(f"answer {name!r} is {type(answer).__name__}, not {kind.__name__}")
    return answer


class DecisionUnavailable(Exception):
    """The engine could not answer (API down, auth failure, unexpected model). The loop falls back."""


class DecisionTimeout(DecisionUnavailable):
    """The engine did not answer before the block deadline. The loop holds: no quote on stale state."""


class DecisionEngine(Protocol):
    name: str

    def decide(self, state: Mapping[str, Any], timeout_s: float) -> Decision: ...
