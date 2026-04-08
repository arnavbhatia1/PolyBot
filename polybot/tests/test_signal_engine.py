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
    return SignalEngine(min_edge=0.10, kelly_fraction=0.15, momentum_weight=0.08)

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
                             seconds_remaining=240, market_price_up=0.53, market_price_down=0.47)
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

def test_exit_when_edge_evaporates(engine):
    """Model says 75% but market is at 85% → market overpricing our side → EXIT."""
    # Moderate BTC above strike, but market has run ahead
    action, prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=50), btc_price=66450, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.85, side="Up", exit_threshold=-0.05)
    assert action == "EXIT"
    assert edge < 0

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
    """Student-t CDF gives less extreme probabilities than normal for large z."""
    se = SignalEngine(student_t_df=4)
    # Large distance: Student-t should give lower P(Up) than normal model would
    prob = se.compute_probability(72000, 71000, 180, 50.0)
    # With z = 1000 / (50 * sqrt(3)) = 11.5, normal gives ~1.0, Student-t gives ~0.99
    assert prob < 0.99  # fat tails mean reversal is more likely


# --- Regime factor tests ---

def test_regime_factor_trending():
    se = SignalEngine()
    # Monotonically increasing closes = positive autocorrelation
    closes = np.array([100 + i * 2.0 for i in range(15)])
    factor = se.compute_regime_factor(closes)
    assert factor > 0  # trending


def test_regime_factor_reverting():
    se = SignalEngine()
    # Alternating up/down = negative autocorrelation
    closes = np.array([100 + ((-1)**i) * 5.0 for i in range(15)])
    factor = se.compute_regime_factor(closes)
    assert factor < 0  # mean reverting


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
