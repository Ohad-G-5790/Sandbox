from __future__ import annotations

from hftbot.judgments import ChoiceAnswer, Decision, NoulAnswer, ScoreAnswer
from hftbot.market import Block, Book, Level, Side, Trade


def book(bid: float = 99.99, ask: float = 100.01, size: float = 10.0, ts: float = 0.0, levels: int = 3, tick: float = 0.01) -> Book:
    return Book(
        ts,
        tuple(Level(round(bid - k * tick, 10), size) for k in range(levels)),
        tuple(Level(round(ask + k * tick, 10), size) for k in range(levels)),
    )


def block(n: int, bid: float = 99.99, ask: float = 100.01, trades: tuple[Trade, ...] = (), size: float = 10.0, dt: float = 0.3) -> Block:
    ts = 1_000.0 + n * dt
    return Block(n, ts, book(bid, ask, size, ts), trades)


def sell(price: float, size: float, ts: float = 0.0) -> Trade:
    return Trade(ts, price, size, Side.SELL)


def buy(price: float, size: float, ts: float = 0.0) -> Trade:
    return Trade(ts, price, size, Side.BUY)


def decision(
    toxic: float = 0.1,
    stressed: float = 0.1,
    quote_env: float = 2.5,
    quote_conf: float = 0.9,
    pressure: float = 0.0,
    up: float = 0.3,
    down: float = 0.3,
    latency_ms: float = 50.0,
) -> Decision:
    return Decision(
        {
            "regime": ChoiceAnswer("mean_reverting", 0.7, {"trending": 0.1, "mean_reverting": 0.7, "high_vol": 0.1, "crisis": 0.1}),
            "direction": ChoiceAnswer("neutral", 0.4, {"up": up, "down": down, "neutral": 1 - up - down}),
            "toxic_flow": NoulAnswer(toxic),
            "liquidity_stressed": NoulAnswer(stressed),
            "quote_environment": ScoreAnswer(quote_env, quote_conf, {0: 0.0, 1: 0.0, 2: 0.5, 3: 0.5}),
            "inventory_pressure": ScoreAnswer(pressure, 0.9, {0: 1.0, 1: 0.0, 2: 0.0, 3: 0.0}),
        },
        "test",
        "test-model",
        latency_ms,
    )


class FixedEngine:
    """Returns the same decision every block, or raises a given exception."""

    name = "fixed"

    def __init__(self, result: Decision | Exception) -> None:
        self.result = result
        self.calls = 0

    def decide(self, state, timeout_s):  # noqa: ANN001, ANN201
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
