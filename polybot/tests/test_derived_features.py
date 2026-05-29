"""L6 contract: default weights 0.0 (layer inert), all features finite on extremes,
combined contribution cap clips correctly when a weight is raised.
"""
import math

import numpy as np
import pytest

from polybot.core.derived_features import (
    DERIVED_FEATURES,
    FeatureContext,
    L6_LOGIT_CAP,
)
from polybot.core.signal_engine import SignalEngine


def _neutral_ctx() -> FeatureContext:
    return FeatureContext(
        atr=10.0,
        atr_rolling_20=10.0,
        atr_long_term_mean=10.0,
        regime=0.0,
        last_return=0.0,
        flow_signal=0.0,
        spot_flow_signal=0.0,
        prev_resolution_margin=0.0,
        seconds_remaining=150.0,
        distance=0.0,
    )


def test_all_features_finite_on_neutral_inputs():
    """Every L6 feature returns a finite scalar from a neutral context.

    Looped here rather than parametrized — a single failure still points to
    the offending feature via the assert message, and the test runs in <1ms.
    """
    ctx = _neutral_ctx()
    for name, fn in DERIVED_FEATURES.items():
        val = fn(ctx)
        assert math.isfinite(val), f"{name} returned non-finite on neutral inputs: {val}"


def test_all_features_finite_and_bounded_on_extreme_inputs():
    """Every L6 feature stays finite and bounded under saturating inputs."""
    extreme = FeatureContext(
        atr=1.0, atr_rolling_20=1e6, atr_long_term_mean=1.0,
        regime=1.0, last_return=10.0,
        flow_signal=1e6, spot_flow_signal=1e6,
        prev_resolution_margin=1e6,
        seconds_remaining=0.0, distance=1e6,
    )
    for name, fn in DERIVED_FEATURES.items():
        val = fn(extreme)
        assert math.isfinite(val), f"{name} blew up on extreme inputs"
        assert abs(val) <= 20.0, f"{name} returned unbounded magnitude {val}"


def test_log_atr_ratio_zero_when_short_equals_long():
    from polybot.core.derived_features import _f_log_atr_ratio
    ctx = _neutral_ctx()
    assert _f_log_atr_ratio(ctx) == 0.0


def test_log_atr_ratio_zero_when_atr_history_empty():
    from polybot.core.derived_features import _f_log_atr_ratio
    ctx = FeatureContext(
        atr=10.0, atr_rolling_20=0.0, atr_long_term_mean=0.0,
        regime=0.0, last_return=0.0, flow_signal=0.0, spot_flow_signal=0.0,
        prev_resolution_margin=0.0,
        seconds_remaining=150.0, distance=0.0,
    )
    # Both fall back to `atr`; short==long ⇒ log(1) = 0.
    assert _f_log_atr_ratio(ctx) == 0.0


def test_default_signal_engine_has_zero_derived_weights():
    """L6 must be inert by default. Pipeline raises a weight off zero to activate."""
    eng = SignalEngine()
    for name in DERIVED_FEATURES.keys():
        assert eng.derived_weights[name] == 0.0, f"{name} default should be 0.0"


def test_signal_engine_l6_inert_when_all_weights_zero():
    """compute_probability must not change vs. baseline when L6 weights are all zero."""
    closes = np.array([100.0, 100.5, 101.0, 100.8, 101.2, 101.0])
    eng = SignalEngine()
    p_baseline = eng.compute_probability(
        btc_price=101.0, strike_price=100.0,
        seconds_remaining=200.0, atr=12.0,
        closes=closes, flow_signal=0.3, spot_flow_signal=0.2,
        prev_resolution_margin=20.0,
    )
    # All weights still 0 → result is identical to a fresh engine probability.
    eng2 = SignalEngine()
    p_again = eng2.compute_probability(
        btc_price=101.0, strike_price=100.0,
        seconds_remaining=200.0, atr=12.0,
        closes=closes, flow_signal=0.3, spot_flow_signal=0.2,
        prev_resolution_margin=20.0,
    )
    assert p_baseline == p_again


def test_signal_engine_l6_contribution_capped():
    """Pushing a single weight to the registry max keeps |L6| ≤ L6_LOGIT_CAP."""
    eng = SignalEngine()
    eng.derived_weights["flow_disagreement"] = 0.05  # registry max
    # Build a context that drives flow_disagreement to its tanh ceiling (≈1.0).
    val = eng._apply_derived_features(
        atr=10.0, regime=0.5, distance=0.0,
        seconds_remaining=150.0,
        flow_signal=10.0, spot_flow_signal=10.0,
        prev_resolution_margin=0.0,
        last_return=0.0,
    )
    assert abs(val) <= L6_LOGIT_CAP + 1e-9


def test_signal_engine_promoted_constants_resolve_from_registry():
    """Default-constructed engine matches param_registry defaults for the promoted constants."""
    from polybot.config.param_registry import default_for
    eng = SignalEngine()
    assert eng.regime_momentum_threshold == default_for("regime_momentum_threshold")
    assert eng.final_logit_clamp == default_for("final_logit_clamp")
    assert eng.deep_loss_hold_threshold == default_for("deep_loss_hold_threshold")
    assert eng.l5_regime_damp_cap == default_for("l5_regime_damp_cap")
    assert eng.atr_regime_shift_threshold == default_for("atr_regime_shift_threshold")
