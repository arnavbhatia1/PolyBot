"""Regime-conditional momentum weight + dynamic ATR floor."""
from polybot.core.signal_engine import (
    SignalEngine,
    _REGIME_MOMENTUM_THRESHOLD,
    _REGIME_MOMENTUM_AMPLIFY,
    _REGIME_MOMENTUM_DAMPEN,
    _MOMENTUM_WEIGHT_CLAMP,
    _ATR_HISTORY_MIN_SAMPLES,
    _ATR_FLOOR_FRACTION,
)


def _engine(mw: float = -0.02, min_atr: float = 8.0) -> SignalEngine:
    return SignalEngine(
        min_edge=0.04,
        kelly_fraction=0.15,
        momentum_weight=mw,
        weights={"rsi": 0.2, "macd": 0.25, "stochastic": 0.2, "obv": 0.15, "vwap": 0.2},
        min_model_probability=0.58,
        min_atr=min_atr,
    )


# --- Regime-conditional momentum --------------------------------------------------

def test_momentum_weight_flips_sign_in_trending_regime():
    eng = _engine(mw=-0.02)
    # Trending: |autocorr| > threshold, positive. L4 should switch from fade to follow.
    result = eng.effective_momentum_weight(regime_autocorr=0.30)
    # Expected: +|mw| * amplify = +0.03
    assert result == 0.02 * _REGIME_MOMENTUM_AMPLIFY
    assert result > 0


def test_momentum_weight_amplifies_in_mean_reverting_regime():
    eng = _engine(mw=-0.02)
    result = eng.effective_momentum_weight(regime_autocorr=-0.30)
    # Mean-reverting: -|mw| * amplify = -0.03
    assert result == -0.02 * _REGIME_MOMENTUM_AMPLIFY
    assert result < 0


def test_momentum_weight_dampened_when_autocorr_in_noise_band():
    eng = _engine(mw=-0.02)
    for rho in [0.0, 0.10, -0.10, _REGIME_MOMENTUM_THRESHOLD]:
        result = eng.effective_momentum_weight(regime_autocorr=rho)
        assert result == -0.02 * _REGIME_MOMENTUM_DAMPEN


def test_momentum_weight_clamped_to_invariant():
    eng = _engine(mw=-0.08)  # Already close to ±0.10 cap.
    # Trending + 1.5x amplify would be 0.12 > 0.10 clamp.
    assert eng.effective_momentum_weight(0.50) == _MOMENTUM_WEIGHT_CLAMP
    # Mean-reverting + 1.5x amplify would be -0.12 < -0.10 clamp.
    assert eng.effective_momentum_weight(-0.50) == -_MOMENTUM_WEIGHT_CLAMP


# --- Dynamic ATR floor ------------------------------------------------------------

def test_atr_floor_falls_back_to_static_before_warmup():
    eng = _engine(min_atr=8.0)
    assert eng._effective_atr_floor() == 8.0  # empty history
    for _ in range(_ATR_HISTORY_MIN_SAMPLES - 1):
        eng._record_atr(100.0)
    assert eng._effective_atr_floor() == 8.0  # still below warmup


def test_atr_floor_lifts_in_high_vol_regime():
    eng = _engine(min_atr=8.0)
    # Rolling mean $200 → dynamic floor = 0.3 × 200 = $60, above static $8.
    for _ in range(_ATR_HISTORY_MIN_SAMPLES + 5):
        eng._record_atr(200.0)
    expected = max(8.0, _ATR_FLOOR_FRACTION * 200.0)
    assert eng._effective_atr_floor() == expected


def test_atr_floor_keeps_static_floor_in_dead_market():
    eng = _engine(min_atr=8.0)
    # Rolling mean $10 → dynamic = 0.3 × 10 = $3, below static $8. Static wins.
    for _ in range(_ATR_HISTORY_MIN_SAMPLES + 5):
        eng._record_atr(10.0)
    assert eng._effective_atr_floor() == 8.0


def test_atr_history_records_only_positive_values():
    eng = _engine()
    eng._record_atr(0.0)
    eng._record_atr(-5.0)
    assert len(eng._atr_history) == 0
    eng._record_atr(50.0)
    assert len(eng._atr_history) == 1
