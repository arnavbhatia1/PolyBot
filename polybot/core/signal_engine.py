import math
import logging
from dataclasses import dataclass

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

    The model computes P(Up) from:
      1. Distance: BTC price vs strike (in volatility units)
      2. Time: less time = more certainty about the current side
      3. Volatility: ATR determines how likely a reversal is
      4. Momentum: indicators provide a small directional nudge

    We trade only when model probability disagrees with market price
    by at least `min_edge`. Kelly sizes the bet based on that edge.

    We DO NOT scalp. Binary markets resolve to 0 or 1 — partial exits
    throw away edge. Kelly already accounts for the risk of total loss.
    """

    def __init__(self, min_edge: float = 0.10, kelly_fraction: float = 0.15,
                 momentum_weight: float = 0.08, weights: dict | None = None):
        self.min_edge = min_edge
        self.kelly_fraction = kelly_fraction
        self.momentum_weight = momentum_weight
        self.weights = weights or {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                   "obv": 0.15, "vwap": 0.20}
        self.entry_threshold = min_edge  # backward compat for learning pipeline

    def compute_probability(self, btc_price: float, strike_price: float,
                            seconds_remaining: float, atr: float,
                            indicators: dict | None = None) -> float:
        """Compute P(Up) — probability BTC finishes above the strike.

        Uses Brownian motion approximation:
          z = distance / (vol * sqrt(time))
          P(Up) = Phi(z) ≈ logistic(1.7 * z)
        """
        if atr <= 0 or seconds_remaining <= 0:
            return 0.5

        distance = btc_price - strike_price
        minutes_remaining = max(seconds_remaining / 60.0, 0.1)

        # Scale volatility by sqrt(time) — standard Brownian motion
        vol_scaled = atr * math.sqrt(minutes_remaining)

        if vol_scaled <= 0:
            return 0.5

        z = distance / vol_scaled

        # Logistic approximation of normal CDF
        prob_up = 1.0 / (1.0 + math.exp(-1.7 * z))

        # Small momentum nudge from indicators (±8% max)
        if indicators:
            momentum = self._compute_momentum(indicators)
            prob_up += momentum * self.momentum_weight

        return max(0.03, min(0.97, prob_up))

    def _compute_momentum(self, indicators: dict) -> float:
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
                 market_price_down: float = 0.5) -> TradeSignal:

        # Gate: already have position or outside window
        if not in_entry_window:
            return TradeSignal("SKIP", 0.5, 0, 0, "Outside entry window")
        if has_position:
            return TradeSignal("SKIP", 0.5, 0, 0, "Already have position")

        # Gate: need valid price data
        if btc_price <= 0 or strike_price <= 0:
            return TradeSignal("SKIP", 0.5, 0, 0, "No BTC/strike price")

        # Compute model probability
        atr = indicators.get("atr", {}).get("atr", 0)
        prob_up = self.compute_probability(btc_price, strike_price,
                                           seconds_remaining, atr, indicators)
        prob_down = 1.0 - prob_up

        # Compute edge for each side
        edge_up = prob_up - market_price_up
        edge_down = prob_down - market_price_down

        # Pick the side with more edge
        if edge_up >= edge_down and edge_up >= self.min_edge:
            kelly = self._kelly(prob_up, market_price_up)
            return TradeSignal(
                "BUY_YES", prob_up, edge_up, kelly,
                f"Up: model={prob_up:.0%} mkt={market_price_up:.0%} edge={edge_up:+.0%} "
                f"BTC={btc_price:,.0f} strike={strike_price:,.0f} Δ={btc_price-strike_price:+,.0f}")

        elif edge_down > edge_up and edge_down >= self.min_edge:
            kelly = self._kelly(prob_down, market_price_down)
            return TradeSignal(
                "BUY_NO", prob_down, edge_down, kelly,
                f"Down: model={prob_down:.0%} mkt={market_price_down:.0%} edge={edge_down:+.0%} "
                f"BTC={btc_price:,.0f} strike={strike_price:,.0f} Δ={btc_price-strike_price:+,.0f}")

        else:
            best = max(edge_up, edge_down)
            return TradeSignal("SKIP", max(prob_up, prob_down), best, 0,
                               f"No edge: best={best:+.0%} < min={self.min_edge:.0%}")

    def _kelly(self, prob: float, market_price: float) -> float:
        """Quarter Kelly for binary outcome."""
        if market_price <= 0.01 or market_price >= 0.99:
            return 0
        b = (1.0 - market_price) / market_price
        q = 1.0 - prob
        raw = (prob * b - q) / b
        return max(0, raw * self.kelly_fraction)
