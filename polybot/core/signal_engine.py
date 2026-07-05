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
from polybot.config.param_registry import default_for as _d
from polybot.execution.base import DEFAULT_FEE_RATE

# Dynamic ATR floor: max(static, FRACTION × rolling_mean). When the rolling-20
# ATR collapses well below the long-term mean (regime shift to low vol), widen
# the floor proportionally so L1 doesn't produce overconfident probabilities.
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

    Entry is inventory sourcing, not forecasting — the CLOB price is a better
    predictor than any feature stack (k=0, 44/44 segments, day-clustered).
    The bot's edge lives in the exit engine (evaluate_hold); L1's only job is a
    sane fair-value anchor for the fee/spread/Kelly gates.
    """

    def __init__(self, min_edge: float | None = None, kelly_fraction: float | None = None,
                 min_model_probability: float | None = None,
                 student_t_df: int | None = None,
                 regime_lookback: int | None = None,
                 min_kelly: float | None = None, atr_sigma_ratio: float | None = None,
                 min_atr: float | None = None,
                 loss_cut_fraction: float | None = None,
                 loss_cut_time_s: float | None = None,
                 deep_loss_hold_threshold: float | None = None,
                 atr_regime_shift_threshold: float | None = None) -> None:
        # Defaults resolve from param_registry — settings.yaml drives production via _build_signal_engine.
        if min_edge is None: min_edge = _d("min_edge")
        if kelly_fraction is None: kelly_fraction = _d("kelly_fraction")
        if min_model_probability is None: min_model_probability = _d("min_model_probability")
        if student_t_df is None: student_t_df = _d("student_t_df")
        if regime_lookback is None: regime_lookback = _d("regime_lookback")
        if min_kelly is None: min_kelly = _d("min_kelly")
        if atr_sigma_ratio is None: atr_sigma_ratio = _d("atr_sigma_ratio")
        if min_atr is None: min_atr = _d("min_atr")
        if loss_cut_fraction is None: loss_cut_fraction = _d("loss_cut_fraction")
        if loss_cut_time_s is None: loss_cut_time_s = _d("loss_cut_time_s")
        if deep_loss_hold_threshold is None: deep_loss_hold_threshold = _d("deep_loss_hold_threshold")
        if atr_regime_shift_threshold is None: atr_regime_shift_threshold = _d("atr_regime_shift_threshold")
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
        # Blended exit threshold the most recent evaluate_hold() used. Exposed so
        # EXIT re-checks (e.g. main's phantom-bid SELL verify) gate against the
        # SAME threshold the scalp decision used, not the raw config value.
        self.last_effective_exit_threshold: float = 0.0
        self.last_atr_rolling_20: float = 0.0
        self.last_atr_long_term_mean: float = 0.0
        # Candle timestamp of the last ATR appended to the rolling deques. Keeps
        # ONE slot per 1-min candle: compute_probability runs per exit tick (~1Hz
        # while holding), so without this the same candle's ATR would flood the
        # 20-slot deque and compress the lookback to sub-minute history.
        self._last_atr_candle_ts: int | None = None

    def _record_atr(self, atr: float, candle_ts: int | None = None) -> None:
        if atr <= 0:
            return
        v = float(atr)
        h = self._atr_history
        lt = self._atr_long_term
        # One slot per candle: when candle_ts repeats (intra-minute forming-candle
        # updates re-call compute_probability at ~1Hz), REPLACE this candle's slot
        # with the latest (most-formed) ATR instead of appending — else hundreds of
        # the same candle's values dominate the rolling deques. candle_ts=None
        # (direct/unit-test calls) keeps the legacy append-every-call behavior.
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

        # Regime (lag-1 autocorr) scales remaining vol: positive autocorr (trend)
        # widens terminal spread, negative (mean-reversion) tightens it.
        regime = self.compute_regime_factor(closes) if closes is not None else 0.0
        self.last_regime_autocorr = regime

        # Direction of the last 1-min move — telemetry only (live Coinbase tick
        # vs the previous fully-closed Binance candle).
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
        # The prob/edge in every signal below refer to THIS side — the skip log
        # must label it as such (an edge-best Down at 15% is the model calling
        # 85% Up, not a coin-flip Down).
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
        """Decide HOLD vs EXIT each tick using the same model as entry.
        Returns (action, model_prob, holding_edge, reason).

        ``market_price_for_side`` is the bid the bot would actually scalp into;
        ``market_mid_for_side`` (when supplied) is the (bid+ask)/2 used only for
        the itm_depth patience calculation so wide spreads don't make the bot
        less patient on positions the market still thinks are ITM.
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
        loss_cut_would_fire = (
            entry_price > 0
            and market_price_for_side < entry_price * self.loss_cut_fraction
            and seconds_remaining < self.loss_cut_time_s
        )
        if loss_cut_would_fire and whip_saw_safe:
            # Only LOCK the loss when the model agrees the bid isn't underpricing
            # the residual (holding_edge <= 0). When holding_edge > 0 the model
            # still values the binary residual ABOVE the panic bid, so hold it to
            # resolution (+EV vs selling) instead of cutting into a thin book — and
            # return HOLD explicitly so it can't fall through to an OTM-urgency
            # scalp that would dump it at the same sub-model-value price.
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

        # Whipsaw cushion (mirrors the loss-cut guard): when BTC sits within
        # 0.5×ATR of the strike on the wrong side, P(side) can flip hard on a
        # borderline print, so hold the binary residual rather than scalp out on
        # a noisy strike-side call.
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
        """Final-seconds 'sniper' entry — the one bot-formable late-window edge.

        Mirrors the offline `momentum` signal proven in analyze_late_window.py: a sharp
        Coinbase move (the resolution venue) just pushed price past the strike, but the
        CLOB ask on that side has NOT yet repriced (still <= ask_cap) — a stale-book lag
        in OUR favor. Buy that side. This is L1's own favored side (move past strike =>
        prob>0.5), so it does NOT fight the model; the only reason the normal path rejects
        it is the max_edge cap (built to dodge stale phantom prices, which can't tell a
        stale-against-us phantom from this stale-in-our-favor lag). The caller bypasses
        max_edge + the late-window time penalty for this action ONLY, and keeps every
        safety gate (spread, depth, freshness, price-sum, min-size, pre-submit VWAP) plus
        the whipsaw/loss-cut exit guard.

        Returns LATE_SNIPE_YES / LATE_SNIPE_NO / SKIP. Deliberately does NOT apply
        min_model_probability (the signal is move-driven, not prob-driven) but DOES keep a
        stale-cheap floor (`sniper_min_edge`): if the ask already exceeds L1 fair, the book
        has repriced and there is no lag left to capture.
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
        kelly = self._kelly(prob, ask, fee_rate=fee_rate)
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
