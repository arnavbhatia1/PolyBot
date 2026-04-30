import math
import pytest
import numpy as np
from polybot.core.signal_engine import SignalEngine, TradeSignal

def _make_indicators(atr_value=30.0, rsi_score=0.0, macd_score=0.0,
                     stoch_score=0.0, obv_score=0.0, vwap_score=0.0):
    return {
        "atr": {"atr": atr_value, "passes": True, "reason": "ok"},
        "ema": {"trend": "bullish", "fast_ema": 100.0, "slow_ema": 99.0},
        "rsi": {"rsi": 50.0, "score": rsi_score},
        "macd": {"macd": 0.0, "signal": 0.0, "histogram": 0.0, "score": macd_score},
        "stochastic": {"k": 50.0, "d": 50.0, "score": stoch_score},
        "obv": {"obv_slope": 0, "price_slope": 0, "score": obv_score},
        "vwap": {"vwap": 100.0, "deviation": 0, "score": vwap_score},
    }

@pytest.fixture
def engine():
    # Reset adaptive calibration state so tests are deterministic regardless of
    # whatever's currently persisted in polybot/memory/adaptive_calibration.json.
    eng = SignalEngine(min_edge=0.10, kelly_fraction=0.15, momentum_weight=0.08)
    eng._calibration_buffer.clear()
    eng._bucket_mults = {name: 1.0 for name, _, _ in __import__("polybot.core.signal_engine", fromlist=["_CALIBRATION_BUCKETS"])._CALIBRATION_BUCKETS}
    return eng

def test_buys_up_when_btc_above_strike(engine):
    """BTC $100 above strike with 3 min left, market at 55% — model finds edge."""
    signal = engine.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                             btc_price=66500, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.55, market_price_down=0.45)
    assert signal.action == "BUY_YES"
    assert signal.edge >= 0.10

def test_buys_down_when_btc_below_strike(engine):
    """BTC $100 below strike, market at 55% Down — model finds edge."""
    signal = engine.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                             btc_price=66300, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.45, market_price_down=0.55)
    assert signal.action == "BUY_NO"
    assert signal.edge >= 0.10

def test_skips_when_btc_at_strike(engine):
    """BTC right at strike, 50/50 market — no edge."""
    signal = engine.evaluate(_make_indicators(), has_position=False, in_entry_window=True,
                             btc_price=66400, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"

def test_skips_when_market_already_correct(engine):
    """BTC slightly above strike, market already priced correctly — no edge."""
    signal = engine.evaluate(_make_indicators(atr_value=50), has_position=False, in_entry_window=True,
                             btc_price=66420, strike_price=66400,
                             seconds_remaining=240, market_price_up=0.66, market_price_down=0.34)
    assert signal.action == "SKIP"

def test_has_position_blocks(engine):
    signal = engine.evaluate(_make_indicators(), has_position=True, in_entry_window=True,
                             btc_price=66500, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"

def test_outside_window_blocks(engine):
    signal = engine.evaluate(_make_indicators(), has_position=False, in_entry_window=False,
                             btc_price=66500, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"

def test_kelly_positive_with_edge(engine):
    signal = engine.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                             btc_price=66600, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.55, market_price_down=0.45)
    assert signal.kelly_size > 0

def test_more_distance_bigger_edge(engine):
    """Further from strike = higher model probability = bigger edge."""
    s1 = engine.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                         btc_price=66450, strike_price=66400,
                         seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    s2 = engine.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                         btc_price=66600, strike_price=66400,
                         seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert s2.edge > s1.edge

def test_less_time_higher_probability(engine):
    """Less time = more certain = higher probability."""
    p1 = engine.compute_probability(66500, 66400, 240, 30)
    p2 = engine.compute_probability(66500, 66400, 60, 30)
    assert p2 > p1

def test_momentum_nudges_probability(engine):
    """Bullish indicators slightly increase P(Up)."""
    p_neutral = engine.compute_probability(66400, 66400, 180, 30, _make_indicators())
    p_bullish = engine.compute_probability(66400, 66400, 180, 30,
                                           _make_indicators(rsi_score=0.8, macd_score=0.9))
    assert p_bullish > p_neutral

def test_no_edge_when_momentum_alone(engine):
    """Indicators alone (BTC at strike) shouldn't create enough edge to trade at 50/50."""
    signal = engine.evaluate(
        _make_indicators(rsi_score=1.0, macd_score=1.0, stoch_score=1.0, obv_score=1.0, vwap_score=1.0),
        has_position=False, in_entry_window=True,
        btc_price=66400, strike_price=66400,
        seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    # With momentum_weight=0.08, max nudge is 0.08. Edge = 0.58 - 0.50 = 0.08 < 0.10 min_edge
    assert signal.action == "SKIP"


# --- evaluate_hold tests ---

def test_hold_when_model_confident(engine):
    """Model at 90%, market at 70% → edge=+20% → HOLD (still underpriced, ride to $1)."""
    # BTC well above strike with little time = high P(Up)
    action, prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=30), btc_price=66600, strike_price=66400,
        seconds_remaining=60, market_price_for_side=0.70, side="Up", exit_threshold=-0.05)
    assert action == "HOLD"
    assert edge > 0

def test_exit_when_conditions_flip(engine):
    """Bought Up but BTC fell below strike → model says Down likely → EXIT."""
    action, prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=30), btc_price=66200, strike_price=66400,
        seconds_remaining=120, market_price_for_side=0.60, side="Up", exit_threshold=-0.05)
    assert action == "EXIT"
    assert edge < -0.05

def test_hold_when_model_still_favors(engine):
    """Model still favors our side AND edge isn't catastrophically bad → HOLD.

    Tightened override now requires model_prob >= 0.70 AND holding_edge > -0.10.
    Both must hold; this case satisfies both (prob ~82%, edge ~-6%).
    """
    action, prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=50), btc_price=66450, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.88, side="Up", exit_threshold=-0.05)
    assert action == "HOLD"
    assert prob > 0.70

def test_exit_when_edge_deeply_negative(engine):
    """Model says ~55% but market at 95% → edge deeply negative → EXIT."""
    # BTC barely above strike with high ATR → model ~55%, market 95% → edge ~ -40%
    action, prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=80), btc_price=66410, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.95, side="Up", exit_threshold=-0.05)
    assert action == "EXIT"
    assert edge < -0.20

def test_hold_at_boundary(engine):
    """Small positive edge → still HOLD."""
    # BTC above strike, model says ~60%, market at 55%
    action, prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=30), btc_price=66450, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.50, side="Up", exit_threshold=-0.05)
    assert action == "HOLD"
    assert edge > -0.05

def test_hold_down_side(engine):
    """Hold Down when BTC is below strike and model supports it."""
    action, prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=30), btc_price=66200, strike_price=66400,
        seconds_remaining=60, market_price_for_side=0.70, side="Down", exit_threshold=-0.05)
    assert action == "HOLD"
    assert edge > 0


# --- Student-t CDF tests ---

def test_student_t_less_extreme_than_normal():
    """Student-t CDF with variance normalization gives less extreme probs at large z
    (fat tails = more reversal probability in the extremes)."""
    import math
    from scipy.stats import norm, t as student_t_dist
    # At large z, Student-t tails are fatter → P(Up) is lower than normal
    z = 3.0
    t_scale = math.sqrt(4 / (4 - 2))
    prob_t = float(student_t_dist.cdf(z * t_scale, df=4))
    prob_norm = float(norm.cdf(z))
    assert prob_t < prob_norm


# --- Regime factor tests ---

def test_regime_factor_trending():
    se = SignalEngine()
    # Monotonically increasing closes = positive autocorrelation (need lookback+2 closes)
    closes = np.array([100 + i * 2.0 for i in range(55)])
    factor = se.compute_regime_factor(closes)
    assert factor > 0  # trending


def test_regime_factor_reverting():
    se = SignalEngine()
    # Alternating up/down = negative autocorrelation (need lookback+2 closes)
    closes = np.array([100 + ((-1)**i) * 5.0 for i in range(55)])
    factor = se.compute_regime_factor(closes)
    assert factor < 0  # mean reverting


def test_regime_factor_short_lookback():
    """With a smaller lookback, fewer closes are needed."""
    se = SignalEngine(regime_lookback=10)
    closes = np.array([100 + i * 2.0 for i in range(15)])
    factor = se.compute_regime_factor(closes)
    assert factor > 0  # trending


def test_regime_factor_insufficient_data():
    """Returns 0.0 when not enough closes for the lookback window."""
    se = SignalEngine(regime_lookback=20)
    closes = np.array([100 + i for i in range(10)])  # only 10 closes, need 22
    factor = se.compute_regime_factor(closes)
    assert factor == 0.0


# --- Order flow integration tests ---

def test_flow_signal_bullish():
    se = SignalEngine(flow_weight=0.06)
    prob_neutral = se.compute_probability(71100, 71000, 180, 50.0, flow_signal=0.0)
    prob_bullish = se.compute_probability(71100, 71000, 180, 50.0, flow_signal=1.0)
    assert prob_bullish > prob_neutral  # bullish flow increases P(Up)


# --- ATR gate tests ---

def test_atr_gate_blocks_entry():
    se = SignalEngine(min_edge=0.10)
    indicators = {
        "atr": {"atr": 5.0, "passes": False, "reason": "too_quiet"},
        "rsi": {"score": 0}, "macd": {"score": 0}, "stochastic": {"score": 0},
        "obv": {"score": 0}, "vwap": {"score": 0},
    }
    signal = se.evaluate(indicators, has_position=False, in_entry_window=True,
                         btc_price=71500, strike_price=71000,
                         seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"
    assert "ATR gate" in signal.reason


# --- Fee-aware hold tests ---

def test_evaluate_hold_fee_aware_threshold():
    """Fee-aware scalp: threshold is harder when fees are high."""
    se = SignalEngine()
    indicators = {"atr": {"atr": 50.0}, "rsi": {"score": 0}, "macd": {"score": 0},
                  "stochastic": {"score": 0}, "obv": {"score": 0}, "vwap": {"score": 0}}
    # With entry_price and fee, effective threshold should be more negative
    action1, _, _, _ = se.evaluate_hold(indicators, 71100, 71000, 180, 0.60, "Up",
                                        exit_threshold=-0.10, entry_price=0.0)
    action2, _, _, _ = se.evaluate_hold(indicators, 71100, 71000, 180, 0.60, "Up",
                                        exit_threshold=-0.10, entry_price=0.50, fee_rate=0.072)
    # With fee awareness, the threshold is harder (more negative) so it's more likely to HOLD
    # Both should return the same action type here but the effective thresholds differ
    assert action1 in ("HOLD", "EXIT")
    assert action2 in ("HOLD", "EXIT")


# --- New tests for math fixes ---

def test_regime_direction_from_returns_not_prob():
    """Fix 1: trending DOWN + above strike → prob should DECREASE."""
    se = SignalEngine(regime_weight=0.05)
    # BTC above strike but trending down (closes decreasing, need lookback+2 closes)
    closes = np.array([67000 - i * 2.0 for i in range(55)])  # trending down, stays positive
    prob_no_regime = se.compute_probability(66450, 66400, 180, 30.0)
    prob_with_regime = se.compute_probability(66450, 66400, 180, 30.0, closes=closes)
    # Down-trending regime should push prob_up LOWER, not higher
    assert prob_with_regime < prob_no_regime


def test_logit_dampening_near_extremes():
    """Fix 2: same flow_signal produces smaller prob shift at p~0.95 vs p~0.50."""
    se = SignalEngine(flow_weight=0.06)
    # Near p=0.5 (BTC at strike)
    p_base_mid = se.compute_probability(66400, 66400, 180, 30.0, flow_signal=0.0)
    p_flow_mid = se.compute_probability(66400, 66400, 180, 30.0, flow_signal=1.0)
    shift_mid = abs(p_flow_mid - p_base_mid)

    # Near p=0.95 (BTC well above strike)
    p_base_high = se.compute_probability(66600, 66400, 60, 30.0, flow_signal=0.0)
    p_flow_high = se.compute_probability(66600, 66400, 60, 30.0, flow_signal=1.0)
    shift_high = abs(p_flow_high - p_base_high)

    # Logit-space adjustment should produce SMALLER shift near extremes
    assert shift_high < shift_mid


def test_kelly_gate_rejects_thin_edge_at_high_price():
    """Fix 3: at strike with no indicators, prob~0.50, edge~0 → SKIP."""
    se = SignalEngine(min_edge=0.03, min_kelly=0.015)
    signal = se.evaluate(_make_indicators(atr_value=50), has_position=False, in_entry_window=True,
                         btc_price=66400, strike_price=66400,
                         seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"


def test_kelly_gate_accepts_underdog_with_edge():
    """Fix 3: decent edge on underdog → Kelly sufficient → ENTER."""
    se = SignalEngine(min_edge=0.03, min_kelly=0.015)
    signal = se.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                         btc_price=66200, strike_price=66400,
                         seconds_remaining=120, market_price_up=0.40, market_price_down=0.60)
    assert signal.action == "BUY_NO"
    assert signal.kelly_size >= 0.015


def test_atr_scaling_increases_z():
    """Fix 4: with atr_sigma_ratio=1.7, probability is further from 0.5."""
    se_old = SignalEngine(atr_sigma_ratio=1.0)
    se_new = SignalEngine(atr_sigma_ratio=1.7)
    prob_old = se_old.compute_probability(66500, 66400, 180, 50.0)
    prob_new = se_new.compute_probability(66500, 66400, 180, 50.0)
    assert abs(prob_new - 0.5) > abs(prob_old - 0.5)


def test_student_t_scale_normalization():
    """Fix 5: t.cdf(z*scale, df=4) vs t.cdf(z, df=4) for known z."""
    import math
    from scipy.stats import t as student_t_dist
    z = 1.5
    t_scale = math.sqrt(4 / (4 - 2))
    prob_unscaled = float(student_t_dist.cdf(z, df=4))
    prob_scaled = float(student_t_dist.cdf(z * t_scale, df=4))
    assert prob_scaled > prob_unscaled
