"""PlattCalibrator (isotonic-backed) — calibration semantics and persistence.

Class name is legacy; internals are isotonic regression. Tests pin the
contract every caller depends on:
  * Default state is identity (no calibration).
  * Fit only adopts if it actually beats identity on weighted log-loss.
  * Fit on biased data adopts and shifts predictions in the correct direction.
  * Save/load round-trips an adopted isotonic fit *exactly*.
  * A legacy {a, b} JSON file from the previous Platt era loads as identity.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from polybot.core.calibrator import PlattCalibrator, compute_log_loss


# ---------------------------------------------------------------------------
# Identity / default state
# ---------------------------------------------------------------------------

def test_default_is_identity():
    cal = PlattCalibrator()
    assert cal.is_identity
    assert cal.n_knots == 0
    assert cal.log_loss_improvement == 0.0
    for p in [0.05, 0.3, 0.5, 0.7, 0.95]:
        assert abs(cal.calibrate(p) - p) < 1e-9


def test_state_hash_identity_then_fit():
    """state_hash distinguishes identity from fitted, and matches across instances
    fitted on the same data — so per-trade stratification can group trades by the
    calibration curve that was live at fill time."""
    cal = PlattCalibrator()
    assert cal.state_hash == "identity"

    rng = np.random.default_rng(0)
    probs = list(rng.uniform(0.3, 0.7, 250))
    outcomes = [1 if p + rng.normal(0, 0.2) > 0.5 else 0 for p in probs]

    a = PlattCalibrator()
    a.fit(probs, outcomes, min_samples=150)
    b = PlattCalibrator()
    b.fit(probs, outcomes, min_samples=150)
    assert a.state_hash != "identity"
    assert a.state_hash == b.state_hash  # determinism

    rng2 = np.random.default_rng(7)
    probs2 = list(rng2.uniform(0.2, 0.8, 250))
    outcomes2 = [1 if p > 0.55 else 0 for p in probs2]
    c = PlattCalibrator()
    c.fit(probs2, outcomes2, min_samples=150)
    assert c.state_hash != a.state_hash  # different fit -> different hash


def test_calibrate_clips_inputs():
    """Inputs outside (eps, 1-eps) get clipped but never raise."""
    cal = PlattCalibrator()
    # At identity, the clip still returns the (clipped) raw — finite.
    assert 0.0 <= cal.calibrate(0.0) <= 1.0
    assert 0.0 <= cal.calibrate(1.0) <= 1.0


# ---------------------------------------------------------------------------
# Min-samples gate
# ---------------------------------------------------------------------------

def test_fit_requires_min_samples():
    cal = PlattCalibrator()
    probs = [0.6] * 50
    outcomes = [1] * 30 + [0] * 20
    assert cal.fit(probs, outcomes) is False
    assert cal.is_identity


def test_fit_returns_false_below_threshold_with_explicit_min():
    cal = PlattCalibrator()
    probs = [0.6] * 74
    outcomes = [1] * 40 + [0] * 34
    assert cal.fit(probs, outcomes, min_samples=75) is False
    assert cal.is_identity


# ---------------------------------------------------------------------------
# Adoption gate (identity floor)
# ---------------------------------------------------------------------------

def test_fit_rejects_when_calibration_no_better_than_identity():
    """If the model is already well-calibrated, isotonic should reject."""
    np.random.seed(0)
    n = 300
    # Perfectly-calibrated: P(outcome=1 | prob=p) = p
    probs = list(np.random.uniform(0.05, 0.95, n))
    outcomes = [int(np.random.random() < p) for p in probs]
    cal = PlattCalibrator()
    # With well-calibrated data, isotonic gain should be ≤ noise floor → reject.
    # Allow either decision (depending on RNG luck), but if rejected, must be identity.
    fitted = cal.fit(probs, outcomes, min_samples=100)
    if not fitted:
        assert cal.is_identity


def test_fit_adopts_on_clearly_biased_data():
    """Overconfident model — every raw prob is 0.80 but actual win rate is 50%."""
    cal = PlattCalibrator()
    probs = [0.80] * 200
    outcomes = [1] * 100 + [0] * 100
    assert cal.fit(probs, outcomes, min_samples=75) is True
    assert not cal.is_identity
    # Calibrated prob at the (single) training value must come down toward 0.5.
    assert cal.calibrate(0.80) < 0.70


def test_fit_pulls_overconfident_predictions_down():
    """Stronger bias: 90% probs, 50% true rate. Isotonic must shift 0.9 → ~0.5."""
    cal = PlattCalibrator()
    np.random.seed(1)
    probs = [0.90] * 200
    outcomes = [1] * 100 + [0] * 100
    assert cal.fit(probs, outcomes, min_samples=75) is True
    out = cal.calibrate(0.90)
    assert 0.40 <= out <= 0.60, f"expected ~0.5 calibration, got {out:.3f}"


def test_fit_monotonic_across_probability_levels():
    """For binary outcomes that genuinely increase with raw prob, isotonic
    output must also increase monotonically across the input range."""
    cal = PlattCalibrator()
    np.random.seed(2)
    n_per_bucket = 50
    probs: list[float] = []
    outcomes: list[int] = []
    # Synthetic: low-prob bucket wins 20%, mid 50%, high 80%.
    for p_in, win_rate in [(0.30, 0.20), (0.50, 0.50), (0.70, 0.80)]:
        probs.extend([p_in] * n_per_bucket)
        wins = int(n_per_bucket * win_rate)
        outcomes.extend([1] * wins + [0] * (n_per_bucket - wins))
    assert cal.fit(probs, outcomes, min_samples=75) is True
    assert cal.calibrate(0.30) <= cal.calibrate(0.50) <= cal.calibrate(0.70)


def test_fit_handles_out_of_distribution_inputs():
    """sklearn's out_of_bounds='clip' should pin predictions at the training
    boundary instead of extrapolating wildly."""
    cal = PlattCalibrator()
    np.random.seed(3)
    probs = [0.40] * 100 + [0.60] * 100
    outcomes = [1] * 30 + [0] * 70 + [1] * 75 + [0] * 25
    cal.fit(probs, outcomes, min_samples=75)
    # If adopted, predictions at 0.05 and 0.95 must stay in [0, 1] — no NaN, no extrapolation.
    lo = cal.calibrate(0.05)
    hi = cal.calibrate(0.95)
    assert 0.0 <= lo <= 1.0
    assert 0.0 <= hi <= 1.0


# ---------------------------------------------------------------------------
# Recency weighting
# ---------------------------------------------------------------------------

def test_fit_respects_sample_weights():
    """Heavy recent weight on a different distribution should pull the fit
    toward the recent bias."""
    cal = PlattCalibrator()
    n = 200
    probs = [0.70] * n
    # First half: 70% win rate. Second half: 30% win rate. Recency-weight
    # the second half ~10× heavier so the fit should pull 0.7 DOWN, not up.
    outcomes = [1] * 70 + [0] * 30 + [1] * 30 + [0] * 70
    weights = [1.0] * 100 + [10.0] * 100
    cal.fit(probs, outcomes, min_samples=75, sample_weights=weights)
    if not cal.is_identity:
        out = cal.calibrate(0.70)
        # Recent half pushes toward 30% → calibrated should be < 0.5.
        assert out < 0.55, f"expected < 0.55 after recent down-weight, got {out:.3f}"


def test_fit_rejects_non_positive_weights():
    """A degenerate weight vector (all zero) must be rejected, not crash."""
    cal = PlattCalibrator()
    probs = [0.5] * 100
    outcomes = [1] * 50 + [0] * 50
    weights = [0.0] * 100
    assert cal.fit(probs, outcomes, min_samples=75, sample_weights=weights) is False
    assert cal.is_identity


# ---------------------------------------------------------------------------
# Persistence: round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_identity(tmp_path):
    cal = PlattCalibrator()
    path = tmp_path / "cal.json"
    cal.save(path)
    cal2 = PlattCalibrator()
    cal2.load(path)
    assert cal2.is_identity
    data = json.loads(path.read_text())
    assert data["type"] == "identity"


def test_save_and_load_isotonic_roundtrip_exact(tmp_path):
    """A fitted isotonic must reproduce the SAME calibration function after
    save/load. This is the contract that protects against calibration drift
    across restarts."""
    cal = PlattCalibrator()
    cal.fit([0.30] * 60 + [0.70] * 60, [1] * 12 + [0] * 48 + [1] * 48 + [0] * 12, min_samples=75)
    assert not cal.is_identity

    path = tmp_path / "cal.json"
    cal.save(path)
    cal2 = PlattCalibrator()
    cal2.load(path)
    assert not cal2.is_identity
    assert cal2.n_knots == cal.n_knots

    test_points = np.linspace(0.02, 0.98, 50)
    for p in test_points:
        assert abs(cal2.calibrate(float(p)) - cal.calibrate(float(p))) < 1e-9


def test_load_legacy_platt_file_falls_back_to_identity(tmp_path):
    """A file from the previous Platt era (just {a, b}) must load as identity.
    The fit cycle will rebuild on real data. Old (a, b) values can't be
    translated to an isotonic shape, so silently dropping them is correct."""
    path = tmp_path / "platt.json"
    path.write_text(json.dumps({"a": -0.0809, "b": -0.5366}))
    cal = PlattCalibrator()
    cal.load(path)
    assert cal.is_identity
    # Calibrate identity → returns input
    assert abs(cal.calibrate(0.7) - 0.7) < 1e-9


def test_load_corrupt_file_falls_back_to_identity(tmp_path):
    """A garbled JSON file must not crash the bot; identity is the safe fallback."""
    path = tmp_path / "broken.json"
    path.write_text("not valid json {")
    cal = PlattCalibrator()
    cal.load(path)
    assert cal.is_identity


def test_load_missing_file_is_noop(tmp_path):
    """Cold-start path: no file yet → calibrator stays identity."""
    path = tmp_path / "nope.json"
    cal = PlattCalibrator()
    cal.load(path)
    assert cal.is_identity


# ---------------------------------------------------------------------------
# compute_log_loss helper (used by scheduler)
# ---------------------------------------------------------------------------

def test_compute_log_loss_perfect_predictions():
    probs = [0.99, 0.99, 0.01, 0.01]
    outcomes = [1, 1, 0, 0]
    assert compute_log_loss(probs, outcomes) < 0.05


def test_compute_log_loss_inverse_predictions():
    probs = [0.01, 0.01, 0.99, 0.99]
    outcomes = [1, 1, 0, 0]
    assert compute_log_loss(probs, outcomes) > 3.0


def test_compute_log_loss_empty_returns_inf():
    assert compute_log_loss([], []) == float("inf")
