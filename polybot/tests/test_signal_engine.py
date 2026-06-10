import math
import pytest
import numpy as np
from polybot.core.signal_engine import SignalEngine

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
    """Less time = more certain = higher probability.

    Distance/ATR chosen so both probabilities stay below the final logit clamp;
    saturation would tie p1 == p2 and break the strict comparison.
    """
    p1 = engine.compute_probability(66430, 66400, 240, 30)
    p2 = engine.compute_probability(66430, 66400, 60, 30)
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
    # No closes ⇒ regime=0 ⇒ L4 weight dampened to 0.5×0.08; the resulting logit
    # nudge moves P(Up) only a few points off 0.5, under the 0.10 min_edge.
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

def test_exit_in_profitable_scalp_window(engine):
    """Edge in (-0.05, -0.10) is the empirically profitable scalp zone → EXIT.

    Model ~60% (BTC barely above strike) but market priced at 67% → edge ~-0.07,
    which sits inside the scalp-correct zone (-0.10 < edge <= effective threshold).
    """
    action, _prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=80), btc_price=66420, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.67, side="Up", exit_threshold=-0.05)
    assert action == "EXIT"
    assert -0.10 < edge < 0.0


def test_deep_loss_holds_to_resolution_when_model_still_gives_chance(engine):
    """Edge < -0.10 AND model_prob still ≥ 0.05 → HOLD (binary residual is real)."""
    # Modest distance + high ATR keeps model_prob well above the dead-side floor.
    # entry_price above the current market → we're genuinely underwater, which is
    # the deep-loss-hold precondition (market < entry).
    action, _prob, edge, reason = engine.evaluate_hold(
        _make_indicators(atr_value=80), btc_price=66360, strike_price=66400,
        seconds_remaining=120, market_price_for_side=0.60, side="Up",
        exit_threshold=-0.05, entry_price=0.70)
    assert action == "HOLD"
    assert edge < -0.10
    assert "deeply underwater" in reason


def test_deep_loss_exits_when_model_says_side_is_dead():
    """Edge < -0.10 AND calibrated prob ≤ calibrator's lowest learned knot → EXIT.

    With a fitted isotonic calibrator, a calibrated probability at or below the
    lowest learned knot is a credible "the side really will pay zero" signal;
    selling at market beats holding for ~$0 expected — the override that keeps
    the deep-loss-hold rule from trapping a dead side to expiry. Uses a stub
    calibrator so the test is independent of fit-data shape.
    """
    class _StubCal:
        is_identity = False
        lowest_learned_prob = 0.05
        def calibrate(self, p):
            # Isotonic clips below-floor inputs to the lowest learned y_threshold.
            return max(0.05, p)

    se = SignalEngine(min_edge=0.10, kelly_fraction=0.15, momentum_weight=0.08,
                     calibrator=_StubCal())
    action, _prob, _edge, _ = se.evaluate_hold(
        _make_indicators(atr_value=30), btc_price=66200, strike_price=66400,
        seconds_remaining=120, market_price_for_side=0.30, side="Up", exit_threshold=-0.05)
    assert action == "EXIT"

def test_evaluate_hold_stamps_effective_exit_threshold(engine):
    """evaluate_hold must stamp last_effective_exit_threshold (the blended
    deep-loss-floor/ExitBoundary value) — main.py's phantom-bid SELL re-verify
    reads it instead of the raw config threshold. It must be populated and vary
    with market price (deep-ITM blends toward the more patient floor)."""
    atm = engine.evaluate_hold(
        _make_indicators(atr_value=80), btc_price=66420, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.55, side="Up", exit_threshold=-0.05)
    atm_thr = engine.last_effective_exit_threshold
    assert math.isfinite(atm_thr) and -0.30 <= atm_thr <= 0.30
    engine.evaluate_hold(
        _make_indicators(atr_value=80), btc_price=66600, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.90, side="Up",
        exit_threshold=-0.05, entry_price=0.92)
    deep_itm_thr = engine.last_effective_exit_threshold
    # Deep-ITM blends toward the patient deep_loss_floor → different from ATM.
    assert deep_itm_thr != atm_thr


def test_deep_underwater_holds_unless_loss_cut(engine):
    """Deep-negative edge with time remaining → HOLD (loss-cut fires only near expiry).

    Empirically, scalping at edge < -0.10 is correct only ~38% of the time (n>1000),
    so once the trade has moved this far against us we're better holding for the
    binary residual than locking in the loss.
    """
    # Bought at 0.98, now marked 0.95 → underwater (market < entry), so the
    # deep-loss-hold rule keeps the binary residual rather than locking the loss.
    action, _prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=80), btc_price=66410, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.95, side="Up",
        exit_threshold=-0.05, entry_price=0.98)
    assert action == "HOLD"
    assert edge < -0.20

def test_orderbook_spike_exits_for_profit(engine):
    """Bought Down at 0.40; BTC is now far above strike (Down clearly losing).
    A random order-book spike prices Down at 0.70 — profitable vs our 0.40 entry.
    Deep-loss-hold must NOT block this: market_price (0.70) > entry_price (0.40).
    Bot should EXIT and take the profit, not ride to $0."""
    # BTC 300 above strike with 5s left → model P(Down) ≈ 0 → holding_edge << -0.10
    action, _prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=20), btc_price=66700, strike_price=66400,
        seconds_remaining=5, market_price_for_side=0.70, side="Down",
        exit_threshold=-0.05, entry_price=0.40)
    assert edge < -0.10
    assert action == "EXIT"

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
    # Inputs sized so the L1 logit stays well below the final clamp — otherwise
    # both probs saturate at the same ceiling and the flow contribution is lost.
    se = SignalEngine(flow_weight=0.06)
    prob_neutral = se.compute_probability(71050, 71000, 180, 80.0, flow_signal=0.0)
    prob_bullish = se.compute_probability(71050, 71000, 180, 80.0, flow_signal=1.0)
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
    """Smoke: evaluate_hold accepts entry_price/fee_rate and returns a valid action."""
    se = SignalEngine()
    indicators = {"atr": {"atr": 50.0}, "rsi": {"score": 0}, "macd": {"score": 0},
                  "stochastic": {"score": 0}, "obv": {"score": 0}, "vwap": {"score": 0}}
    action1, _, _, _ = se.evaluate_hold(indicators, 71100, 71000, 180, 0.60, "Up",
                                        exit_threshold=-0.10, entry_price=0.0)
    action2, _, _, _ = se.evaluate_hold(indicators, 71100, 71000, 180, 0.60, "Up",
                                        exit_threshold=-0.10, entry_price=0.50, fee_rate=0.072)
    assert action1 in ("HOLD", "EXIT")
    assert action2 in ("HOLD", "EXIT")


# --- Logit-composition + math invariants ---

def test_regime_direction_from_returns_not_prob():
    """Trending DOWN + above strike → prob should DECREASE."""
    se = SignalEngine(regime_weight=0.05)
    # BTC above strike but trending down (closes decreasing, need lookback+2 closes)
    closes = np.array([67000 - i * 2.0 for i in range(55)])  # trending down, stays positive
    prob_no_regime = se.compute_probability(66450, 66400, 180, 30.0)
    prob_with_regime = se.compute_probability(66450, 66400, 180, 30.0, closes=closes)
    # Down-trending regime should push prob_up LOWER, not higher
    assert prob_with_regime < prob_no_regime


def test_logit_dampening_near_extremes():
    """Same flow_signal produces smaller prob shift at p~0.95 vs p~0.50."""
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
    """At strike with no indicators, prob~0.50, edge~0 → SKIP."""
    se = SignalEngine(min_edge=0.03, min_kelly=0.015)
    signal = se.evaluate(_make_indicators(atr_value=50), has_position=False, in_entry_window=True,
                         btc_price=66400, strike_price=66400,
                         seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"


def test_kelly_gate_accepts_underdog_with_edge():
    """Decent edge on underdog → Kelly sufficient → ENTER."""
    se = SignalEngine(min_edge=0.03, min_kelly=0.015)
    signal = se.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                         btc_price=66200, strike_price=66400,
                         seconds_remaining=120, market_price_up=0.40, market_price_down=0.60)
    assert signal.action == "BUY_NO"
    assert signal.kelly_size >= 0.015


def test_atr_scaling_increases_z():
    """With atr_sigma_ratio=1.7, probability is further from 0.5."""
    se_old = SignalEngine(atr_sigma_ratio=1.0)
    se_new = SignalEngine(atr_sigma_ratio=1.7)
    prob_old = se_old.compute_probability(66500, 66400, 180, 50.0)
    prob_new = se_new.compute_probability(66500, 66400, 180, 50.0)
    assert abs(prob_new - 0.5) > abs(prob_old - 0.5)


def test_student_t_scale_normalization():
    """t.cdf(z*scale, df=4) vs t.cdf(z, df=4) for known z."""
    from scipy.stats import t as student_t_dist
    z = 1.5
    t_scale = math.sqrt(4 / (4 - 2))
    prob_unscaled = float(student_t_dist.cdf(z, df=4))
    prob_scaled = float(student_t_dist.cdf(z * t_scale, df=4))
    assert prob_scaled > prob_unscaled


def test_skip_signal_carries_the_side_its_prob_refers_to():
    """When the edge-best side is the long-shot (model strongly Down, but only
    the overpriced-Down/underpriced-Up race leaves Up with the better edge),
    the SKIP signal's prob is the Up side's sub-50% value — and signal.side must
    say so. A prob>=0.5 display heuristic would label it as a high-prob Up call
    and read like a sign-inverted model."""
    class _ClampCal:  # the production curve's [0.15, 0.85] output clamp
        def calibrate(self, p):
            return min(0.85, max(0.15, p))

    se = SignalEngine(min_edge=0.04, kelly_fraction=0.15, momentum_weight=0.0,
                      min_model_probability=0.56, calibrator=_ClampCal())
    # BTC far below strike → calibrated P(down)=0.85. Down overpriced (0.95) →
    # its edge is negative; Up underpriced (0.05) → the small positive edge wins
    # the edge race carrying the 0.15 long-shot prob.
    sig = se.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                      btc_price=66200, strike_price=66400, seconds_remaining=120,
                      market_price_up=0.05, market_price_down=0.95)
    assert sig.action == "SKIP"
    assert sig.prob < 0.5, "edge-best side here must be the long-shot Up"
    assert sig.side == "Up", "signal must label the side its prob refers to"

    # Symmetric sanity: fair pricing → edge-best side is the model's side.
    sig2 = se.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                       btc_price=66200, strike_price=66400, seconds_remaining=120,
                       market_price_up=0.5, market_price_down=0.5)
    assert sig2.side == "Down"
    assert sig2.prob > 0.5
