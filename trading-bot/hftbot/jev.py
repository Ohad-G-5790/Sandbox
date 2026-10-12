"""Jev through the official TypeSafe SDK (`pip install "hftbot[jev]"`, then set TYPESAFE_API_KEY).

The SDK talks to https://api.typesafe.ai directly. Gateways (OpenRouter, Vercel, LiteLLM) add a round
trip a block-cadence loop cannot afford, so they are deliberately not supported here.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from hftbot.judgments import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    Decision,
    DecisionTimeout,
    DecisionUnavailable,
    NoulAnswer,
    NoulQuestion,
    Question,
    ScoreAnswer,
    ScoreQuestion,
    default_battery,
)

JEV_USD_PER_MILLION_INPUT_TOKENS = 0.042
"""Published launch price; output tokens are free. Used only for cost reporting."""


def _import_sdk() -> Any:
    try:
        import typesafe_sdk
    except ImportError as error:
        raise ImportError('Jev needs the TypeSafe SDK: pip install "hftbot[jev]"') from error
    return typesafe_sdk


class JevEngine:
    """Asks the whole battery in one call. Never retries: a retried answer arrives after the block it was for."""

    name = "jev"

    def __init__(
        self,
        battery: Mapping[str, Question] | None = None,
        *,
        model: str | None = None,
        pinned_model: str | None = None,
        client: Any = None,
    ) -> None:
        """
        Args:
            battery: Questions to ask every block; defaults to `default_battery()`.
            model: Model name or alias to request; the SDK default is "jev-latest".
            pinned_model: The exact model name your thresholds were calibrated on. When set, an answer
                from any other model (for example after "jev-latest" moves) is refused, so the loop falls
                back instead of trading on uncalibrated probabilities.
            client: A preconfigured `typesafe_sdk.TypeSafeClient` (tests pass one with a mock transport).
        """
        self._sdk = _import_sdk()
        self._no_retry = self._sdk.RetryPolicy(max_retries=0)
        self._client = client if client is not None else self._sdk.TypeSafeClient(model=model, retry=self._no_retry)
        self._battery = dict(battery or default_battery())
        self._questions = {name: self._to_sdk(question) for name, question in self._battery.items()}
        self.pinned_model = pinned_model

    def decide(self, state: Mapping[str, Any], timeout_s: float) -> Decision:
        sdk = self._sdk
        started = time.perf_counter()
        try:
            response = self._client.system_one(state=dict(state), questions=self._questions, timeout=timeout_s, retry=self._no_retry)
        except sdk.TypeSafeAPITimeoutError as error:
            raise DecisionTimeout(f"Jev did not answer within {timeout_s * 1000:.0f} ms") from error
        except sdk.TypeSafeError as error:
            raise DecisionUnavailable(f"Jev request failed: {error}") from error
        latency_ms = (time.perf_counter() - started) * 1000

        if self.pinned_model is not None and response.model != self.pinned_model:
            raise DecisionUnavailable(f"Jev answered with {response.model!r} but thresholds are calibrated for {self.pinned_model!r}")
        answers: dict[str, Answer] = {}
        for name, question in self._battery.items():
            if name not in response.answers:
                raise DecisionUnavailable(f"Jev returned no answer for {name!r}")
            answers[name] = self._from_sdk(name, question, response.answers[name])
        return Decision(answers, self.name, response.model, latency_ms, response.usage.input_tokens or 0)

    def list_models(self) -> list[str]:
        return [m.name for m in self._client.models.list().models]

    def close(self) -> None:
        self._client.close()

    def _to_sdk(self, question: Question) -> Any:
        sdk = self._sdk
        if isinstance(question, NoulQuestion):
            return sdk.Noul(instructions=question.instructions)
        if isinstance(question, ChoiceQuestion):
            return sdk.Choice(instructions=question.instructions, criteria=dict(question.labels))
        return sdk.Score(instructions=question.instructions, criteria=list(question.levels))

    def _from_sdk(self, name: str, question: Question, answer: Any) -> Answer:
        sdk = self._sdk
        if isinstance(question, NoulQuestion) and isinstance(answer, sdk.NoulAnswer):
            return NoulAnswer(answer.noul)
        if isinstance(question, ChoiceQuestion) and isinstance(answer, sdk.ChoiceAnswer):
            return ChoiceAnswer(answer.choice, answer.confidence, dict(answer.probabilities))
        if isinstance(question, ScoreQuestion) and isinstance(answer, sdk.ScoreAnswer):
            return ScoreAnswer(answer.score, answer.confidence, dict(answer.probabilities))
        raise DecisionUnavailable(f"Jev answered {name!r} with {type(answer).__name__} for a {type(question).__name__}")
