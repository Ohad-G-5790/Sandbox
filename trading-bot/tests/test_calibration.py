import random

import pytest

from hftbot.calibration import PlattScaler, brier, calibration_report, expected_calibration_error, log_loss, reliability


def _draw(n, seed, distort=lambda p: p):
    rng = random.Random(seed)
    probs, outcomes = [], []
    for _ in range(n):
        p = rng.random()
        probs.append(distort(p))
        outcomes.append(int(rng.random() < p))
    return probs, outcomes


def test_brier_and_log_loss_known_values():
    assert brier([1.0, 0.0], [1, 0]) == 0.0
    assert brier([0.5, 0.5], [1, 0]) == 0.25
    assert log_loss([0.5], [1]) == pytest.approx(0.6931, abs=1e-4)


def test_calibrated_probabilities_have_low_ece():
    probs, outcomes = _draw(20_000, 1)
    assert expected_calibration_error(probs, outcomes) < 0.02
    curve = reliability(probs, outcomes)
    assert all(abs(b.mean_predicted - b.observed_frequency) < 0.05 for b in curve)


def test_platt_scaling_repairs_overconfidence():
    def overconfident(p):
        return min(max(0.5 + (p - 0.5) * 1.8, 0.01), 0.99)

    probs, outcomes = _draw(20_000, 2, overconfident)
    before = expected_calibration_error(probs, outcomes)
    scaler = PlattScaler().fit(probs, outcomes)
    after = expected_calibration_error([scaler.transform(p) for p in probs], outcomes)
    assert scaler.a < 1.0  # it learned to shrink the extremes
    assert after < before / 2


def test_report_labels_outcomes_from_later_blocks():
    def rec(block, mid, p_up, buy_ratio=0.5, toxic=0.5):
        return {
            "block": block,
            "mid": mid,
            "snapshot": {"book": {"spread_bps": 2.0}, "flow_1m": {"buy_ratio": buy_ratio}},
            "decision": {
                "answers": {
                    "direction": {"type": "choice", "probabilities": {"up": p_up, "down": 1 - p_up, "neutral": 0.0}},
                    "toxic_flow": {"type": "noul", "p": toxic},
                }
            },
        }

    records = [rec(0, 100.0, 0.9, buy_ratio=0.8, toxic=0.8), rec(1, 100.0, 0.2), rec(2, 100.1, 0.5), rec(3, 99.9, 0.5)]
    reports = {(r.question, r.label): r for r in calibration_report(records, horizon_blocks=2)}
    up = reports[("direction", "up")]
    assert up.n == 2 and up.base_rate == 0.5  # block 0 -> 2 went up 10 bps, block 1 -> 3 went down
    assert reports[("toxic_flow", "yes")].base_rate == 0.5


def test_appended_runs_are_labeled_within_their_own_run():
    def rec(block, mid):
        return {
            "block": block,
            "mid": mid,
            "snapshot": {"book": {"spread_bps": 2.0}, "flow_1m": {"buy_ratio": 0.5}},
            "decision": {"answers": {"direction": {"type": "choice", "probabilities": {"up": 0.6, "down": 0.4, "neutral": 0.0}}}},
        }

    # Run 1 goes up after block 1; run 2 restarts numbering and goes down. Mixing them would mislabel.
    records = [rec(1, 100.0), rec(2, 100.5), rec(1, 100.0), rec(2, 99.5)]
    up = {r.label: r for r in calibration_report(records, horizon_blocks=1)}["up"]
    assert up.n == 2 and up.base_rate == 0.5
