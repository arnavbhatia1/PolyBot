from __future__ import annotations

import math
import logging
from collections import deque
from dataclasses import dataclass

import numpy as np

from polybot.core.exit_boundary import ExitBoundary, effective_exit_threshold
from polybot.core.returns import lag1_autocorr
from polybot.core.aux_layers import (
    autocorr_vol_scale, student_t_cdf,
    MIN_STUDENT_T_DF as _MIN_STUDENT_T_DF,
)
from polybot.execution.base import DEFAULT_FEE_RATE

# Dynamic ATR floor: max(static, FRACTION × rolling_mean); widened when the
# rolling-20 collapses vs long-term — a low-vol regime makes L1 overconfident.
_ATR_HISTORY_SIZE = 20
_ATR_FLOOR_FRACTION = 0.30
_ATR_HISTORY_MIN_SAMPLES = 5
_ATR_LONG_TERM_SIZE = 200
_ATR_LONG_TERM_MIN_SAMPLES = 50

# L1 prob clip — keeps the CDF away from exact 0/1.
_L1_CLIP = 1e-6

logger = logging.getLogger(__name__)

@dataclass
class TradeSignal:
    action: str          # "BUY_YES", "BUY_NO", "SKIP"
    prob: float          # Model probability for the chosen side (0-1)
    edge: float          # Model probability - market price
    kelly_size: float    # Optimal fraction of bankroll
    reason: str
    side: str = ""       # "Up"/"Down" the prob/edge refer to; "" on pre-model skips


class SignalEngine:
    """P(Up) for 5-min BTC Up/Down: L1 Student-t CDF over distance-to-strike.

    Never rebuild entry-side prediction — the CLOB price beats any feature
    stack. L1 is only a fair-value anchor for the fee/spread/Kelly gates and
    the exit engine (evaluate_hold).
    """

    def __init__(self, min_edge: float = 0.04, kelly_fraction: float = 0.08,
                 min_model_probability: float = 0.56,
                 student_t_df: int = 5,
                 regime_lookback: int = 50,
                 min_kelly: float = 0.01, atr_sigma_ratio: float = 1.3,
                 min_atr: float = 12.0,
                 loss_cut_fraction: float = 0.65,
                 loss_cut_time_s: float = 90.0,
                 deep_loss_hold_threshold: float = -0.10,
                 atr_regime_shift_threshold: float = 0.60) -> None:
        # Test/direct-construction fallbacks — production overrides every one
        # from settings.yaml via _build_signal_engine.
        self.min_edge: float = min_edge
        self.kelly_fraction: float = kelly_fraction
        self.min_model_probability: float = min_model_probability
        self.student_t_df: int = student_t_df
        self.regime_lookback: int = regime_lookback
        self.min_kelly: float = min_kelly
        self.atr_sigma_ratio: float = atr_sigma_ratio
        self.min_atr: float = min_atr
        self.loss_cut_fraction: float = loss_cut_fraction
        self.loss_cut_time_s: float = loss_cut_time_s
        self.deep_loss_hold_threshold: float = deep_loss_hold_threshold
        self.atr_regime_shift_threshold: float = atr_regime_shift_threshold
        self._exit_boundary = ExitBoundary()
        self._atr_history: deque[float] = deque(maxlen=_ATR_HISTORY_SIZE)
        self._atr_long_term: deque[float] = deque(maxlen=_ATR_LONG_TERM_SIZE)
        self._atr_history_sum: float = 0.0
        self._atr_long_term_sum: float = 0.0
        self.last_regime_autocorr: float = 0.0
        self.last_regime_direction: float = 0.0
        self.last_raw_prob_up: float = 0.5
        self.last_loss_cut_event: str = ""
        # Threshold the last evaluate_hold() used — EXIT re-checks (phantom-bid
        # SELL verify) must gate against this, not the raw config value.
        self.last_effective_exit_threshold: float = 0.0
        self.last_atr_rolling_20: float = 0.0
        self.last_atr_long_term_mean: float = 0.0
        # One ATR slot per 1-min candle: exit ticks re-run compute_probability
        # ~1Hz, so without this dedup one candle floods the 20-slot deque.
        self._last_atr_candle_ts: int | None = None

    def _record_atr(self, atr: float, candle_ts: int | None = None) -> None:
        if atr <= 0:
            return
        v = float(atr)
        h = self._atr_history
        lt = self._atr_long_term
        # Repeat candle_ts → REPLACE this candle's slot (appending would let one
        # candle dominate the deques); candle_ts=None (tests) appends every call.
        if candle_ts is not None and candle_ts == self._last_atr_candle_ts and len(h) > 0:
            self._atr_history_sum += v - h[-1]
            h[-1] = v
            self._atr_long_term_sum += v - lt[-1]
            lt[-1] = v
        else:
            if len(h) == h.maxlen:
                self._atr_history_sum -= h[0]
            h.append(v)
            self._atr_history_sum += v
            if len(lt) == lt.maxlen:
                self._atr_long_term_sum -= lt[0]
            lt.append(v)
            self._atr_long_term_sum += v
            self._last_atr_candle_ts = candle_ts
        n_short = len(h)
        self.last_atr_rolling_20 = (self._atr_history_sum / n_short) if n_short > 0 else 0.0
        n_long = len(lt)
        self.last_atr_long_term_mean = (self._atr_long_term_sum / n_long) if n_long > 0 else 0.0

    def _effective_atr_floor(self) -> float:
        n_short = len(self._atr_history)
        if n_short < _ATR_HISTORY_MIN_SAMPLES:
            return self.min_atr
        rolling_mean = self._atr_history_sum / n_short
        base_floor = max(self.min_atr, _ATR_FLOOR_FRACTION * rolling_mean)
        n_long = len(self._atr_long_term)
        if n_long >= _ATR_LONG_TERM_MIN_SAMPLES:
            long_term_mean = self._atr_long_term_sum / n_long
            if long_term_mean > 0 and rolling_mean / long_term_mean < self.atr_regime_shift_threshold:
                regime_floor = long_term_mean * self.atr_regime_shift_threshold * _ATR_FLOOR_FRACTION
                return max(base_floor, regime_floor)
        return base_floor

    def compute_regime_factor(self, closes) -> float:
        """Lag-1 autocorrelation of 1-min closes — L1's vol-scale input (AR(1)
        terminal-SD correction), and exit-context telemetry."""
        if closes is None:
            return 0.0
        return lag1_autocorr(closes, self.regime_lookback)

    def compute_probability(self, btc_price: float, strike_price: float,
                            seconds_remaining: float, atr: float,
                            closes: np.ndarray | None = None,
                            atr_candle_ts: int | None = None) -> float:
        """P(Up) at expiry — Student-t CDF of distance-to-strike over remaining vol."""
        if atr <= 0 or seconds_remaining <= 0:
            self.last_raw_prob_up = 0.5
            return 0.5

        distance = btc_price - strike_price
        minutes_remaining = max(seconds_remaining / 60.0, 0.01)

        # Lag-1 autocorr scales remaining vol: trend widens, mean-reversion tightens.
        regime = self.compute_regime_factor(closes) if closes is not None else 0.0
        self.last_regime_autocorr = regime

        # Last 1-min move direction — telemetry only (live Coinbase tick vs the
        # previous fully-closed Binance candle).
        if closes is not None and len(closes) >= 2 and float(closes[-2]) != 0.0:
            last_return = (btc_price - float(closes[-2])) / float(closes[-2])
        else:
            last_return = 0.0
        self.last_regime_direction = 1.0 if last_return > 0 else (-1.0 if last_return < 0 else 0.0)

        self._record_atr(atr, candle_ts=atr_candle_ts)
        atr_effective = max(atr, self._effective_atr_floor())
        vol_scaled = ((atr_effective / self.atr_sigma_ratio) * math.sqrt(minutes_remaining)
                      * autocorr_vol_scale(regime))
        if vol_scaled <= 0:
            self.last_raw_prob_up = 0.5
            return 0.5

        z = distance / vol_scaled
        # df clamped to ≥3 (shared MIN_STUDENT_T_DF) — df ≤ 2 has undefined
        # variance and t_scale needs df > 2.
        df_eff = max(_MIN_STUDENT_T_DF, self.student_t_df)
        t_scale = math.sqrt(df_eff / (df_eff - 2))
        prob_up = student_t_cdf(z * t_scale, df_eff)
        prob_up = max(_L1_CLIP, min(1.0 - _L1_CLIP, prob_up))
        self.last_raw_prob_up = prob_up
        return prob_up

    def evaluate(self, indicators: dict[str, dict], has_position: bool, in_entry_window: bool,
                 btc_price: float = 0, strike_price: float = 0,
                 seconds_remaining: float = 0, market_price_up: float = 0.5,
                 market_price_down: float = 0.5,
                 closes: np.ndarray | None = None,
                 fee_rate: float = DEFAULT_FEE_RATE) -> TradeSignal:
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
                                           seconds_remaining, atr, closes=closes,
                                           atr_candle_ts=atr_data.get("candle_ts"))
        prob_down = 1.0 - prob_up
        best_prob = max(prob_up, prob_down)
        if best_prob < self.min_model_probability:
            return TradeSignal("SKIP", best_prob, 0, 0,
                               f"below min prob {self.min_model_probability:.0%}",
                               side="Up" if prob_up >= prob_down else "Down")

        edge_up = prob_up - market_price_up
        edge_down = prob_down - market_price_down
        if edge_up >= edge_down:
            best_side, best_edge, best_prob, best_mkt = "BUY_YES", edge_up, prob_up, market_price_up
        else:
            best_side, best_edge, best_prob, best_mkt = "BUY_NO", edge_down, prob_down, market_price_down
        # prob/edge below refer to THIS side — skip logs must say so (edge-best
        # Down at 15% = the model calling 85% Up, not a coin-flip Down).
        side_label = "Up" if best_side == "BUY_YES" else "Down"

        if best_prob < self.min_model_probability:
            return TradeSignal("SKIP", best_prob, best_edge, 0,
                               f"below min prob {self.min_model_probability:.0%}",
                               side=side_label)

        if best_edge < self.min_edge:
            return TradeSignal("SKIP", best_prob, best_edge, 0,
                               f"No edge: best={best_edge:+.1%} < floor={self.min_edge:.1%}",
                               side=side_label)

        kelly = self._kelly(best_prob, best_mkt, fee_rate=fee_rate)
        if kelly < self.min_kelly:
            return TradeSignal("SKIP", best_prob, best_edge, 0,
                               f"Kelly too small: {kelly:.1%} < {self.min_kelly:.1%}",
                               side=side_label)

        if best_side == "BUY_YES":
            return TradeSignal(
                "BUY_YES", prob_up, edge_up, kelly,
                f"Up: model={prob_up:.0%} mkt={market_price_up:.0%} edge={edge_up:+.0%} "
                f"BTC={btc_price:,.0f} strike={strike_price:,.0f} d={btc_price-strike_price:+,.0f}",
                side="Up")
        return TradeSignal(
            "BUY_NO", prob_down, edge_down, kelly,
            f"Down: model={prob_down:.0%} mkt={market_price_down:.0%} edge={edge_down:+.0%} "
            f"BTC={btc_price:,.0f} strike={strike_price:,.0f} d={btc_price-strike_price:+,.0f}",
            side="Down")

    def evaluate_hold(self, indicators: dict[str, dict], btc_price: float, strike_price: float,
                      seconds_remaining: float, market_price_for_side: float,
                      side: str, exit_threshold: float = -0.10,
                      entry_price: float = 0.0, fee_rate: float = DEFAULT_FEE_RATE,
                      closes: np.ndarray | None = None,
                      market_mid_for_side: float | None = None) -> tuple[str, float, float, str]:
        """Decide HOLD vs EXIT each tick with the same model as entry.
        Returns (action, model_prob, holding_edge, reason).

        ``market_price_for_side`` = the bid actually scalped into.
        ``market_mid_for_side`` feeds only the itm_depth patience calc, so a
        wide spread can't make the bot impatient on a still-ITM position.
        """
        atr = indicators.get("atr", {}).get("atr", 0)
        prob_up = self.compute_probability(btc_price, strike_price,
                                           seconds_remaining, atr, closes=closes,
                                           atr_candle_ts=indicators.get("atr", {}).get("candle_ts"))
        model_prob = prob_up if side == "Up" else 1.0 - prob_up
        holding_edge = model_prob - market_price_for_side

        # Blend: ATM trusts the boundary; deeper ITM weights toward the more patient
        # floor. Shared with the exit-threshold replay via exit_boundary.
        effective_threshold = effective_exit_threshold(
            exit_threshold, seconds_remaining, market_price_for_side,
            fee_rate=fee_rate, market_mid_for_side=market_mid_for_side,
            boundary=self._exit_boundary)
        self.last_effective_exit_threshold = effective_threshold

        # Loss-cut: deep underwater near expiry AND BTC truly past strike
        # (>0.5×ATR — blocks whipsaw false cuts when BTC sits on the strike and
        # the contract flickers 5¢↔70¢ on thin prints).
        atr_for_cut = indicators.get("atr", {}).get("atr", 0) or 0
        btc_dist = abs(btc_price - strike_price)
        wrong_side = (
            (side == "Up" and btc_price < strike_price)
            or (side == "Down" and btc_price > strike_price)
        )
        whip_saw_safe = wrong_side and (atr_for_cut <= 0 or btc_dist > 0.5 * atr_for_cut)
        loss_cut_would_fire = (
            entry_price > 0
            and market_price_for_side < entry_price * self.loss_cut_fraction
            and seconds_remaining < self.loss_cut_time_s
        )
        if loss_cut_would_fire and whip_saw_safe:
            # Lock only when holding_edge <= 0 — above, the residual beats the
            # panic bid, so HOLD, and return explicitly so it can't fall through
            # to an OTM-urgency scalp at the same sub-model price.
            if holding_edge > 0:
                self.last_loss_cut_event = ""
                return ("HOLD", model_prob, holding_edge,
                        "holding to resolution — underwater but the model values the "
                        "residual above the current bid")
            self.last_loss_cut_event = "fired"
            return ("EXIT", model_prob, holding_edge,
                    f"cutting loss — market dropped to {market_price_for_side:.2f} "
                    f"(entered at {entry_price:.2f}) with only {seconds_remaining:.0f}s left, "
                    f"BTC {btc_dist:.0f} from strike (>0.5×ATR={0.5*atr_for_cut:.0f})")
        if loss_cut_would_fire and not whip_saw_safe:
            self.last_loss_cut_event = "whipsaw_blocked"
            logger.debug(
                f"loss_cut blocked by whipsaw guard — market {market_price_for_side:.2f} < "
                f"{entry_price * self.loss_cut_fraction:.2f}, secs {seconds_remaining:.0f}, "
                f"BTC dist {btc_dist:.0f} vs 0.5×ATR={0.5*atr_for_cut:.0f}"
            )
        else:
            self.last_loss_cut_event = ""

        # Past deep_loss_hold_threshold the binary residual beats scalping the loss.
        if (holding_edge < self.deep_loss_hold_threshold
                and market_price_for_side < entry_price):
            return ("HOLD", model_prob, holding_edge,
                    "holding to resolution — deeply underwater but better odds holding than selling now")

        # Whipsaw cushion (mirrors the loss-cut guard): within 0.5×ATR of the
        # strike P(side) flips on borderline prints — hold, don't scalp on noise.
        near_strike_whipsaw = (wrong_side and atr_for_cut > 0
                               and btc_dist <= 0.5 * atr_for_cut)
        if holding_edge <= effective_threshold and not near_strike_whipsaw:
            return ("EXIT", model_prob, holding_edge,
                    f"Market ({market_price_for_side:.2f}) has moved against us "
                    f"({model_prob:.0%})")
        return ("HOLD", model_prob, holding_edge,
                f"Hold {side}: model={model_prob:.0%} mkt={market_price_for_side:.0%} "
                f"edge={holding_edge:+.0%}")

    def evaluate_late_sniper(
            self, indicators: dict[str, dict], btc_price: float, strike_price: float,
            seconds_remaining: float, market_ask_up: float, market_ask_down: float,
            cb_move: float | None, cb_move_threshold: float, ask_cap: float,
            sniper_min_edge: float, fee_rate: float = DEFAULT_FEE_RATE,
            closes: np.ndarray | None = None) -> TradeSignal:
        """Late-window sniper: buy the side a sharp Coinbase move pushed past
        the strike while that side's CLOB ask lags (still <= ask_cap).

        Mirrors the `momentum` signal in analyze_late_window.py. The move is
        L1's own favored side; only max_edge (built to dodge stale-AGAINST-us
        prices) rejects it, so the caller bypasses max_edge + the late-window
        time penalty for this action ONLY and keeps every other safety gate.
        Deliberately skips min_model_probability (move-driven, not prob-driven)
        but keeps the `sniper_min_edge` floor: ask at/above L1 fair = book
        already repriced, no lag left to capture.
        Returns LATE_SNIPE_YES / LATE_SNIPE_NO / SKIP.
        """
        if btc_price <= 0 or strike_price <= 0 or cb_move is None:
            return TradeSignal("SKIP", 0.5, 0, 0, "sniper: no price/strike/move")
        if abs(cb_move) < cb_move_threshold:
            return TradeSignal("SKIP", 0.5, 0, 0,
                               f"sniper: move {cb_move:+.1f} < {cb_move_threshold:.1f}")
        up = cb_move > 0
        # the move must have pushed Coinbase past the strike toward the chosen side
        if not ((up and btc_price > strike_price) or ((not up) and btc_price < strike_price)):
            return TradeSignal("SKIP", 0.5, 0, 0, "sniper: move not past strike")
        ask = market_ask_up if up else market_ask_down
        if ask is None or not (0.0 < ask < 1.0):
            return TradeSignal("SKIP", 0.5, 0, 0, "sniper: no executable ask")
        if ask > ask_cap:
            return TradeSignal("SKIP", 0.5, 0, 0,
                               f"sniper: ask {ask:.2f} > cap {ask_cap:.2f} (book already repriced)")
        atr = indicators.get("atr", {}).get("atr", 0)
        if atr is None or atr <= 0:
            # Cold ATR → compute_probability falls back to 0.5; with the ATR gate
            # bypassed the sniper would fire a garbage (0.5 - ask) edge at boot.
            return TradeSignal("SKIP", 0.5, 0, 0, "sniper: ATR not ready")
        prob_up = self.compute_probability(btc_price, strike_price, seconds_remaining, atr,
                                           closes=closes,
                                           atr_candle_ts=indicators.get("atr", {}).get("candle_ts"))
        prob = prob_up if up else 1.0 - prob_up
        edge = prob - ask
        if edge < sniper_min_edge:        # ask already at/above L1 fair -> no stale-cheap lag left
            return TradeSignal("SKIP", prob, edge, 0,
                               f"sniper: book already repriced — edge {edge:+.0%} is below the "
                               f"{sniper_min_edge:.0%} floor",
                               side="Up" if up else "Down")
        # SIZE on the defended edge (ask + sniper_min_edge), NEVER raw L1: L1 is
        # ~+17pp overconfident conditional on firing, and Kelly on that phantom
        # edge upsizes exactly the losing fires. Entry floor and exit engine
        # still use L1; the gate guarantees prob >= ask + sniper_min_edge, so
        # this is always the conservative branch.
        kelly = self._kelly(ask + sniper_min_edge, ask, fee_rate=fee_rate)
        action = "LATE_SNIPE_YES" if up else "LATE_SNIPE_NO"
        side_word = "Up" if up else "Down"
        move_word = "jumped" if cb_move > 0 else "dropped"
        return TradeSignal(
            action, prob, edge, kelly,
            f"Coinbase {move_word} ${abs(cb_move):.0f} across the strike and the {side_word} "
            f"ask is still {ask:.2f} — buying before the book reprices  "
            f"(model {prob:.0%}, edge {edge:+.0%}, BTC {btc_price:,.0f} vs strike {strike_price:,.0f})",
            side=side_word)

    def _kelly(self, prob: float, market_price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
        """Fee-aware Kelly. Entry fee on shares → net_b = b × (1 - fee_rate).
        Resolution fees collapse to 0 at price 0/1, no exit adjustment needed.
        """
        if market_price <= 0.01 or market_price >= 0.99:
            return 0
        b = (1.0 - market_price) / market_price
        net_b = b * max(1e-6, 1.0 - fee_rate)
        raw = (prob * net_b - (1.0 - prob)) / net_b
        return max(0, raw * self.kelly_fraction)
