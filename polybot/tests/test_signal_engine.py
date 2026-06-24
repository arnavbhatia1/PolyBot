import math
import pytest
import numpy as np
from polybot.core.signal_engine import SignalEngine

def _make_indicators(atr_value=30.0):
    return {
        "atr": {"atr": atr_value, "passes": True, "reason": "ok"},
    }

@pytest.fixture
def engine():
    return SignalEngine(min_edge=0.10, kelly_fraction=0.15)

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
    p1 = engine.compute_probability(66430, 66400, 240, 30)
    p2 = engine.compute_probability(66430, 66400, 60, 30)
    assert p2 > p1


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

    Model ~59% (atr=120, BTC $30 above strike) but market priced at 67% → edge
    ~-0.08, which sits inside the scalp-correct zone (-0.10 < edge <= effective
    threshold).
    """
    action, _prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=120), btc_price=66430, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.67, side="Up", exit_threshold=-0.05)
    assert action == "EXIT"
    assert -0.10 < edge < 0.0


def test_deep_loss_holds_to_resolution_when_underwater(engine):
    """Edge < deep_loss_hold_threshold AND market < entry → HOLD (binary residual is real)."""
    # entry_price above the current market → we're genuinely underwater, which is
    # the deep-loss-hold precondition (market < entry).
    action, _prob, edge, reason = engine.evaluate_hold(
        _make_indicators(atr_value=80), btc_price=66360, strike_price=66400,
        seconds_remaining=120, market_price_for_side=0.60, side="Up",
        exit_threshold=-0.05, entry_price=0.70)
    assert action == "HOLD"
    assert edge < -0.10
    assert "deeply underwater" in reason


def test_deep_loss_holds_even_when_model_says_side_is_dead():
    """The deep-loss-hold branch is unconditional: even with the model giving the
    side essentially zero chance, market < entry AND edge < deep_loss_hold_threshold
    → HOLD the binary residual (no calibrator dead-side override exists)."""
    se = SignalEngine(min_edge=0.10, kelly_fraction=0.15)
    # BTC $400 below strike with 2 min left → model P(Up) ≈ 0; entered at 0.70,
    # marked 0.30 → deeply underwater.
    action, prob, edge, reason = se.evaluate_hold(
        _make_indicators(atr_value=30), btc_price=66000, strike_price=66400,
        seconds_remaining=120, market_price_for_side=0.30, side="Up",
        exit_threshold=-0.05, entry_price=0.70)
    assert prob < 0.05
    assert edge < -0.10
    assert action == "HOLD"
    assert "deeply underwater" in reason

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


# --- Loss-cut positive-edge guard (2026-06-17 fix) ---

def test_loss_cut_fires_when_model_agrees_residual_is_cheap(engine):
    """Deep underwater, <90s, BTC wrong-side by >0.5xATR, AND the model agrees the
    bid is NOT underpricing the residual (holding_edge <= 0) → loss-cut fires."""
    # Up side, BTC $200 below strike (wrong side, >>0.5xATR) → model P(Up) ≈ 0;
    # a thin bid at 0.10 sits at/above that residual, so holding_edge <= 0.
    action, _prob, edge, reason = engine.evaluate_hold(
        _make_indicators(atr_value=30), btc_price=66200, strike_price=66400,
        seconds_remaining=60, market_price_for_side=0.10, side="Up",
        exit_threshold=-0.10, entry_price=0.80)
    assert edge <= 0                       # model values the residual at/below the bid
    assert action == "EXIT"
    assert engine.last_loss_cut_event == "fired"
    assert "cutting loss" in reason


def test_loss_cut_skipped_when_model_values_residual_above_bid(engine):
    """The 06-17 fix: when the model still values the binary residual ABOVE the
    panic bid (holding_edge > 0), a deep-underwater near-expiry position is NOT
    loss-cut — it holds the residual rather than locking the loss into a thin bid.
    (Pre-fix this fired and sold ~0.05-0.18/share of model EV away on ~1/5 cuts.)"""
    # Up side, BTC only $20 below strike (wrong side, dist 20 > 0.5xATR=15) so the
    # model still gives the residual real value, while a panic bid prices it 0.05.
    action, _prob, edge, _reason = engine.evaluate_hold(
        _make_indicators(atr_value=30), btc_price=66380, strike_price=66400,
        seconds_remaining=60, market_price_for_side=0.05, side="Up",
        exit_threshold=-0.10, entry_price=0.80)
    assert edge > 0                        # model values the residual above the bid
    assert action == "HOLD"                # not loss-cut, not scalped
    assert engine.last_loss_cut_event != "fired"


# --- ATR per-candle dedup (2026-06-17 fix) ---

def test_record_atr_one_slot_per_candle():
    """Intra-candle compute_probability calls (same candle_ts, ~1Hz exit ticks)
    occupy ONE rolling-ATR slot tracking the latest value; distinct candle_ts
    append; candle_ts=None keeps legacy append-every-call behavior."""
    se = SignalEngine()
    se._record_atr(30.0, candle_ts=1000)
    se._record_atr(34.0, candle_ts=1000)
    se._record_atr(36.0, candle_ts=1000)
    assert len(se._atr_history) == 1
    assert se._atr_history[-1] == 36.0
    assert se._atr_history_sum == 36.0
    assert se.last_atr_rolling_20 == 36.0
    # New candle → new slot; running mean stays exact.
    se._record_atr(20.0, candle_ts=2000)
    assert list(se._atr_history) == [36.0, 20.0]
    assert se._atr_history_sum == 56.0
    assert se.last_atr_rolling_20 == 28.0
    # No candle_ts → always append (back-compat for direct/unit-test calls).
    se._record_atr(10.0)
    se._record_atr(10.0)
    assert len(se._atr_history) == 4


def test_record_atr_long_term_deque_stays_in_lockstep():
    """Short and long-term ATR deques both keep one slot per candle with exact
    running sums (no drift from the replace path)."""
    se = SignalEngine()
    for v in (30.0, 31.0, 33.0):           # same candle, three forming updates
        se._record_atr(v, candle_ts=500)
    se._record_atr(40.0, candle_ts=560)    # next candle
    assert list(se._atr_long_term) == [33.0, 40.0]
    assert se._atr_long_term_sum == 73.0
    assert se.last_atr_long_term_mean == 36.5


def test_evaluate_hold_records_atr_once_per_candle(engine):
    """Many evaluate_hold ticks within one candle (same atr candle_ts) record the
    ATR once, not once per tick — the exit-tick pollution bug fixed end-to-end."""
    ind = {"atr": {"atr": 30.0, "passes": True, "reason": "ok", "candle_ts": 5000}}
    for _ in range(12):
        engine.evaluate_hold(ind, 66450, 66400, 120, 0.55, "Up", exit_threshold=-0.10)
    assert len(engine._atr_history) == 1
    ind2 = {"atr": {"atr": 28.0, "passes": True, "reason": "ok", "candle_ts": 5060}}
    engine.evaluate_hold(ind2, 66450, 66400, 60, 0.55, "Up", exit_threshold=-0.10)
    assert len(engine._atr_history) == 2


def test_record_atr_maxlen_eviction_keeps_invariants():
    """>205 distinct candles, each with several same-candle forming replaces,
    crossing BOTH the short (20) and long-term (200) deque eviction boundaries:
    the running sums stay exact and the two deques stay in lockstep at [-1]
    (the riskiest arithmetic path — front-eviction on one deque while the other
    keeps growing)."""
    import math as _m
    se = SignalEngine()
    for c in range(210):
        ts = 1000 + c
        for j in range(3):  # intra-candle forming updates → replace, not append
            se._record_atr(20.0 + (c % 7) + j * 0.5, candle_ts=ts)
        assert se._atr_history[-1] == se._atr_long_term[-1]
        assert abs(se._atr_history_sum - _m.fsum(se._atr_history)) < 1e-6
        assert abs(se._atr_long_term_sum - _m.fsum(se._atr_long_term)) < 1e-6
    assert len(se._atr_history) == 20      # short deque saturated + evicting
    assert len(se._atr_long_term) == 200   # long deque saturated + evicting


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


# --- ATR gate tests ---

def test_atr_gate_blocks_entry():
    se = SignalEngine(min_edge=0.10)
    indicators = {
        "atr": {"atr": 5.0, "passes": False, "reason": "too_quiet"},
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
    indicators = {"atr": {"atr": 50.0}}
    action1, _, _, _ = se.evaluate_hold(indicators, 71100, 71000, 180, 0.60, "Up",
                                        exit_threshold=-0.10, entry_price=0.0)
    action2, _, _, _ = se.evaluate_hold(indicators, 71100, 71000, 180, 0.60, "Up",
                                        exit_threshold=-0.10, entry_price=0.50, fee_rate=0.072)
    assert action1 in ("HOLD", "EXIT")
    assert action2 in ("HOLD", "EXIT")


# --- Math invariants ---

def test_kelly_gate_rejects_thin_edge_at_high_price():
    """At strike, prob~0.50, edge~0 → SKIP."""
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
    se = SignalEngine(min_edge=0.04, kelly_fraction=0.15, min_model_probability=0.56)
    # High ATR + BTC $200 below strike keeps P(Up) ≈ 0.29 — a long shot but far from
    # zero. Down overpriced (0.95) → its edge is negative; Up underpriced (0.05) → the
    # positive edge wins the edge race carrying the sub-50% long-shot prob.
    sig = se.evaluate(_make_indicators(atr_value=400), has_position=False, in_entry_window=True,
                      btc_price=66200, strike_price=66400, seconds_remaining=120,
                      market_price_up=0.05, market_price_down=0.95)
    assert sig.action == "SKIP"
    assert sig.prob < 0.5, "edge-best side here must be the long-shot Up"
    assert sig.side == "Up", "signal must label the side its prob refers to"

    # Symmetric sanity: fair pricing → edge-best side is the model's side.
    sig2 = se.evaluate(_make_indicators(atr_value=400), has_position=False, in_entry_window=True,
                       btc_price=66200, strike_price=66400, seconds_remaining=120,
                       market_price_up=0.5, market_price_down=0.5)
    assert sig2.side == "Down"
    assert sig2.prob > 0.5
