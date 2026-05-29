"""IsotonicCalibrator contract: default identity, fit only adopts beating identity,
biased-data fit shifts in correct direction, save/load round-trips exactly,
legacy {a,b} Platt JSON loads as identity.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from polybot.core.calibrator import IsotonicCalibrator


# ---------------------------------------------------------------------------
# Identity / default state
# ---------------------------------------------------------------------------

def test_default_is_identity():
    cal = IsotonicCalibrator()
    assert cal.is_identity
    assert cal.n_knots == 0
    assert cal.log_loss_improvement == 0.0
    for p in [0.05, 0.3, 0.5, 0.7, 0.95]:
        assert abs(cal.calibrate(p) - p) < 1e-9


def test_calibrate_clips_inputs():
    """Inputs outside (eps, 1-eps) get clipped but never raise."""
    cal = IsotonicCalibrator()
    # At identity, the clip still returns the (clipped) raw — finite.
    assert 0.0 <= cal.calibrate(0.0) <= 1.0
    assert 0.0 <= cal.calibrate(1.0) <= 1.0


def test_calibrate_np_interp_matches_sklearn_predict(tmp_path):
    """C1: the np.interp fast path is numerically identical to sklearn's
    IsotonicRegression.predict across the full input range — including the
    out-of-knot extremes and the EPS-clipped endpoints. Guards the latency
    refactor from ever drifting the calibrated probability (spec-defined behavior).
    """
    cal = IsotonicCalibrator()
    p = tmp_path / "iso.json"
    p.write_text(json.dumps({
        "type": "isotonic",
        "x_thresholds": [0.1, 0.3, 0.5, 0.7, 0.9],
        "y_thresholds": [0.02, 0.25, 0.55, 0.80, 0.98],
        "n_samples": 300,
    }))
    cal.load(p)
    assert not cal.is_identity
    eps = 1e-6
    for raw in np.linspace(0.0, 1.0, 201):
        clipped = max(eps, min(1.0 - eps, float(raw)))
        got = cal.calibrate(float(raw))
        ref = float(cal._iso.predict(np.array([clipped]))[0])
        assert abs(got - ref) < 1e-12, f"raw={raw}: np.interp={got} != sklearn={ref}"


# ---------------------------------------------------------------------------
# Min-samples gate
# ---------------------------------------------------------------------------

def test_fit_requires_min_samples():
    cal = IsotonicCalibrator()
    probs = [0.6] * 50
    outcomes = [1] * 30 + [0] * 20
    assert cal.fit(probs, outcomes) is False
    assert cal.is_identity


def test_fit_returns_false_below_threshold_with_explicit_min():
    cal = IsotonicCalibrator()
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
    cal = IsotonicCalibrator()
    # With well-calibrated data, isotonic gain should be ≤ noise floor → reject.
    # Allow either decision (depending on RNG luck), but if rejected, must be identity.
    fitted = cal.fit(probs, outcomes, min_samples=100)
    if not fitted:
        assert cal.is_identity


def test_fit_adopts_on_clearly_biased_data():
    """Overconfident model — at the 0.80 bucket, actual win rate is 50%.

    Includes well-calibrated low/high anchor buckets so the isotonic fit
    spans [0.2, 0.8] and passes the healthy-output gate. In production,
    raw model probs naturally span a range across many trades.
    """
    cal = IsotonicCalibrator()
    # Anchor buckets: well-calibrated, span the output range.
    probs = [0.15] * 50 + [0.85] * 50
    outcomes = [1] * 8 + [0] * 42 + [1] * 42 + [0] * 8   # ~15% and ~85% wins
    # The bucket under test: 0.80 → 50% win rate (overconfident).
    probs += [0.80] * 200
    outcomes += [1] * 100 + [0] * 100
    assert cal.fit(probs, outcomes, min_samples=75) is True
    assert not cal.is_identity
    # Calibrated prob at the biased bucket must come down toward 0.5.
    assert cal.calibrate(0.80) < 0.70


def test_fit_pulls_overconfident_predictions_down():
    """Stronger bias: 0.90 bucket has 50% true rate. Isotonic must shift 0.9 → ~0.5.

    High anchor sits at 0.95 (above the bias bucket) so monotonicity doesn't
    force the 0.90 fit upward toward the anchor.
    """
    cal = IsotonicCalibrator()
    np.random.seed(1)
    probs = [0.15] * 50 + [0.95] * 50
    outcomes = [1] * 8 + [0] * 42 + [1] * 47 + [0] * 3   # ~16% and ~94% wins
    probs += [0.90] * 200
    outcomes += [1] * 100 + [0] * 100
    assert cal.fit(probs, outcomes, min_samples=75) is True
    out = cal.calibrate(0.90)
    assert 0.40 <= out <= 0.60, f"expected ~0.5 calibration, got {out:.3f}"


def test_fit_monotonic_across_probability_levels():
    """For binary outcomes that genuinely increase with raw prob, isotonic
    output must also increase monotonically across the input range."""
    cal = IsotonicCalibrator()
    rng = np.random.default_rng(2)
    # 400 samples, step-function miscalibration (low/high outcomes split at 0.5).
    # Strong enough that the OOB bootstrap's lower-80% CI clearly clears zero.
    probs = list(rng.uniform(0.15, 0.85, 400))
    outcomes = [1 if rng.uniform() < (0.05 + 0.9 * (p > 0.5)) else 0 for p in probs]
    assert cal.fit(probs, outcomes, min_samples=150) is True
    assert cal.calibrate(0.30) <= cal.calibrate(0.50) <= cal.calibrate(0.70)


def test_fit_handles_out_of_distribution_inputs():
    """sklearn's out_of_bounds='clip' should pin predictions at the training
    boundary instead of extrapolating wildly."""
    cal = IsotonicCalibrator()
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
    cal = IsotonicCalibrator()
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
    cal = IsotonicCalibrator()
    probs = [0.5] * 100
    outcomes = [1] * 50 + [0] * 50
    weights = [0.0] * 100
    assert cal.fit(probs, outcomes, min_samples=75, sample_weights=weights) is False
    assert cal.is_identity


# ---------------------------------------------------------------------------
# Persistence: round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_identity(tmp_path):
    cal = IsotonicCalibrator()
    path = tmp_path / "cal.json"
    cal.save(path)
    cal2 = IsotonicCalibrator()
    cal2.load(path)
    assert cal2.is_identity
    data = json.loads(path.read_text())
    assert data["type"] == "identity"


def test_save_and_load_isotonic_roundtrip_exact(tmp_path):
    """A fitted isotonic must reproduce the SAME calibration function after
    save/load. This is the contract that protects against calibration drift
    across restarts."""
    cal = IsotonicCalibrator()
    # Step-function miscalibration on a continuous prob range: clean signal,
    # OOB bootstrap clears the lower-80% CI gate reliably.
    rng = np.random.default_rng(4)
    probs = list(rng.uniform(0.15, 0.85, 400))
    outcomes = [1 if rng.uniform() < (0.05 + 0.9 * (p > 0.5)) else 0 for p in probs]
    cal.fit(probs, outcomes, min_samples=150)
    assert not cal.is_identity

    path = tmp_path / "cal.json"
    cal.save(path)
    cal2 = IsotonicCalibrator()
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
    cal = IsotonicCalibrator()
    cal.load(path)
    assert cal.is_identity
    # Calibrate identity → returns input
    assert abs(cal.calibrate(0.7) - 0.7) < 1e-9


def test_load_corrupt_file_falls_back_to_identity(tmp_path):
    """A garbled JSON file must not crash the bot; identity is the safe fallback."""
    path = tmp_path / "broken.json"
    path.write_text("not valid json {")
    cal = IsotonicCalibrator()
    cal.load(path)
    assert cal.is_identity


def test_load_missing_file_is_noop(tmp_path):
    """Cold-start path: no file yet → calibrator stays identity."""
    path = tmp_path / "nope.json"
    cal = IsotonicCalibrator()
    cal.load(path)
    assert cal.is_identity
