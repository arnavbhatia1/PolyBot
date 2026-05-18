from __future__ import annotations

import math
import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING
import numpy as np
from scipy.special import stdtr as _stdtr
from polybot.core.exit_boundary import ExitBoundary
from polybot.core.returns import lag1_autocorr
from polybot.config.param_registry import default_for as _d

if TYPE_CHECKING:
    from polybot.core.calibrator import PlattCalibrator

# Regime-conditional L4: |autocorr| > threshold = real regime signal.
_REGIME_MOMENTUM_THRESHOLD = 0.15
_REGIME_MOMENTUM_AMPLIFY = 1.5
_REGIME_MOMENTUM_DAMPEN = 0.5
_MOMENTUM_WEIGHT_CLAMP = 0.10

# Dynamic ATR floor: max(static, FRACTION × rolling_mean). When the rolling-20
# ATR collapses well below the long-term mean (regime shift to low vol), widen
# the floor proportionally so L1 doesn't produce overconfident probabilities.
_ATR_HISTORY_SIZE = 20
_ATR_FLOOR_FRACTION = 0.30
_ATR_HISTORY_MIN_SAMPLES = 5
_ATR_LONG_TERM_SIZE = 200
_ATR_LONG_TERM_MIN_SAMPLES = 50
_ATR_REGIME_SHIFT_THRESHOLD = 0.60

# When holding_edge < this and time remains, hold to resolution rather than
# scalp; the binary residual is +EV vs locking in the loss. Loss-cut still
# fires near expiry when market collapses below entry × loss_cut_fraction.
_DEEP_LOSS_HOLD_THRESHOLD = -0.10
# L1 prob clip — tight enough that the final ±4 logit clamp (not this clip) is
# the precision floor. Old 1e-3 clip collapsed deep-ITM precision before any
# other layer ran; 1e-6 maps to logit ±13.8, well past the final clamp.
_L1_CLIP = 1e-6
# Minimum Student-t df. Pipeline range is 3-8; df ≤ 2 has undefined variance.
# Clamping at 3 removes the t_scale fallback discontinuity (was 1.0 at df ≤ 2,
# jumping to √3 = 1.73 at df = 3).
_MIN_STUDENT_T_DF = 3
# L5 (prev-window margin carry) overlaps with L2 (regime × direction of last
# 1-min return) early in a window. Dampen L5 by (1 - min(_L5_DAMP_CAP, |regime|))
# so the orthogonal-info portion still contributes when regime is weak.
_L5_REGIME_DAMP_CAP = 0.7

logger = logging.getLogger(__name__)

@dataclass
class TradeSignal:
    action: str          # "BUY_YES", "BUY_NO", "SKIP"
    prob: float          # Model probability for the chosen side (0-1)
    edge: float          # Model probability - market price
    kelly_size: float    # Optimal fraction of bankroll
    reason: str

def compute_signal_consensus(signals: dict[str, float], side: str, dead_zone: float = 0.05, consensus_config: dict | None = None) -> float:
    """Kelly multiplier from how many signals agree with the chosen side."""
    cc = consensus_config or {
        "very_high_pct": 0.80, "very_high_mult": 1.3,
        "high_pct": 0.60, "high_mult": 1.0,
        "medium_pct": 0.40, "medium_mult": 0.8,
        "low_mult": 0.6,
    }
    if not signals:
        return 1.0
    agree = 0
    total = 0
    for val in signals.values():
        if abs(val) < dead_zone:
            continue
        total += 1
        if side == "Up" and val > 0:
            agree += 1
        elif side == "Down" and val < 0:
            agree += 1
    if total == 0:
        return 1.0
    pct = agree / total
    if pct >= cc["very_high_pct"]:
        return cc["very_high_mult"]
    if pct >= cc["high_pct"]:
        return cc["high_mult"]
    if pct >= cc["medium_pct"]:
        return cc["medium_mult"]
    return cc["low_mult"]


class SignalEngine:
    """Computes P(Up) for a 5-min BTC Up/Down contract via:
    L1 Student-t CDF, L2 regime autocorr, L3 CLOB flow, L3b spot CVD,
    L3e Bybit OI liquidation, L4 indicator momentum, L5 prev-window carry,
    plus Platt calibration. Trades when |model - market| >= min_edge.
    Platt scaling (re-fit each pipeline cycle) is the sole overconfidence
    correction.
    """

    def __init__(self, min_edge: float | None = None, kelly_fraction: float | None = None,
                 momentum_weight: float | None = None, weights: dict[str, float] | None = None,
                 min_model_probability: float | None = None,
                 student_t_df: int | None = None, regime_weight: float | None = None,
                 flow_weight: float | None = None, regime_lookback: int | None = None,
                 min_kelly: float | None = None, atr_sigma_ratio: float | None = None,
                 calibrator: PlattCalibrator | None = None,
                 spot_flow_weight: float | None = None,
                 prev_margin_weight: float | None = None,
                 min_atr: float | None = None,
                 liquidation_weight: float | None = None,
                 logit_scale: float | None = None,
                 loss_cut_fraction: float | None = None,
                 loss_cut_time_s: float | None = None,
                 consensus_dead_zone: float | None = None,
                 consensus_config: dict | None = None) -> None:
        # Defaults resolve from param_registry — settings.yaml drives production via _build_signal_engine.
        if min_edge is None: min_edge = _d("min_edge")
        if kelly_fraction is None: kelly_fraction = _d("kelly_fraction")
        if momentum_weight is None: momentum_weight = _d("momentum_weight")
        if weights is None: weights = _d("weights")
        if min_model_probability is None: min_model_probability = _d("min_model_probability")
        if student_t_df is None: student_t_df = _d("student_t_df")
        if regime_weight is None: regime_weight = _d("regime_weight")
        if flow_weight is None: flow_weight = _d("flow_weight")
        if regime_lookback is None: regime_lookback = _d("regime_lookback")
        if min_kelly is None: min_kelly = _d("min_kelly")
        if atr_sigma_ratio is None: atr_sigma_ratio = _d("atr_sigma_ratio")
        if spot_flow_weight is None: spot_flow_weight = _d("spot_flow_weight")
        if prev_margin_weight is None: prev_margin_weight = _d("prev_margin_weight")
        if min_atr is None: min_atr = _d("min_atr")
        if liquidation_weight is None: liquidation_weight = _d("liquidation_weight")
        if logit_scale is None: logit_scale = _d("logit_scale")
        if loss_cut_fraction is None: loss_cut_fraction = _d("loss_cut_fraction")
        if loss_cut_time_s is None: loss_cut_time_s = _d("loss_cut_time_s")
        if consensus_dead_zone is None: consensus_dead_zone = _d("consensus_dead_zone")
        self.min_edge: float = min_edge
        self.kelly_fraction: float = kelly_fraction
        self.momentum_weight: float = momentum_weight
        self.min_model_probability: float = min_model_probability
        self.student_t_df: int = student_t_df
        self.regime_weight: float = regime_weight
        self.flow_weight: float = flow_weight
        self.regime_lookback: int = regime_lookback
        self.weights: dict[str, float] = weights
        self.min_kelly: float = min_kelly
        self.atr_sigma_ratio: float = atr_sigma_ratio
        self.calibrator = calibrator
        self.spot_flow_weight: float = spot_flow_weight
        self.prev_margin_weight: float = prev_margin_weight
        self.min_atr: float = min_atr
        self.liquidation_weight: float = liquidation_weight
        self.logit_scale: float = logit_scale
        self.loss_cut_fraction: float = loss_cut_fraction
        self.loss_cut_time_s: float = loss_cut_time_s
        self.consensus_dead_zone: float = consensus_dead_zone
        self.consensus_config: dict = consensus_config or {
            "very_high_pct": 0.80, "very_high_mult": 1.3,
            "high_pct": 0.60, "high_mult": 1.0,
            "medium_pct": 0.40, "medium_mult": 0.8,
            "low_mult": 0.6,
        }
        self._exit_boundary = ExitBoundary(df=self.student_t_df)
        self._atr_history: deque[float] = deque(maxlen=_ATR_HISTORY_SIZE)
        self._atr_long_term: deque[float] = deque(maxlen=_ATR_LONG_TERM_SIZE)
        self._atr_history_sum: float = 0.0
        self._atr_long_term_sum: float = 0.0
        self.last_regime_autocorr: float = 0.0
        self.last_regime_direction: float = 0.0
        self.last_raw_prob_up: float = 0.5

    def _record_atr(self, atr: float) -> None:
        if atr <= 0:
            return
        v = float(atr)
        h = self._atr_history
        if len(h) == h.maxlen:
            self._atr_history_sum -= h[0]
        h.append(v)
        self._atr_history_sum += v
        lt = self._atr_long_term
        if len(lt) == lt.maxlen:
            self._atr_long_term_sum -= lt[0]
        lt.append(v)
        self._atr_long_term_sum += v

    def _effective_atr_floor(self) -> float:
        n_short = len(self._atr_history)
        if n_short < _ATR_HISTORY_MIN_SAMPLES:
            return self.min_atr
        rolling_mean = self._atr_history_sum / n_short
        base_floor = max(self.min_atr, _ATR_FLOOR_FRACTION * rolling_mean)
        n_long = len(self._atr_long_term)
        if n_long >= _ATR_LONG_TERM_MIN_SAMPLES:
            long_term_mean = self._atr_long_term_sum / n_long
            if long_term_mean > 0 and rolling_mean / long_term_mean < _ATR_REGIME_SHIFT_THRESHOLD:
                regime_floor = long_term_mean * _ATR_REGIME_SHIFT_THRESHOLD * _ATR_FLOOR_FRACTION
                return max(base_floor, regime_floor)
        return base_floor

    def effective_momentum_weight(self, regime_autocorr: float) -> float:
        """Regime-aware magnitude scaler for L4 (unsigned).

        Polarity per-indicator-group is handled inside `compute_momentum`; this
        function returns only the *magnitude* with regime amplification:
          |autocorr| > threshold → amplify 1.5× (clear regime, lean harder)
          else                    → dampen 0.5× (no regime, soft contribution)

        Returned magnitude is clamped to _MOMENTUM_WEIGHT_CLAMP. The sign of
        `momentum_weight` is irrelevant after this change — only |momentum_weight|
        is consulted, so legacy negative-fade defaults still work unchanged.
        """
        base = abs(self.momentum_weight)
        if abs(regime_autocorr) > _REGIME_MOMENTUM_THRESHOLD:
            magnitude = base * _REGIME_MOMENTUM_AMPLIFY
        else:
            magnitude = base * _REGIME_MOMENTUM_DAMPEN
        return min(_MOMENTUM_WEIGHT_CLAMP, magnitude)

    @property
    def entry_threshold(self) -> float:
        return self.min_edge

    @entry_threshold.setter
    def entry_threshold(self, value: float) -> None:
        self.min_edge = value

    def compute_regime_factor(self, closes) -> float:
        """1-lag autocorr of recent returns so the regime detector and L2
        cannot disagree on the same closes array.. Positive=trending, negative=reverting.
        """
        return lag1_autocorr(closes, self.regime_lookback)

    def compute_probability(self, btc_price: float, strike_price: float,
                            seconds_remaining: float, atr: float,
                            indicators: dict | None = None,
                            closes: np.ndarray | None = None,
                            flow_signal: float = 0.0,
                            spot_flow_signal: float = 0.0,
                            prev_resolution_margin: float = 0.0,
                            liquidation_pressure: float = 0.0) -> float:
        """P(Up) at expiry — Student-t CDF + logit-space layer adjustments + Platt."""
        if atr <= 0 or seconds_remaining <= 0:
            return 0.5

        distance = btc_price - strike_price
        minutes_remaining = max(seconds_remaining / 60.0, 0.01)

        self._record_atr(atr)
        atr_effective = max(atr, self._effective_atr_floor())
        vol_scaled = (atr_effective / self.atr_sigma_ratio) * math.sqrt(minutes_remaining)
        if vol_scaled <= 0:
            return 0.5

        z = distance / vol_scaled
        # df clamped to ≥3 (pipeline range is 3-8). Removes the t_scale=1.0 fallback
        # discontinuity that jumped to √(3/1)=1.73 the moment df reached 3.
        df_eff = max(_MIN_STUDENT_T_DF, self.student_t_df)
        t_scale = math.sqrt(df_eff / (df_eff - 2))
        prob_up = float(_stdtr(df_eff, z * t_scale))

        # Tight clip — preserves deep-ITM/OTM precision so the final logit
        # clamp is the only place L1 information is bounded.
        prob_up = max(_L1_CLIP, min(1.0 - _L1_CLIP, prob_up))
        logit_p = math.log(prob_up / (1.0 - prob_up))

        logit_regime_w = self.regime_weight * self.logit_scale
        logit_flow_w = self.flow_weight * self.logit_scale

        # L2 — regime autocorr × direction of last 1-min return
        regime = self.compute_regime_factor(closes) if closes is not None else 0.0
        self.last_regime_autocorr = regime
        # L4 magnitude is regime-amplified; polarity is handled per-group inside compute_momentum.
        logit_momentum_w = self.effective_momentum_weight(regime) * self.logit_scale
        if closes is not None and len(closes) >= 2:
            last_return = float(closes[-1] - closes[-2]) / float(closes[-2])
            direction = 1.0 if last_return > 0 else (-1.0 if last_return < 0 else 0.0)
        else:
            direction = 0.0
        self.last_regime_direction = direction
        logit_p += regime * direction * logit_regime_w

        # L3 + L3b: CLOB flow + spot flow, capped collectively to prevent triple-counting
        logit_before_flow = logit_p
        logit_p += flow_signal * logit_flow_w
        logit_p += spot_flow_signal * (self.spot_flow_weight * self.logit_scale)
        max_flow_logit = 0.35
        flow_total = logit_p - logit_before_flow
        if abs(flow_total) > max_flow_logit:
            logit_p = logit_before_flow + max_flow_logit * (1.0 if flow_total > 0 else -1.0)

        # L3e — liquidation pressure
        if liquidation_pressure != 0.0:
            logit_p += liquidation_pressure * (self.liquidation_weight * self.logit_scale)

        # L5 — previous-window margin carry, tanh-normalized by ATR. Dampened by
        # regime strength: |regime| ~1 means L2 already encodes the same drift,
        # so L5 contributes only its orthogonal-info portion.
        if prev_resolution_margin != 0.0 and atr > 0:
            l5_damp = 1.0 - min(_L5_REGIME_DAMP_CAP, abs(regime))
            logit_p += (math.tanh(prev_resolution_margin / max(atr, 1.0))
                        * (self.prev_margin_weight * self.logit_scale)
                        * l5_damp)

        # L4 — indicator committee (regime-aware polarity per-group, sign-coherent)
        if indicators:
            logit_p += self.compute_momentum(indicators, regime) * logit_momentum_w

        # Hard-clamp total logit to prevent any single day's signal stack from
        # producing absurd probabilities (e.g., 0.998 on a cascade of aligned signals).
        logit_p = max(-4.0, min(4.0, logit_p))

        prob_up = 1.0 / (1.0 + math.exp(-logit_p))
        self.last_raw_prob_up = prob_up
        if self.calibrator:
            prob_up = self.calibrator.calibrate(prob_up)
        return prob_up

    def compute_momentum(self, indicators: dict[str, dict], regime_autocorr: float = 0.0) -> float:
        """L4 indicator aggregate, signed coherently with the regime.

        Indicators split by *native polarity*:
          Mean-reverting (fade-aligned by construction):
            RSI score: high → negative (overbought → bearish)
            Stochastic score: high → negative
            VWAP score: -(price - vwap) — above vwap → negative
          Trend-confirming (trend-aligned by construction):
            MACD score: positive histogram → positive (bullish momentum)
            OBV score: agreement between volume slope and price slope → ±

        Regime conditioning is applied PER GROUP so trend-confirm indicators
        aren't sign-flipped together with mean-revert indicators (the original
        aggregate-level flip stomped OBV's correct directional reading in
        trending regimes):

          Trending (autocorr > +0.15):
            flip mean-revert sign → both groups now align with the trend.
          Mean-reverting (autocorr < -0.15):
            keep mean-revert; dampen trend-confirm (it's mostly noise in chop).
          Neutral:
            dampen both — no regime signal to lean on.

        Output is clamped to [-1, 1]; the magnitude/regime amp lives in
        `effective_momentum_weight` at the call site.
        """
        w = self.weights
        def _s(name: str) -> float:
            ind = indicators.get(name, {})
            return ind.get("norm_score", ind.get("score", 0))

        mean_revert = (
            _s("rsi") * w.get("rsi", 0.20)
            + _s("stochastic") * w.get("stochastic", 0.20)
            + _s("vwap") * w.get("vwap", 0.20)
        )
        trend_confirm = (
            _s("macd") * w.get("macd", 0.25)
            + _s("obv") * w.get("obv", 0.15)
        )

        if regime_autocorr > _REGIME_MOMENTUM_THRESHOLD:
            score = -mean_revert + trend_confirm
        elif regime_autocorr < -_REGIME_MOMENTUM_THRESHOLD:
            score = mean_revert + _REGIME_MOMENTUM_DAMPEN * trend_confirm
        else:
            score = _REGIME_MOMENTUM_DAMPEN * (mean_revert + trend_confirm)

        return max(-1.0, min(1.0, score))

    def evaluate(self, indicators: dict[str, dict], has_position: bool, in_entry_window: bool,
                 btc_price: float = 0, strike_price: float = 0,
                 seconds_remaining: float = 0, market_price_up: float = 0.5,
                 market_price_down: float = 0.5,
                 closes: np.ndarray | None = None,
                 flow_signal: float = 0.0,
                 spot_flow_signal: float = 0.0,
                 prev_resolution_margin: float = 0.0,
                 liquidation_pressure: float = 0.0) -> TradeSignal:
        if not in_entry_window:
            return TradeSignal("SKIP", 0.5, 0, 0, "Outside entry window")
        if has_position:
            return TradeSignal("SKIP", 0.5, 0, 0, "Already have position")
        if btc_price <= 0 or strike_price <= 0:
            return TradeSignal("SKIP", 0.5, 0, 0, "No BTC/strike price")

        atr_data = indicators.get("atr", {})
        if not atr_data.get("passes", True):
            return TradeSignal("SKIP", 0.5, 0, 0, f"ATR gate: {atr_data.get('reason', 'unknown')}")

        atr = atr_data.get("atr", 0)
        prob_up = self.compute_probability(btc_price, strike_price,
                                           seconds_remaining, atr, indicators,
                                           closes=closes,
                                           flow_signal=flow_signal,
                                           spot_flow_signal=spot_flow_signal,
                                           prev_resolution_margin=prev_resolution_margin,
                                           liquidation_pressure=liquidation_pressure)
        prob_down = 1.0 - prob_up
        best_prob = max(prob_up, prob_down)
        if best_prob < self.min_model_probability:
            return TradeSignal("SKIP", best_prob, 0, 0,
                               f"Low confidence: model={best_prob:.0%} < min={self.min_model_probability:.0%}")

        edge_up = prob_up - market_price_up
        edge_down = prob_down - market_price_down
        if edge_up >= edge_down:
            best_side, best_edge, best_prob, best_mkt = "BUY_YES", edge_up, prob_up, market_price_up
        else:
            best_side, best_edge, best_prob, best_mkt = "BUY_NO", edge_down, prob_down, market_price_down

        if best_prob < self.min_model_probability:
            return TradeSignal("SKIP", best_prob, best_edge, 0,
                               f"model={best_prob:.0%} < min={self.min_model_probability:.0%}")

        if best_edge < self.min_edge:
            return TradeSignal("SKIP", best_prob, best_edge, 0,
                               f"No edge: best={best_edge:+.1%} < floor={self.min_edge:.1%}")

        kelly = self._kelly(best_prob, best_mkt)
        if kelly < self.min_kelly:
            return TradeSignal("SKIP", best_prob, best_edge, 0,
                               f"Kelly too small: {kelly:.1%} < {self.min_kelly:.1%}")

        if best_side == "BUY_YES":
            return TradeSignal(
                "BUY_YES", prob_up, edge_up, kelly,
                f"Up: model={prob_up:.0%} mkt={market_price_up:.0%} edge={edge_up:+.0%} "
                f"BTC={btc_price:,.0f} strike={strike_price:,.0f} d={btc_price-strike_price:+,.0f}")
        return TradeSignal(
            "BUY_NO", prob_down, edge_down, kelly,
            f"Down: model={prob_down:.0%} mkt={market_price_down:.0%} edge={edge_down:+.0%} "
            f"BTC={btc_price:,.0f} strike={strike_price:,.0f} d={btc_price-strike_price:+,.0f}")

    def evaluate_hold(self, indicators: dict[str, dict], btc_price: float, strike_price: float,
                      seconds_remaining: float, market_price_for_side: float,
                      side: str, exit_threshold: float = -0.10,
                      entry_price: float = 0.0, fee_rate: float = 0.018,
                      closes: np.ndarray | None = None,
                      flow_signal: float = 0.0,
                      spot_flow_signal: float = 0.0,
                      prev_resolution_margin: float = 0.0,
                      liquidation_pressure: float = 0.0) -> tuple[str, float, float, str]:
        """Decide HOLD vs EXIT each tick using the same model as entry.
        Returns (action, model_prob, holding_edge, reason).
        """
        atr = indicators.get("atr", {}).get("atr", 0)
        prob_up = self.compute_probability(btc_price, strike_price,
                                           seconds_remaining, atr, indicators,
                                           closes=closes,
                                           flow_signal=flow_signal,
                                           spot_flow_signal=spot_flow_signal,
                                           prev_resolution_margin=prev_resolution_margin,
                                           liquidation_pressure=liquidation_pressure)
        model_prob = prob_up if side == "Up" else 1.0 - prob_up
        holding_edge = model_prob - market_price_for_side

        itm_depth = max(0.0, (market_price_for_side - 0.5) / 0.5)
        deep_loss_floor = exit_threshold * (1.0 + 0.5 * itm_depth)

        optimal_threshold = self._exit_boundary.compute_exit_threshold(
            seconds_remaining, entry_price, fee_rate, market_price_for_side)
        # Blend: ATM trusts the boundary; deeper ITM weights toward the more patient floor.
        effective_threshold = (
            (1 - itm_depth) * max(deep_loss_floor, optimal_threshold)
            + itm_depth * min(deep_loss_floor, optimal_threshold)
        )

        # Loss-cut: deep underwater near expiry AND BTC is genuinely past strike
        # (>0.5×ATR). The ATR guard suppresses whipsaw-induced false cuts when
        # BTC sits on the strike and the contract flickers 5¢↔70¢ on thin prints.
        atr_for_cut = indicators.get("atr", {}).get("atr", 0) or 0
        btc_dist = abs(btc_price - strike_price)
        wrong_side = (
            (side == "Up" and btc_price < strike_price)
            or (side == "Down" and btc_price > strike_price)
        )
        whip_saw_safe = wrong_side and (atr_for_cut <= 0 or btc_dist > 0.5 * atr_for_cut)
        if (entry_price > 0
                and market_price_for_side < entry_price * self.loss_cut_fraction
                and seconds_remaining < self.loss_cut_time_s
                and whip_saw_safe):
            return ("EXIT", model_prob, holding_edge,
                    f"cutting loss — market dropped to {market_price_for_side:.2f} "
                    f"(entered at {entry_price:.2f}) with only {seconds_remaining:.0f}s left, "
                    f"BTC {btc_dist:.0f} from strike (>0.5×ATR={0.5*atr_for_cut:.0f})")

        # Past _DEEP_LOSS_HOLD_THRESHOLD the binary residual beats scalping the loss.
        # Skipped on profitable positions (market > entry) so a price spike still exits.
        if (holding_edge < _DEEP_LOSS_HOLD_THRESHOLD
                and (entry_price <= 0 or market_price_for_side < entry_price)):
            return ("HOLD", model_prob, holding_edge,
                    f"holding to resolution — deeply underwater but better odds holding than selling now")

        if holding_edge <= effective_threshold:
            return ("EXIT", model_prob, holding_edge,
                    f"market price {market_price_for_side:.2f} has moved against us "
                    f"(model still sees {model_prob:.0%}) — exiting before it slips further")
        return ("HOLD", model_prob, holding_edge,
                f"Hold {side}: model={model_prob:.0%} mkt={market_price_for_side:.0%} "
                f"edge={holding_edge:+.0%}")

    def _kelly(self, prob: float, market_price: float, fee_rate: float = 0.018) -> float:
        """Fee-aware Kelly fraction.

        Polymarket collects the entry fee in shares, so for $1 invested at price
        p you receive (1/p) × (1 - fee_rate × (1-p)) shares. Working through:
            net_b = b × (1 - fee_rate),   where b = (1-p)/p
        Kelly with the fee-adjusted payoff therefore divides by (1 - fee_rate)
        less of the raw b. Effect at fee_rate=0.018: ~5% smaller positions at
        mid prices. Resolution fees are zero (fee = rate × shares × price ×
        (1-price), which collapses to 0 at price 0 or 1), so no second-order
        adjustment is needed.
        """
        if market_price <= 0.01 or market_price >= 0.99:
            return 0
        b = (1.0 - market_price) / market_price
        net_b = b * max(1e-6, 1.0 - fee_rate)
        raw = (prob * net_b - (1.0 - prob)) / net_b
        return max(0, raw * self.kelly_fraction)
