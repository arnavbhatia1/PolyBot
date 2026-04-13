from __future__ import annotations

import math
import logging
from dataclasses import dataclass

import numpy as np
from scipy.stats import t as student_t

from polybot.core.exit_boundary import ExitBoundary

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
    """Compute Kelly multiplier based on signal agreement.

    Counts how many independent signals agree with the chosen trade direction.
    More agreement = higher conviction = bigger size.

    Args:
        signals: dict of signal_name -> value (positive = bullish, negative = bearish)
                 Exception: "wall" is INVERTED (positive wall = bearish)
        side: "Up" or "Down" — the trade direction
        dead_zone: signals within this of zero are ignored (noise)
        consensus_config: thresholds and multipliers (pipeline-tunable)

    Returns:
        Multiplier based on agreement percentage (from consensus_config)
    """
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
    for name, val in signals.items():
        if abs(val) < dead_zone:
            continue
        total += 1
        effective_val = -val if name == "wall" else val
        if side == "Up" and effective_val > 0:
            agree += 1
        elif side == "Down" and effective_val < 0:
            agree += 1

    if total == 0:
        return 1.0

    agreement_pct = agree / total
    if agreement_pct >= cc["very_high_pct"]:
        return cc["very_high_mult"]
    elif agreement_pct >= cc["high_pct"]:
        return cc["high_mult"]
    elif agreement_pct >= cc["medium_pct"]:
        return cc["medium_mult"]
    return cc["low_mult"]


class SignalEngine:
    """Determines if a 5-min BTC Up/Down contract is mispriced.

    These contracts resolve to 1.0 (winner) or 0.0 (loser) based on
    whether BTC's Chainlink price at the end of the 5-min window is
    above or below the opening price.

    4-layer probability model:

      Layer 1 — Core: Student-t CDF (fat tails, df=4)
        z = distance / (vol * sqrt(time))
        P(Up) = t.cdf(z, df=4)
        Fat tails give less extreme probabilities than normal CDF when
        BTC is far from strike, finding more edge on underdog side.

      Layer 2 — Regime: 1-lag autocorrelation of recent returns
        Positive autocorr = trending → amplify probability from 0.5
        Negative autocorr = mean-reverting → dampen toward 0.5
        Weight: ±5%

      Layer 3 — Order flow: external buy/sell pressure signal
        Positive = bullish (buy pressure), negative = bearish
        Weight: ±6%
        Passed in by caller from order_flow module.

      Layer 4 — Momentum: indicator weighted score (weakest signal)
        RSI, MACD, Stochastic, OBV, VWAP → directional nudge
        Weight: ±4%

    We trade only when model probability disagrees with market price
    by at least `min_edge`. Kelly sizes the bet based on that edge.
    """

    def __init__(self, min_edge: float = 0.04, kelly_fraction: float = 0.15,
                 momentum_weight: float = -0.02, weights: dict[str, float] | None = None,
                 min_model_probability: float = 0.58,
                 student_t_df: int = 5, regime_weight: float = 0.03,
                 flow_weight: float = 0.04, regime_lookback: int = 50,
                 min_kelly: float = 0.015, atr_sigma_ratio: float = 1.4,
                 calibrator: 'PlattCalibrator | None' = None,
                 spot_flow_weight: float = 0.04,
                 wall_weight: float = 0.05,
                 prev_margin_weight: float = 0.02,
                 conviction_multiplier: bool = True,
                 min_atr: float = 8.0,
                 liquidation_weight: float = 0.03,
                 logit_scale: float = 4.0,
                 probability_compression: float = 1.0,
                 consensus_dead_zone: float = 0.05,
                 conviction_config: dict | None = None,
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
        self.wall_weight: float = wall_weight
        self.prev_margin_weight: float = prev_margin_weight
        self.conviction_multiplier: bool = conviction_multiplier
        self.min_atr: float = min_atr
        self.liquidation_weight: float = liquidation_weight
        self.logit_scale: float = logit_scale
        self.probability_compression: float = probability_compression
        self.consensus_dead_zone: float = consensus_dead_zone
        self.conviction_config: dict = conviction_config or {
            "high_prob": 0.90, "high_mult": 1.3,
            "mid_prob": 0.85, "mid_mult": 1.15,
            "low_prob": 0.72, "low_mult": 0.7,
        }
        self.consensus_config: dict = consensus_config or {
            "very_high_pct": 0.80, "very_high_mult": 1.3,
            "high_pct": 0.60, "high_mult": 1.0,
            "medium_pct": 0.40, "medium_mult": 0.8,
            "low_mult": 0.6,
        }
        self.exit_config: dict = exit_config or {
            "patience_seconds": 120, "patience_max_penalty": 0.05,
            "urgency_seconds": 120, "urgency_max_bonus": 0.05,
            "hold_min_prob": 0.50, "panic_edge": -0.20,
            "low_price_hold": 0.15,
        }
        self._exit_boundary = ExitBoundary(df=self.student_t_df)

    @property
    def entry_threshold(self) -> float:
        return self.min_edge

    @entry_threshold.setter
    def entry_threshold(self, value: float) -> None:
        self.min_edge = value

    def compute_regime_factor(self, closes) -> float:
        """1-lag autocorrelation of recent returns. Positive=trending, negative=reverting."""
        n = self.regime_lookback
        if len(closes) < n + 2:
            return 0.0
        returns = np.diff(closes[-(n + 1):]) / closes[-(n + 1):-1]  # last n returns
        if len(returns) < 6:
            return 0.0
        r1 = returns[:-1]
        r2 = returns[1:]
        if np.std(r1) == 0 or np.std(r2) == 0:
            return 0.0
        corr = float(np.corrcoef(r1, r2)[0, 1])
        if np.isnan(corr):
            return 0.0
        return max(-1.0, min(1.0, corr))

    def compute_probability(self, btc_price: float, strike_price: float,
                            seconds_remaining: float, atr: float,
                            indicators: dict | None = None,
                            closes: np.ndarray | None = None,
                            flow_signal: float = 0.0,
                            spot_flow_signal: float = 0.0,
                            wall_pressure: float = 0.0,
                            prev_resolution_margin: float = 0.0,
                            iv_ratio: float = 1.0,
                            liquidation_pressure: float = 0.0,
                            gex_signal: float = 0.0) -> float:
        """Compute P(Up) — probability BTC finishes above the strike.

        Uses Brownian motion approximation with Student-t CDF (fat tails):
          z = distance / (vol * sqrt(time))
          P(Up) = t.cdf(z, df=student_t_df)

        Then applies regime, order flow, and momentum adjustments.
        """
        if atr <= 0 or seconds_remaining <= 0:
            return 0.5

        distance = btc_price - strike_price
        minutes_remaining = max(seconds_remaining / 60.0, 0.01)

        # ATR floor: prevent extreme z-scores in quiet markets
        atr_effective = max(atr, self.min_atr)

        # Scale ATR to standard deviation. ATR from 1-min candles is the correct
        # 5-minute vol measure. Deribit 30-day IV is NOT used here — it's a regime
        # mismatch (30-day forward variance applied to 5-min windows systematically
        # overestimates vol in quiet periods and underestimates around macro events).
        # iv_ratio kept as parameter for pipeline but defaults to 1.0.
        vol_scaled = (atr_effective / self.atr_sigma_ratio) * math.sqrt(minutes_remaining) * iv_ratio

        if vol_scaled <= 0:
            return 0.5

        z = distance / vol_scaled

        # Fix 5: Normalize z for Student-t variance (df=4 → variance=2, scale=√2)
        if self.student_t_df > 2:
            t_scale = math.sqrt(self.student_t_df / (self.student_t_df - 2))
        else:
            t_scale = 1.0
        prob_up = float(student_t.cdf(z * t_scale, df=self.student_t_df))

        # Probability compression: shrink CDF toward 0.5 before logit layers.
        # Addresses systematic overconfidence (96% model -> 74% actual).
        # 1.0 = identity, 0.5 = halve distance from 0.5. Pipeline-tunable.
        if self.probability_compression < 1.0:
            prob_up = 0.5 + (prob_up - 0.5) * self.probability_compression

        # --- Fix 2: Convert to logit space for Bayesian-correct evidence combination ---
        prob_up = max(0.001, min(0.999, prob_up))
        logit_p = math.log(prob_up / (1.0 - prob_up))

        # Internal weight conversion: at p=0.5, dp/dlogit = 0.25
        # logit_weight = prob_weight * logit_scale preserves behavior at p=0.5
        logit_regime_w = self.regime_weight * self.logit_scale
        logit_flow_w = self.flow_weight * self.logit_scale
        logit_momentum_w = self.momentum_weight * self.logit_scale

        # Fix 1: Layer 2 — Regime: direction from recent return, not prob sign
        regime = self.compute_regime_factor(closes) if closes is not None else 0.0
        if closes is not None and len(closes) >= 2:
            last_return = float(closes[-1] - closes[-2]) / float(closes[-2])
            direction = 1.0 if last_return > 0 else (-1.0 if last_return < 0 else 0.0)
        else:
            direction = 0.0
        logit_p += regime * direction * logit_regime_w

        # Layer 3 — Order flow (Polymarket CLOB)
        logit_before_flow = logit_p
        logit_p += flow_signal * logit_flow_w

        # Layer 3b — Spot market flow (CVD + taker ratio from Binance aggTrades)
        logit_spot_flow_w = self.spot_flow_weight * self.logit_scale
        logit_p += spot_flow_signal * logit_spot_flow_w

        # Layer 3c — Wall pressure near strike (from Binance L2 depth)
        # Positive wall_pressure = resistance above = bearish for Up = reduce logit
        logit_wall_w = self.wall_weight * self.logit_scale
        logit_p -= wall_pressure * logit_wall_w

        # Flow layer cap: prevent multicollinearity from triple-counting order flow evidence
        max_flow_logit = 0.35  # ~one layer's worth of adjustment
        flow_total = logit_p - logit_before_flow
        if abs(flow_total) > max_flow_logit:
            logit_p = logit_before_flow + max_flow_logit * (1.0 if flow_total > 0 else -1.0)

        # Layer 3e — Liquidation pressure (from Bybit OI changes)
        if liquidation_pressure != 0.0:
            logit_liq_w = self.liquidation_weight * self.logit_scale
            logit_p += liquidation_pressure * logit_liq_w

        # Layer 5 — Previous window momentum carry
        if prev_resolution_margin != 0.0 and atr > 0:
            normalized_margin = prev_resolution_margin / max(atr, 1.0)
            logit_prev_w = self.prev_margin_weight * self.logit_scale
            logit_p += math.tanh(normalized_margin) * logit_prev_w

        # Layer 4 — Momentum
        if indicators:
            momentum = self.compute_momentum(indicators)
            logit_p += momentum * logit_momentum_w

        # Convert back via sigmoid — natural (0, 1) bounds, no clamping needed
        prob_up = 1.0 / (1.0 + math.exp(-logit_p))

        # Fix 9: Platt calibration (identity if no calibrator loaded)
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
                 wall_pressure: float = 0.0,

                 prev_resolution_margin: float = 0.0,
                 iv_ratio: float = 1.0,
                 liquidation_pressure: float = 0.0,
                 gex_signal: float = 0.0) -> TradeSignal:

        # Gate: already have position or outside window
        if not in_entry_window:
            return TradeSignal("SKIP", 0.5, 0, 0, "Outside entry window")
        if has_position:
            return TradeSignal("SKIP", 0.5, 0, 0, "Already have position")

        # Gate: need valid price data
        if btc_price <= 0 or strike_price <= 0:
            return TradeSignal("SKIP", 0.5, 0, 0, "No BTC/strike price")

        # Gate: ATR filter — skip if volatility too low or too high
        atr_data = indicators.get("atr", {})
        if not atr_data.get("passes", True):
            reason = atr_data.get("reason", "unknown")
            return TradeSignal("SKIP", 0.5, 0, 0, f"ATR gate: {reason}")

        # Compute model probability
        atr = atr_data.get("atr", 0)
        prob_up = self.compute_probability(btc_price, strike_price,
                                           seconds_remaining, atr, indicators,
                                           closes=closes,
                                           flow_signal=flow_signal,
                                           spot_flow_signal=spot_flow_signal,
                                           wall_pressure=wall_pressure,

                                           prev_resolution_margin=prev_resolution_margin,
                                           iv_ratio=iv_ratio,
                                           liquidation_pressure=liquidation_pressure,
                                           gex_signal=gex_signal)
        prob_down = 1.0 - prob_up

        # Gate: model must be confident enough (not a coin flip)
        best_prob = max(prob_up, prob_down)
        if best_prob < self.min_model_probability:
            return TradeSignal("SKIP", best_prob, 0, 0,
                               f"Low confidence: model={best_prob:.0%} < min={self.min_model_probability:.0%}")

        # Compute edge for each side
        edge_up = prob_up - market_price_up
        edge_down = prob_down - market_price_down

        # Pick the side with more edge
        if edge_up >= edge_down:
            best_side, best_edge, best_prob, best_mkt = "BUY_YES", edge_up, prob_up, market_price_up
        else:
            best_side, best_edge, best_prob, best_mkt = "BUY_NO", edge_down, prob_down, market_price_down

        # Gate: noise floor
        if best_edge < self.min_edge:
            return TradeSignal("SKIP", best_prob, best_edge, 0,
                               f"No edge: best={best_edge:+.0%} < floor={self.min_edge:.0%}")

        # Gate (Fix 3): Kelly must justify a position
        kelly = self._kelly(best_prob, best_mkt)
        if kelly < self.min_kelly:
            return TradeSignal("SKIP", best_prob, best_edge, 0,
                               f"Kelly too small: {kelly:.1%} < {self.min_kelly:.1%}")

        # Entry signal
        if best_side == "BUY_YES":
            return TradeSignal(
                "BUY_YES", prob_up, edge_up, kelly,
                f"Up: model={prob_up:.0%} mkt={market_price_up:.0%} edge={edge_up:+.0%} "
                f"BTC={btc_price:,.0f} strike={strike_price:,.0f} d={btc_price-strike_price:+,.0f}")
        else:
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
                      wall_pressure: float = 0.0,
     
                      prev_resolution_margin: float = 0.0,
                      iv_ratio: float = 1.0,
                      liquidation_pressure: float = 0.0,
                      gex_signal: float = 0.0) -> tuple[str, float, float, str]:
        """Continuously evaluate whether to hold or exit an existing position.

        Uses the same 4-layer probability model as entry. Compares the
        model's current probability for our side against the current market price.
        If the market has moved beyond what the model supports, exit.

        Fee-aware scalp threshold: accounts for exit fee cost so we don't
        scalp when the fees eat the profit. Time urgency: near expiry, lower
        the exit bar (better to scalp than risk total loss).

        Returns: (action, model_prob, holding_edge, reason)
          action: "HOLD" or "EXIT"
          model_prob: current model probability for our side
          holding_edge: model_prob - market_price (negative = should exit)
          reason: human-readable explanation
        """
        atr = indicators.get("atr", {}).get("atr", 0)
        prob_up = self.compute_probability(btc_price, strike_price,
                                           seconds_remaining, atr, indicators,
                                           closes=closes,
                                           flow_signal=flow_signal,
                                           spot_flow_signal=spot_flow_signal,
                                           wall_pressure=wall_pressure,

                                           prev_resolution_margin=prev_resolution_margin,
                                           iv_ratio=iv_ratio,
                                           liquidation_pressure=liquidation_pressure,
                                           gex_signal=gex_signal)
        model_prob = prob_up if side == "Up" else 1.0 - prob_up
        holding_edge = model_prob - market_price_for_side

        # Fee-aware scalp threshold
        if entry_price > 0 and market_price_for_side > 0:
            # Cost of scalping: exit fee as fraction of $1 share value
            exit_fee_per_share = fee_rate * market_price_for_side * (1.0 - market_price_for_side)
            scalp_cost = exit_fee_per_share
            effective_threshold = exit_threshold - scalp_cost
        else:
            effective_threshold = exit_threshold

        # Exit params from config (pipeline-tunable)
        ec = self.exit_config

        # Optimal exit boundary: binary option time value (NOT European option sqrt(t))
        # Deep ITM near expiry: more patient (want $1 resolution, not early exit)
        # Deep OTM near expiry: less patient (cut losses, time value exhausted)
        optimal_threshold = self._exit_boundary.compute_exit_threshold(
            seconds_remaining, entry_price, fee_rate, market_price_for_side)
        effective_threshold = max(effective_threshold, optimal_threshold)

        if holding_edge <= effective_threshold:
            # Trust the model: don't scalp when model still favors our side
            # Exception: deeply negative edge means market knows something we don't
            if model_prob >= ec["hold_min_prob"] and holding_edge > ec["panic_edge"]:
                return ("HOLD", model_prob, holding_edge,
                        f"Hold {side} (model still {model_prob:.0%}): "
                        f"mkt={market_price_for_side:.0%} edge={holding_edge:+.0%}")
            # Don't panic-sell at very low prices — option value exceeds recovery
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
        """Kelly for binary outcome with optional conviction scaling."""
        if market_price <= 0.01 or market_price >= 0.99:
            return 0
        b = (1.0 - market_price) / market_price
        q = 1.0 - prob
        raw = (prob * b - q) / b
        base = max(0, raw * self.kelly_fraction)
        if not self.conviction_multiplier:
            return base
        # Scale Kelly by conviction — thresholds and multipliers from config
        cc = self.conviction_config
        if prob >= cc["high_prob"]:
            return base * cc["high_mult"]
        elif prob >= cc["mid_prob"]:
            return base * cc["mid_mult"]
        elif prob < cc["low_prob"]:
            return base * cc["low_mult"]
        return base
