import math
import json
import pytest
from polybot.core.calibrator import PlattCalibrator, compute_log_loss


def test_identity_calibration():
    """Default params (a=-1, b=0) return input unchanged."""
    cal = PlattCalibrator()
    assert cal.is_identity
    for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
        assert abs(cal.calibrate(p) - p) < 0.001


def test_fit_requires_min_samples():
    """Returns False with < 100 outcomes."""
    cal = PlattCalibrator()
    probs = [0.6] * 50
    outcomes = [1] * 30 + [0] * 20
    assert cal.fit(probs, outcomes) is False
    assert cal.is_identity


def test_fit_with_biased_data():
    """Fit on data where model overestimates -> a and b shift to correct."""
    cal = PlattCalibrator()
    probs = [0.80] * 200
    outcomes = [1] * 100 + [0] * 100
    assert cal.fit(probs, outcomes) is True
    assert not cal.is_identity
    calibrated = cal.calibrate(0.80)
    assert calibrated < 0.70


def test_save_and_load(tmp_path):
    """Round-trip persistence to JSON."""
    cal = PlattCalibrator(a=-0.8, b=0.1)
    path = tmp_path / "platt.json"
    cal.save(path)
    cal2 = PlattCalibrator()
    cal2.load(path)
    assert abs(cal2.a - (-0.8)) < 1e-6
    assert abs(cal2.b - 0.1) < 1e-6


def test_calibration_improves_log_loss():
    """Calibrated probs have lower log-loss on holdout."""
    import numpy as np
    np.random.seed(42)
    n = 300
    probs = [0.70] * n
    outcomes = list(np.random.binomial(1, 0.55, n))
    cal = PlattCalibrator()
    cal.fit(probs[:200], outcomes[:200])
    holdout_probs = probs[200:]
    holdout_outcomes = outcomes[200:]
    raw_loss = compute_log_loss(holdout_probs, holdout_outcomes)
    cal_probs = [cal.calibrate(p) for p in holdout_probs]
    cal_loss = compute_log_loss(cal_probs, holdout_outcomes)
    assert cal_loss < raw_loss


def test_compute_log_loss():
    """Sanity check: perfect predictions have near-zero loss."""
    probs = [0.99, 0.99, 0.01, 0.01]
    outcomes = [1, 1, 0, 0]
    loss = compute_log_loss(probs, outcomes)
    assert loss < 0.05
    bad_probs = [0.01, 0.01, 0.99, 0.99]
    bad_loss = compute_log_loss(bad_probs, outcomes)
    assert bad_loss > 3.0
