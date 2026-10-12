"""JevEngine against the real TypeSafe SDK with a mocked HTTP transport (no network, no API key needed)."""

import json

import pytest

sdk = pytest.importorskip("typesafe_sdk")
httpx2 = pytest.importorskip("httpx2")

from hftbot.jev import JevEngine  # noqa: E402
from hftbot.judgments import DecisionTimeout, DecisionUnavailable, default_battery  # noqa: E402

ANSWERS = {
    "regime": {"type": "choice", "choice": "trending", "confidence": 0.63, "probabilities": {"trending": 0.63, "mean_reverting": 0.22, "high_vol": 0.1, "crisis": 0.05}},
    "direction": {"type": "choice", "choice": "up", "confidence": 0.55, "probabilities": {"up": 0.55, "down": 0.25, "neutral": 0.2}},
    "toxic_flow": {"type": "noul", "noul": 0.83},
    "liquidity_stressed": {"type": "noul", "noul": 0.12},
    "quote_environment": {"type": "score", "score": 2.3, "confidence": 0.81, "legend": {"0": "Do not quote", "1": "Marginal", "2": "Standard", "3": "Excellent"}, "probabilities": {"0": 0.05, "1": 0.1, "2": 0.35, "3": 0.5}},
    "inventory_pressure": {"type": "score", "score": 0.4, "confidence": 0.9, "legend": {"0": "None", "1": "Mild", "2": "Skew quotes hard", "3": "Reduce now"}, "probabilities": {"0": 0.7, "1": 0.2, "2": 0.1, "3": 0.0}},
}


def engine_with(handler, **kwargs):
    client = sdk.TypeSafeClient(api_key="test-key", transport=httpx2.MockTransport(handler), retry=sdk.RetryPolicy(max_retries=0))
    return JevEngine(client=client, **kwargs)


def ok(model="jev-2026-09-15", answers=ANSWERS):
    def handler(request):
        handler.body = json.loads(request.content)
        handler.headers = request.headers
        return httpx2.Response(200, json={"model": model, "answers": answers, "usage": {"input_tokens": 512, "output_tokens": 6}})

    return handler


def test_request_uses_the_real_wire_format():
    handler = ok()
    engine_with(handler).decide({"price": {"mid": 100.0}}, timeout_s=0.25)
    body = handler.body
    assert body["state"] == {"price": {"mid": 100.0}}
    assert body["model"] == "jev-latest"
    assert set(body["questions"]) == set(default_battery())
    # Score questions take an ordered `criteria` list (the article's `legend=` keyword does not exist).
    assert body["questions"]["quote_environment"]["type"] == "score"
    assert body["questions"]["quote_environment"]["criteria"] == ["Do not quote", "Marginal", "Standard", "Excellent"]
    assert body["questions"]["toxic_flow"] == {"type": "noul", "instructions": default_battery()["toxic_flow"].instructions}
    assert handler.headers["authorization"].endswith("test-key")


def test_response_is_parsed_into_typed_answers():
    decision = engine_with(ok()).decide({"x": 1}, timeout_s=0.25)
    assert decision.source == "jev" and decision.model == "jev-2026-09-15" and decision.input_tokens == 512
    assert decision.noul("toxic_flow") == 0.83
    assert decision.choice("regime").probabilities["trending"] == 0.63
    q = decision.score("quote_environment")
    assert q.score == 2.3 and q.confidence == 0.81 and q.probabilities[3] == 0.5


def test_pinned_model_mismatch_is_refused():
    with pytest.raises(DecisionUnavailable, match="calibrated for"):
        engine_with(ok(model="jev-2026-11-01"), pinned_model="jev-2026-09-15").decide({"x": 1}, 0.25)


def test_missing_answer_is_refused():
    partial = {k: v for k, v in ANSWERS.items() if k != "toxic_flow"}
    with pytest.raises(DecisionUnavailable, match="toxic_flow"):
        engine_with(ok(answers=partial)).decide({"x": 1}, 0.25)


def test_server_error_is_unavailable_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(503, json={"detail": "overloaded"})

    with pytest.raises(DecisionUnavailable):
        engine_with(handler).decide({"x": 1}, 0.25)
    assert len(calls) == 1


def test_timeout_maps_to_decision_timeout():
    def handler(request):
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(DecisionTimeout):
        engine_with(handler).decide({"x": 1}, 0.25)
