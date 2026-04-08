import math
import logging
from dataclasses import dataclass

import numpy as np
from scipy.stats import t as student_t

logger = logging.getLogger(__name__)

@dataclass
class TradeSignal:
    action: str          # "BUY_YES", "BUY_NO", "SKIP"
    prob: float          # Model probability for the chosen side (0-1)
    edge: float          # Model probability - market price
    kelly_size: float    # Optimal fraction of bankroll
    reason: str


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

    def __init__(self, min_edge: float = 0.20, kelly_fraction: float = 0.15,
                 momentum_weight: float = 0.04, weights: dict | None = None,
                 min_model_probability: float = 0.65,
                 student_t_df: int = 4, regime_weight: float = 0.05,
                 flow_weight: float = 0.06):
        self.min_edge = min_edge
        self.kelly_fraction = kelly_fraction
        self.momentum_weight = momentum_weight
        self.min_model_probability = min_model_probability
        self.student_t_df = student_t_df
        self.regime_weight = regime_weight
        self.flow_weight = flow_weight
        self.weights = weights or {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                   "obv": 0.15, "vwap": 0.20}

    @property
    def entry_threshold(self):
        return self.min_edge

    @entry_threshold.setter
    def entry_threshold(self, value):
        self.min_edge = value

    def compute_regime_factor(self, closes) -> float:
        """1-lag autocorrelation of recent returns. Positive=trending, negative=reverting."""
        if len(closes) < 12:
            return 0.0
        returns = np.diff(closes[-11:]) / closes[-11:-1]  # last 10 returns
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
                            flow_signal: float = 0.0) -> float:
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

        # Scale volatility by sqrt(time) — standard Brownian motion
        vol_scaled = atr * math.sqrt(minutes_remaining)

        if vol_scaled <= 0:
            return 0.5

        z = distance / vol_scaled

        # Layer 1: Student-t CDF (fat tails, df=4)
        prob_up = float(student_t.cdf(z, df=self.student_t_df))

        # Layer 2: Regime detection — autocorrelation of recent returns
        regime = self.compute_regime_factor(closes) if closes is not None else 0.0
        # Trending: push prob further from 0.5. Reverting: pull toward 0.5.
        direction = 1.0 if prob_up > 0.5 else -1.0
        regime_adj = regime * direction * self.regime_weight
        prob_up += regime_adj

        # Layer 3: Order flow — external buy/sell pressure signal
        flow_adj = flow_signal * self.flow_weight
        prob_up += flow_adj

        # Layer 4: Small momentum nudge from indicators (±4% max)
        if indicators:
            momentum = self.compute_momentum(indicators)
            prob_up += momentum * self.momentum_weight

        return max(0.03, min(0.97, prob_up))

    def compute_momentum(self, indicators: dict) -> float:
        w = self.weights
        return max(-1.0, min(1.0,
            indicators.get("rsi", {}).get("score", 0) * w.get("rsi", 0.20) +
            indicators.get("macd", {}).get("score", 0) * w.get("macd", 0.25) +
            indicators.get("stochastic", {}).get("score", 0) * w.get("stochastic", 0.20) +
            indicators.get("obv", {}).get("score", 0) * w.get("obv", 0.15) +
            indicators.get("vwap", {}).get("score", 0) * w.get("vwap", 0.20)
        ))

    def evaluate(self, indicators: dict, has_position: bool, in_entry_window: bool,
                 btc_price: float = 0, strike_price: float = 0,
                 seconds_remaining: float = 0, market_price_up: float = 0.5,
                 market_price_down: float = 0.5,
                 closes: np.ndarray | None = None,
                 flow_signal: float = 0.0) -> TradeSignal:

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
                                           flow_signal=flow_signal)
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
        if edge_up >= edge_down and edge_up >= self.min_edge:
            kelly = self._kelly(prob_up, market_price_up)
            return TradeSignal(
                "BUY_YES", prob_up, edge_up, kelly,
                f"Up: model={prob_up:.0%} mkt={market_price_up:.0%} edge={edge_up:+.0%} "
                f"BTC={btc_price:,.0f} strike={strike_price:,.0f} d={btc_price-strike_price:+,.0f}")

        elif edge_down > edge_up and edge_down >= self.min_edge:
            kelly = self._kelly(prob_down, market_price_down)
            return TradeSignal(
                "BUY_NO", prob_down, edge_down, kelly,
                f"Down: model={prob_down:.0%} mkt={market_price_down:.0%} edge={edge_down:+.0%} "
                f"BTC={btc_price:,.0f} strike={strike_price:,.0f} d={btc_price-strike_price:+,.0f}")

        else:
            best = max(edge_up, edge_down)
            return TradeSignal("SKIP", max(prob_up, prob_down), best, 0,
                               f"No edge: best={best:+.0%} < min={self.min_edge:.0%}")

    def evaluate_hold(self, indicators: dict, btc_price: float, strike_price: float,
                      seconds_remaining: float, market_price_for_side: float,
                      side: str, exit_threshold: float = -0.10,
                      entry_price: float = 0.0, fee_rate: float = 0.072,
                      closes: np.ndarray | None = None,
                      flow_signal: float = 0.0) -> tuple[str, float, float, str]:
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
                                           flow_signal=flow_signal)
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

        # Time urgency: near expiry, lower the bar (better to scalp than risk total loss)
        time_urgency = max(0.0, 1.0 - seconds_remaining / 120.0)  # ramps in last 2 min
        effective_threshold += time_urgency * 0.05  # up to +5% easier to exit

        if holding_edge <= effective_threshold:
            return ("EXIT", model_prob, holding_edge,
                    f"Exit {side}: model={model_prob:.0%} mkt={market_price_for_side:.0%} "
                    f"edge={holding_edge:+.0%} thresh={effective_threshold:+.0%} "
                    f"BTC={btc_price:,.0f} strike={strike_price:,.0f}")
        return ("HOLD", model_prob, holding_edge,
                f"Hold {side}: model={model_prob:.0%} mkt={market_price_for_side:.0%} "
                f"edge={holding_edge:+.0%}")

    def _kelly(self, prob: float, market_price: float) -> float:
        """Quarter Kelly for binary outcome."""
        if market_price <= 0.01 or market_price >= 0.99:
            return 0
        b = (1.0 - market_price) / market_price
        q = 1.0 - prob
        raw = (prob * b - q) / b
        return max(0, raw * self.kelly_fraction)
