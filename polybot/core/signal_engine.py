from __future__ import annotations

import math
import logging
from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy.stats import t as student_t

from polybot.core.exit_boundary import ExitBoundary

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

# Rolling (predicted_prob, won) buffer; bucketed by confidence so the model can
# learn that *only its extreme predictions* are miscalibrated, without dampening
# the moderate predictions that are working fine.
_CALIBRATION_BUFFER_SIZE = 100
_CALIBRATION_MIN_SAMPLES = 30
_CALIBRATION_DRIFT_FILE = "polybot/memory/adaptive_calibration.json"
_CALIBRATION_BUCKET_MIN_SAMPLES = 15

# Buckets keyed by side-confidence = max(p, 1-p). Boundaries chosen so the
# entry threshold (0.58) starts the moderate bucket and the extreme bucket
# captures the cases where the model and market most often disagree.
_CALIBRATION_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("moderate", 0.58, 0.70),
    ("high",     0.70, 0.85),
    ("extreme",  0.85, 1.01),  # 1.01 so 1.0 is inclusive at the right
)

# Disagreement buckets keyed by |model_prob - market_price|. When the market
# disagrees strongly with the model and the model historically loses those, the
# bot learns to compress probabilities in the high-disagreement range — directly
# addressing the failure mode where the model held a "+54% edge" position into a
# loss because the market knew something it didn't.
_DISAGREEMENT_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("agree",     0.0,  0.10),
    ("medium",    0.10, 0.25),
    ("strong",    0.25, 1.01),
)

logger = logging.getLogger(__name__)

@dataclass
class TradeSignal:
    action: str          # "BUY_YES", "BUY_NO", "SKIP"
    prob: float          # Model probability for the chosen side (0-1)
    edge: float          # Model probability - market price
    kelly_size: float    # Optimal fraction of bankroll
    reason: str


def compute_signal_consensus(signals: dict[str, float], side: str,
                              dead_zone: float = 0.05,
                              consensus_config: dict | None = None) -> float:
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
    """

    def __init__(self, min_edge: float = 0.04, kelly_fraction: float = 0.15,
                 momentum_weight: float = -0.02, weights: dict[str, float] | None = None,
                 min_model_probability: float = 0.58,
                 student_t_df: int = 5, regime_weight: float = 0.03,
                 flow_weight: float = 0.04, regime_lookback: int = 50,
                 min_kelly: float = 0.015, atr_sigma_ratio: float = 1.4,
                 calibrator: 'PlattCalibrator | None' = None,
                 spot_flow_weight: float = 0.04,
                 prev_margin_weight: float = 0.02,
                 min_atr: float = 8.0,
                 liquidation_weight: float = 0.03,
                 logit_scale: float = 4.0,
                 probability_compression: float = 1.0,
                 consensus_dead_zone: float = 0.05,
                 consensus_config: dict | None = None,
                 exit_config: dict | None = None) -> None:
        self.min_edge: float = min_edge
        self.kelly_fraction: float = kelly_fraction
        self.momentum_weight: float = momentum_weight
        self.min_model_probability: float = min_model_probability
        self.student_t_df: int = student_t_df
        self.regime_weight: float = regime_weight
        self.flow_weight: float = flow_weight
        self.regime_lookback: int = regime_lookback
        self.weights: dict[str, float] = weights or {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                   "obv": 0.15, "vwap": 0.20}
        self.min_kelly: float = min_kelly
        self.atr_sigma_ratio: float = atr_sigma_ratio
        self.calibrator = calibrator
        self.spot_flow_weight: float = spot_flow_weight
        self.prev_margin_weight: float = prev_margin_weight
        self.min_atr: float = min_atr
        self.liquidation_weight: float = liquidation_weight
        self.logit_scale: float = logit_scale
        self.probability_compression: float = probability_compression
        self.consensus_dead_zone: float = consensus_dead_zone
        self.consensus_config: dict = consensus_config or {
            "very_high_pct": 0.80, "very_high_mult": 1.3,
            "high_pct": 0.60, "high_mult": 1.0,
            "medium_pct": 0.40, "medium_mult": 0.8,
            "low_mult": 0.6,
        }
        # The hold_min_prob/panic_edge override only fires when the model strongly
        # favors our side AND the edge isn't already meaningfully negative.
        self.exit_config: dict = exit_config or {
            "patience_seconds": 120, "patience_max_penalty": 0.05,
            "urgency_seconds": 120, "urgency_max_bonus": 0.05,
            "hold_min_prob": 0.70, "panic_edge": -0.10,
            "low_price_hold": 0.15,
        }
        self._exit_boundary = ExitBoundary(df=self.student_t_df)
        self._atr_history: deque[float] = deque(maxlen=_ATR_HISTORY_SIZE)
        self._atr_long_term: deque[float] = deque(maxlen=_ATR_LONG_TERM_SIZE)
        self.last_regime_autocorr: float = 0.0
        self.last_regime_direction: float = 0.0
        # Adaptive calibration buffer (predicted_prob, market_price, won) — persisted
        # across restarts. Two orthogonal learning loops:
        #   confidence buckets: drift in the model's prediction range (moderate/high/extreme)
        #   disagreement buckets: drift conditional on |model - market| (agree/medium/strong)
        # When evaluating a new prediction the smaller of the two multipliers wins,
        # so either signal can trigger compression independently.
        self._calibration_buffer: deque[tuple[float, float | None, bool]] = deque(maxlen=_CALIBRATION_BUFFER_SIZE)
        self._bucket_mults: dict[str, float] = {name: 1.0 for name, _, _ in _CALIBRATION_BUCKETS}
        self._disagreement_mults: dict[str, float] = {name: 1.0 for name, _, _ in _DISAGREEMENT_BUCKETS}
        self._load_calibration_buffer()

    def _record_atr(self, atr: float) -> None:
        if atr > 0:
            self._atr_history.append(float(atr))
            self._atr_long_term.append(float(atr))

    def _effective_atr_floor(self) -> float:
        if len(self._atr_history) < _ATR_HISTORY_MIN_SAMPLES:
            return self.min_atr
        rolling_mean = sum(self._atr_history) / len(self._atr_history)
        base_floor = max(self.min_atr, _ATR_FLOOR_FRACTION * rolling_mean)
        if len(self._atr_long_term) >= _ATR_LONG_TERM_MIN_SAMPLES:
            long_term_mean = sum(self._atr_long_term) / len(self._atr_long_term)
            if long_term_mean > 0 and rolling_mean / long_term_mean < _ATR_REGIME_SHIFT_THRESHOLD:
                regime_floor = long_term_mean * _ATR_REGIME_SHIFT_THRESHOLD * _ATR_FLOOR_FRACTION
                return max(base_floor, regime_floor)
        return base_floor

    def _load_calibration_buffer(self) -> None:
        try:
            from pathlib import Path
            import json as _json
            p = Path(_CALIBRATION_DRIFT_FILE)
            if not p.exists():
                return
            data = _json.loads(p.read_text())
            # Buffer entries: support both old [prob, won] and new [prob, market_price, won].
            for entry in data.get("buffer", []):
                if not isinstance(entry, list):
                    continue
                if len(entry) >= 3:
                    mp = float(entry[1]) if entry[1] is not None else None
                    self._calibration_buffer.append((float(entry[0]), mp, bool(entry[2])))
                elif len(entry) >= 2:
                    self._calibration_buffer.append((float(entry[0]), None, bool(entry[1])))
            mults = data.get("multipliers")
            if isinstance(mults, dict):
                for name in self._bucket_mults:
                    if name in mults:
                        self._bucket_mults[name] = float(mults[name])
            elif "multiplier" in data:  # legacy single-value
                legacy = float(data["multiplier"])
                self._bucket_mults = {name: legacy for name in self._bucket_mults}
            dis = data.get("disagreement_multipliers")
            if isinstance(dis, dict):
                for name in self._disagreement_mults:
                    if name in dis:
                        self._disagreement_mults[name] = float(dis[name])
        except Exception:
            pass  # First run or corrupted file — start fresh

    def _save_calibration_buffer(self) -> None:
        try:
            from pathlib import Path
            import json as _json
            p = Path(_CALIBRATION_DRIFT_FILE)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "buffer": [
                    [round(prob, 4), (round(mp, 4) if mp is not None else None), bool(won)]
                    for prob, mp, won in self._calibration_buffer
                ],
                "multipliers": {name: round(m, 4) for name, m in self._bucket_mults.items()},
                "disagreement_multipliers": {name: round(m, 4) for name, m in self._disagreement_mults.items()},
            }
            p.write_text(_json.dumps(data, indent=2))
        except Exception:
            pass

    def record_resolution(self, predicted_prob: float, won: bool,
                          market_price: float | None = None) -> None:
        """Append a resolved trade and refresh per-bucket compression multipliers.

        ``market_price`` is the chosen side's market price at entry. When supplied
        the trade also feeds the disagreement-bucket learning loop; otherwise it
        only updates confidence buckets (legacy / paper-mode trades without
        market_price recorded).
        """
        if not (0.0 < predicted_prob < 1.0):
            return
        mp = float(market_price) if market_price is not None and 0.0 < market_price < 1.0 else None
        self._calibration_buffer.append((float(predicted_prob), mp, bool(won)))
        self._bucket_mults = self._compute_bucket_multipliers()
        self._disagreement_mults = self._compute_disagreement_multipliers()
        self._save_calibration_buffer()

    @staticmethod
    def _bucket_lookup(value: float, table: tuple[tuple[str, float, float], ...]) -> str | None:
        for name, lo, hi in table:
            if lo <= value < hi:
                return name
        return None

    @staticmethod
    def _drift_to_multiplier(drift: float) -> float:
        """3pp drift -> 1.0, 25pp drift -> 0.50, linear in between, floored at 0.5."""
        if drift <= 0.03:
            return 1.0
        mult = 1.0 - (drift - 0.03) * (0.50 / (0.25 - 0.03))
        return max(0.5, min(1.0, mult))

    def _compute_bucket_multipliers(self) -> dict[str, float]:
        """Per-confidence-bucket multipliers from rolling buffer."""
        result = {name: 1.0 for name, _, _ in _CALIBRATION_BUCKETS}
        if len(self._calibration_buffer) < _CALIBRATION_MIN_SAMPLES:
            return result
        for name, lo, hi in _CALIBRATION_BUCKETS:
            in_bucket = [
                (p, won) for p, _mp, won in self._calibration_buffer
                if lo <= max(p, 1.0 - p) < hi
            ]
            if len(in_bucket) < _CALIBRATION_BUCKET_MIN_SAMPLES:
                continue
            mean_pred = sum(max(p, 1.0 - p) for p, _ in in_bucket) / len(in_bucket)
            mean_actual = sum(1 for _, won in in_bucket if won) / len(in_bucket)
            result[name] = self._drift_to_multiplier(abs(mean_pred - mean_actual))
        return result

    def _compute_disagreement_multipliers(self) -> dict[str, float]:
        """Per-disagreement-bucket multipliers. Uses only entries with market_price.

        Disagreement = |model_prob_for_chosen_side - market_price|. When the
        strong-disagreement bucket has high drift it means the bot loses badly
        when the market disagrees with it — exactly the +54% edge failure mode.
        """
        result = {name: 1.0 for name, _, _ in _DISAGREEMENT_BUCKETS}
        with_mp = [(p, mp, won) for p, mp, won in self._calibration_buffer if mp is not None]
        if len(with_mp) < _CALIBRATION_MIN_SAMPLES:
            return result
        for name, lo, hi in _DISAGREEMENT_BUCKETS:
            in_bucket = [
                (p, won) for p, mp, won in with_mp
                if lo <= abs(p - mp) < hi
            ]
            if len(in_bucket) < _CALIBRATION_BUCKET_MIN_SAMPLES:
                continue
            mean_pred = sum(p for p, _ in in_bucket) / len(in_bucket)
            mean_actual = sum(1 for _, won in in_bucket if won) / len(in_bucket)
            result[name] = self._drift_to_multiplier(abs(mean_pred - mean_actual))
        return result

    def _adaptive_compression_for(self, prob: float, market_price: float | None = None) -> float:
        """Multiplier for the prediction. Returns the more-conservative (smaller) of
        the confidence-bucket and disagreement-bucket multipliers — either signal
        can trigger compression independently. ``market_price`` is the BUY-Up price
        (disagreement is symmetric across sides for binaries)."""
        conf_bucket = self._bucket_lookup(max(prob, 1.0 - prob), _CALIBRATION_BUCKETS)
        conf_mult = self._bucket_mults.get(conf_bucket, 1.0) if conf_bucket else 1.0
        if market_price is None or not (0.0 < market_price < 1.0):
            return conf_mult
        # Disagreement uses the side whose probability matches the market price
        # interpretation (both refer to "Up wins"), so |prob_up - market_price_up|
        # naturally captures how far model and market diverge.
        dis_bucket = self._bucket_lookup(abs(prob - market_price), _DISAGREEMENT_BUCKETS)
        dis_mult = self._disagreement_mults.get(dis_bucket, 1.0) if dis_bucket else 1.0
        return min(conf_mult, dis_mult)

    def get_adaptive_calibration_state(self) -> dict[str, dict]:
        """Per-bucket {n, mean_pred, mean_actual, drift, multiplier} for pipeline
        visibility — both confidence and disagreement loops.
        """
        out: dict[str, dict] = {"confidence": {}, "disagreement": {}}
        for name, lo, hi in _CALIBRATION_BUCKETS:
            in_bucket = [
                (p, won) for p, _mp, won in self._calibration_buffer
                if lo <= max(p, 1.0 - p) < hi
            ]
            n = len(in_bucket)
            if n == 0:
                out["confidence"][name] = {"n": 0, "multiplier": self._bucket_mults.get(name, 1.0)}
                continue
            mean_pred = sum(max(p, 1.0 - p) for p, _ in in_bucket) / n
            mean_actual = sum(1 for _, won in in_bucket if won) / n
            out["confidence"][name] = {
                "n": n,
                "mean_predicted": round(mean_pred, 4),
                "mean_actual": round(mean_actual, 4),
                "drift": round(abs(mean_pred - mean_actual), 4),
                "multiplier": round(self._bucket_mults.get(name, 1.0), 4),
            }
        with_mp = [(p, mp, won) for p, mp, won in self._calibration_buffer if mp is not None]
        for name, lo, hi in _DISAGREEMENT_BUCKETS:
            in_bucket = [
                (p, won) for p, mp, won in with_mp
                if lo <= abs(p - mp) < hi
            ]
            n = len(in_bucket)
            if n == 0:
                out["disagreement"][name] = {"n": 0, "multiplier": self._disagreement_mults.get(name, 1.0)}
                continue
            mean_pred = sum(p for p, _ in in_bucket) / n
            mean_actual = sum(1 for _, won in in_bucket if won) / n
            out["disagreement"][name] = {
                "n": n,
                "mean_predicted": round(mean_pred, 4),
                "mean_actual": round(mean_actual, 4),
                "drift": round(abs(mean_pred - mean_actual), 4),
                "multiplier": round(self._disagreement_mults.get(name, 1.0), 4),
            }
        return out

    def effective_momentum_weight(self, regime_autocorr: float) -> float:
        """Trending: flip sign and amplify. Reverting: keep fade and amplify. Else: dampen."""
        base = self.momentum_weight
        if regime_autocorr > _REGIME_MOMENTUM_THRESHOLD:
            effective = abs(base) * _REGIME_MOMENTUM_AMPLIFY
        elif regime_autocorr < -_REGIME_MOMENTUM_THRESHOLD:
            effective = -abs(base) * _REGIME_MOMENTUM_AMPLIFY
        else:
            effective = base * _REGIME_MOMENTUM_DAMPEN
        return max(-_MOMENTUM_WEIGHT_CLAMP, min(_MOMENTUM_WEIGHT_CLAMP, effective))

    @property
    def entry_threshold(self) -> float:
        return self.min_edge

    @entry_threshold.setter
    def entry_threshold(self, value: float) -> None:
        self.min_edge = value

    def compute_regime_factor(self, closes) -> float:
        """1-lag autocorr of recent returns. Positive=trending, negative=reverting."""
        n = self.regime_lookback
        if len(closes) < n + 2:
            return 0.0
        returns = np.diff(closes[-(n + 1):]) / closes[-(n + 1):-1]
        if len(returns) < 6:
            return 0.0
        r1, r2 = returns[:-1], returns[1:]
        if np.std(r1) == 0 or np.std(r2) == 0:
            return 0.0
        corr = float(np.corrcoef(r1, r2)[0, 1])
        return 0.0 if np.isnan(corr) else max(-1.0, min(1.0, corr))

    def compute_probability(self, btc_price: float, strike_price: float,
                            seconds_remaining: float, atr: float,
                            indicators: dict | None = None,
                            closes: np.ndarray | None = None,
                            flow_signal: float = 0.0,
                            spot_flow_signal: float = 0.0,
                            prev_resolution_margin: float = 0.0,
                            iv_ratio: float = 1.0,
                            liquidation_pressure: float = 0.0,
                            market_price_up: float | None = None) -> float:
        """P(Up) at expiry — Student-t CDF + logit-space layer adjustments + Platt."""
        if atr <= 0 or seconds_remaining <= 0:
            return 0.5

        distance = btc_price - strike_price
        minutes_remaining = max(seconds_remaining / 60.0, 0.01)

        self._record_atr(atr)
        atr_effective = max(atr, self._effective_atr_floor())
        vol_scaled = (atr_effective / self.atr_sigma_ratio) * math.sqrt(minutes_remaining) * iv_ratio
        if vol_scaled <= 0:
            return 0.5

        z = distance / vol_scaled
        # Scale z by sqrt(df/(df-2)) so ATR (≈σ_true) matches t-distribution variance
        t_scale = math.sqrt(self.student_t_df / (self.student_t_df - 2)) if self.student_t_df > 2 else 1.0
        prob_up = float(student_t.cdf(z * t_scale, df=self.student_t_df))

        # Static + bucketed adaptive compression toward 0.5. Adaptive multiplier is
        # bucket-specific (confidence) AND market-aware (disagreement) — either
        # signal can trigger compression. moderate-confidence + market-agrees
        # predictions stay untouched; extreme-confidence OR market-strongly-disagrees
        # gets compressed.
        effective_compression = self.probability_compression * self._adaptive_compression_for(prob_up, market_price_up)
        if effective_compression < 1.0:
            prob_up = 0.5 + (prob_up - 0.5) * effective_compression

        prob_up = max(0.001, min(0.999, prob_up))
        logit_p = math.log(prob_up / (1.0 - prob_up))

        logit_regime_w = self.regime_weight * self.logit_scale
        logit_flow_w = self.flow_weight * self.logit_scale

        # L2 — regime autocorr × direction of last 1-min return
        regime = self.compute_regime_factor(closes) if closes is not None else 0.0
        self.last_regime_autocorr = regime
        # L4 weight is regime-conditional (computed once, applied at the bottom)
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

        # L5 — previous-window margin carry, tanh-normalized by ATR
        if prev_resolution_margin != 0.0 and atr > 0:
            logit_p += math.tanh(prev_resolution_margin / max(atr, 1.0)) * (self.prev_margin_weight * self.logit_scale)

        # L4 — indicator momentum
        if indicators:
            logit_p += self.compute_momentum(indicators) * logit_momentum_w

        prob_up = 1.0 / (1.0 + math.exp(-logit_p))
        if self.calibrator:
            prob_up = self.calibrator.calibrate(prob_up)
        return prob_up

    def compute_momentum(self, indicators: dict[str, dict]) -> float:
        w = self.weights
        def _score(name: str) -> float:
            ind = indicators.get(name, {})
            return ind.get("norm_score", ind.get("score", 0))
        return max(-1.0, min(1.0,
            _score("rsi") * w.get("rsi", 0.20) +
            _score("macd") * w.get("macd", 0.25) +
            _score("stochastic") * w.get("stochastic", 0.20) +
            _score("obv") * w.get("obv", 0.15) +
            _score("vwap") * w.get("vwap", 0.20)
        ))

    def evaluate(self, indicators: dict[str, dict], has_position: bool, in_entry_window: bool,
                 btc_price: float = 0, strike_price: float = 0,
                 seconds_remaining: float = 0, market_price_up: float = 0.5,
                 market_price_down: float = 0.5,
                 closes: np.ndarray | None = None,
                 flow_signal: float = 0.0,
                 spot_flow_signal: float = 0.0,
                 prev_resolution_margin: float = 0.0,
                 iv_ratio: float = 1.0,
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
                                           iv_ratio=iv_ratio,
                                           liquidation_pressure=liquidation_pressure,
                                           market_price_up=market_price_up if 0.0 < market_price_up < 1.0 else None)
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

        if best_edge < self.min_edge:
            return TradeSignal("SKIP", best_prob, best_edge, 0,
                               f"No edge: best={best_edge:+.0%} < floor={self.min_edge:.0%}")

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
                      entry_price: float = 0.0, fee_rate: float = 0.072,
                      closes: np.ndarray | None = None,
                      flow_signal: float = 0.0,
                      spot_flow_signal: float = 0.0,
                      prev_resolution_margin: float = 0.0,
                      iv_ratio: float = 1.0,
                      liquidation_pressure: float = 0.0) -> tuple[str, float, float, str]:
        """Decide HOLD vs EXIT each tick using the same model as entry.
        Returns (action, model_prob, holding_edge, reason).
        """
        atr = indicators.get("atr", {}).get("atr", 0)
        # Derive market_price_up from market_price_for_side so the disagreement
        # bucket sees the same divergence regardless of side.
        market_price_up = market_price_for_side if side == "Up" else (1.0 - market_price_for_side)
        prob_up = self.compute_probability(btc_price, strike_price,
                                           seconds_remaining, atr, indicators,
                                           closes=closes,
                                           flow_signal=flow_signal,
                                           spot_flow_signal=spot_flow_signal,
                                           prev_resolution_margin=prev_resolution_margin,
                                           iv_ratio=iv_ratio,
                                           liquidation_pressure=liquidation_pressure,
                                           market_price_up=market_price_up if 0.0 < market_price_up < 1.0 else None)
        model_prob = prob_up if side == "Up" else 1.0 - prob_up
        holding_edge = model_prob - market_price_for_side

        # Fee-aware threshold: subtract exit fee cost from the trigger
        if entry_price > 0 and market_price_for_side > 0:
            scalp_cost = fee_rate * market_price_for_side * (1.0 - market_price_for_side)
            effective_threshold = exit_threshold - scalp_cost
        else:
            effective_threshold = exit_threshold

        ec = self.exit_config
        optimal_threshold = self._exit_boundary.compute_exit_threshold(
            seconds_remaining, entry_price, fee_rate, market_price_for_side)
        effective_threshold = max(effective_threshold, optimal_threshold)

        if holding_edge <= effective_threshold:
            if model_prob >= ec["hold_min_prob"] and holding_edge > ec["panic_edge"]:
                return ("HOLD", model_prob, holding_edge,
                        f"Hold {side} (model still {model_prob:.0%}): "
                        f"mkt={market_price_for_side:.0%} edge={holding_edge:+.0%}")
            # Don't panic-sell at deep-OTM prices — option value exceeds recovery cost
            if market_price_for_side < ec["low_price_hold"]:
                return ("HOLD", model_prob, holding_edge,
                        f"Hold {side} (mkt {market_price_for_side:.0%} too low to sell): "
                        f"model={model_prob:.0%} edge={holding_edge:+.0%}")
            return ("EXIT", model_prob, holding_edge,
                    f"Exit {side}: model={model_prob:.0%} mkt={market_price_for_side:.0%} "
                    f"edge={holding_edge:+.0%} thresh={effective_threshold:+.0%} "
                    f"BTC={btc_price:,.0f} strike={strike_price:,.0f}")
        return ("HOLD", model_prob, holding_edge,
                f"Hold {side}: model={model_prob:.0%} mkt={market_price_for_side:.0%} "
                f"edge={holding_edge:+.0%}")

    def _kelly(self, prob: float, market_price: float) -> float:
        if market_price <= 0.01 or market_price >= 0.99:
            return 0
        b = (1.0 - market_price) / market_price
        raw = (prob * b - (1.0 - prob)) / b
        return max(0, raw * self.kelly_fraction)
