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
#
# After the L4 polarity-split refactor:
#   * effective_momentum_weight returns an UNSIGNED magnitude (regime-amplified).
#   * Sign/polarity is handled per indicator group inside compute_momentum.

def test_momentum_magnitude_amplified_in_trending_regime():
    eng = _engine(mw=-0.02)
    result = eng.effective_momentum_weight(regime_autocorr=0.30)
    assert result == 0.02 * _REGIME_MOMENTUM_AMPLIFY
    assert result > 0


def test_momentum_magnitude_amplified_in_mean_reverting_regime():
    eng = _engine(mw=-0.02)
    result = eng.effective_momentum_weight(regime_autocorr=-0.30)
    # Same magnitude as trending — polarity is no longer encoded here.
    assert result == 0.02 * _REGIME_MOMENTUM_AMPLIFY


def test_momentum_magnitude_dampened_when_autocorr_in_noise_band():
    eng = _engine(mw=-0.02)
    for rho in [0.0, 0.10, -0.10, _REGIME_MOMENTUM_THRESHOLD]:
        result = eng.effective_momentum_weight(regime_autocorr=rho)
        assert result == 0.02 * _REGIME_MOMENTUM_DAMPEN


def test_momentum_magnitude_clamped_to_invariant():
    eng = _engine(mw=-0.08)  # |mw| × 1.5 = 0.12 > 0.10 clamp.
    assert eng.effective_momentum_weight(0.50) == _MOMENTUM_WEIGHT_CLAMP
    assert eng.effective_momentum_weight(-0.50) == _MOMENTUM_WEIGHT_CLAMP


def test_compute_momentum_flips_mean_revert_in_trending_regime():
    """Mean-revert indicators (RSI/Stoch/VWAP) get sign-flipped in trending so
    they align with the trend rather than fighting it."""
    eng = _engine(mw=-0.02)
    indicators = {
        "rsi": {"score": -0.6},        # overbought, fade signal
        "stochastic": {"score": -0.5},
        "vwap": {"score": -0.4},        # price above vwap
        "macd": {"score": 0.5},         # trend-confirm bullish
        "obv": {"score": 0.4},          # trend-confirm bullish
    }
    trending = eng.compute_momentum(indicators, regime_autocorr=0.30)
    reverting = eng.compute_momentum(indicators, regime_autocorr=-0.30)
    # In trending we add (-mean_revert) + trend_confirm — both contribute positively.
    assert trending > 0
    # In reverting we keep mean_revert (negative) and dampen trend_confirm.
    assert reverting < trending


def test_compute_momentum_neutral_dampens_both_groups():
    eng = _engine(mw=-0.02)
    indicators = {
        "rsi": {"score": -0.6},
        "stochastic": {"score": 0.0},
        "vwap": {"score": 0.0},
        "macd": {"score": 0.5},
        "obv": {"score": 0.0},
    }
    trending = abs(eng.compute_momentum(indicators, regime_autocorr=0.30))
    neutral = abs(eng.compute_momentum(indicators, regime_autocorr=0.0))
    # Neutral applies 0.5× damp to both groups, so magnitude is smaller than
    # the trending case where groups are coherently summed without damp.
    assert neutral < trending


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
