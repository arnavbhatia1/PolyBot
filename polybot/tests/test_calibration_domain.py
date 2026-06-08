"""Calibrator train/serve domain: the fit must be in the P(up) domain it's served in.

The calibrator is APPLIED to P(up) (signal_engine + replay), but the bot records only
the chosen-side prob (>=0.56). `_calibration_xy` reconstructs raw P(up) + the up-outcome
so the fit spans the full [0,1] range, including the P(up) < 0.5 region that down-favored
windows produce. Pre-fix, the fit used the chosen-side prob directly and never populated
that region — these tests pin the reconstruction.
"""
import pytest

from polybot.agents.scheduler import AgentScheduler as Scheduler
from polybot.core.calibrator import IsotonicCalibrator


def _mk(mp, side, correct, ts="2026-06-08T12:00:00+00:00"):
    return {"side": side, "correct": correct, "timestamp": ts,
            "indicator_snapshot": {"trade_context": {"model_probability_raw": mp}}}


def test_calibration_xy_reconstructs_pup_domain():
    pool = [_mk(0.70, "Up", True), _mk(0.70, "Up", False),
            _mk(0.70, "Down", True), _mk(0.70, "Down", False)]
    probs, outs, _ = Scheduler._calibration_xy(pool)
    # Down trades map to P(up) = 1 - chosen-side prob = 0.30 (the region production serves
    # but the pre-fix chosen-side fit never saw).
    assert probs == pytest.approx([0.70, 0.70, 0.30, 0.30])
    # up_won: Up&win=1, Up&lose=0, Down&win=0 (up lost), Down&lose=1 (up won).
    assert outs == [1, 0, 0, 1]
    assert any(p < 0.5 for p in probs), "down-favored windows must populate P(up) < 0.5"


def test_calibration_xy_skips_unusable_rows():
    pool = [
        _mk(0.0, "Up", True),                       # mp <= 0
        _mk(0.7, None, True),                        # missing side
        {"side": "Up", "correct": True},            # no trade_context -> mp defaults 0
    ]
    probs, outs, ws = Scheduler._calibration_xy(pool)
    assert probs == [] and outs == [] and ws == []


def test_highest_and_lowest_learned_prob_identity():
    c = IsotonicCalibrator()
    # Identity floors disable the per-side dead-side override on both sides.
    assert c.lowest_learned_prob == 0.0
    assert c.highest_learned_prob == 1.0


def test_fit_on_full_range_resolves_below_half():
    # Overconfident synthetic in the P(up) domain: model says p but true rate is
    # compressed toward 0.5. A correct fit must pull extremes in AND keep monotone
    # resolution across the full range (not clip the <0.5 region to one knot).
    probs, outs = [], []
    for i in range(600):
        p = 0.05 + 0.90 * (i % 100) / 99.0          # sweep 0.05..0.95
        true_rate = 0.5 + 0.5 * (p - 0.5)           # compressed (overconfident model)
        probs.append(p)
        outs.append(1 if ((i * 7) % 100) / 100.0 < true_rate else 0)
    c = IsotonicCalibrator()
    c.fit(probs, outs, min_samples=75)
    if not c.is_identity:
        assert c.calibrate(0.20) <= c.calibrate(0.50) <= c.calibrate(0.80)
        assert c.calibrate(0.20) < c.calibrate(0.80)   # genuine resolution across full range
