import math
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class TradeSignal:
    action: str          # "BUY_YES", "BUY_NO", "SKIP"
    score: float         # Model probability (0-1)
    edge: float          # Model probability - market price
    kelly_size: float    # Optimal fraction of bankroll
    reason: str
    gate_results: dict


class SignalEngine:
    """Computes whether a 5-min BTC Up/Down contract is mispriced.

    The key insight: these contracts resolve based on whether BTC's price
    at the END of the 5-min window is above or below the OPENING price.
    The contract's market price reflects the crowd's probability estimate.

    Our edge comes from computing a BETTER probability estimate using:
    1. BTC price vs strike (how far above/below the opening price)
    2. Time remaining (less time = more certainty about outcome)
    3. Volatility (higher vol = more uncertain, lower vol = more predictable)
    4. Momentum (are indicators confirming the current direction?)

    We only trade when our model probability significantly disagrees with
    the market price — that's the mispricing we exploit.
    """

    def __init__(self, min_edge: float = 0.10, kelly_fraction: float = 0.25,
                 momentum_weight: float = 0.15, weights: dict | None = None):
        self.min_edge = min_edge           # Minimum edge to trade (10% mispricing)
        self.kelly_fraction = kelly_fraction  # Quarter Kelly
        self.momentum_weight = momentum_weight  # How much indicators adjust base probability
        self.weights = weights or {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                   "obv": 0.15, "vwap": 0.20}
        # For backward compat with learning pipeline
        self.entry_threshold = min_edge

    def _compute_base_probability(self, btc_price: float, strike_price: float,
                                   seconds_remaining: float, atr: float) -> float:
        """Compute probability that BTC stays above (or below) the strike.

        Uses a simple normal distribution model:
        - distance = how far BTC is from the strike (in ATR units)
        - time_factor = sqrt(remaining_minutes) — more time = more uncertainty
        - z_score = distance / (volatility * time_factor)
        - probability = CDF(z_score)
        """
        if atr <= 0 or seconds_remaining <= 0:
            # No volatility data or no time left — use price as probability proxy
            return 0.5

        distance = btc_price - strike_price  # Positive = above strike
        minutes_remaining = max(seconds_remaining / 60.0, 0.1)
        time_factor = math.sqrt(minutes_remaining)

        # Volatility per minute (ATR is per candle = 1 minute)
        vol_per_min = atr

        # Z-score: how many standard deviations away from the strike
        # Higher z = more likely to stay on current side
        z_score = distance / (vol_per_min * time_factor) if vol_per_min * time_factor > 0 else 0

        # Approximate normal CDF using logistic function (fast, close enough)
        # P(BTC stays above strike) ≈ 1 / (1 + exp(-1.7 * z))
        prob_up = 1.0 / (1.0 + math.exp(-1.7 * z_score))

        # Clamp to avoid extreme confidence
        return max(0.05, min(0.95, prob_up))

    def _compute_momentum_adjustment(self, indicators: dict) -> float:
        """Compute a momentum adjustment from technical indicators.

        Returns a value from -1 to +1 that nudges the base probability.
        Positive = bullish momentum, negative = bearish momentum.
        """
        w = self.weights
        score = (
            indicators.get("rsi", {}).get("score", 0) * w.get("rsi", 0.20) +
            indicators.get("macd", {}).get("score", 0) * w.get("macd", 0.25) +
            indicators.get("stochastic", {}).get("score", 0) * w.get("stochastic", 0.20) +
            indicators.get("obv", {}).get("score", 0) * w.get("obv", 0.15) +
            indicators.get("vwap", {}).get("score", 0) * w.get("vwap", 0.20)
        )
        return max(-1.0, min(1.0, score))

    def _check_gates(self, indicators: dict, has_position: bool,
                     in_entry_window: bool) -> tuple[bool, str, dict]:
        gates = {}
        if not in_entry_window:
            gates["entry_window"] = False
            return False, "Outside entry window", gates
        gates["entry_window"] = True
        if has_position:
            gates["position"] = False
            return False, "Already have position", gates
        gates["position"] = True
        atr = indicators.get("atr", {})
        if not atr.get("passes", False):
            gates["atr"] = False
            return False, f"ATR gate failed: {atr.get('reason', 'unknown')}", gates
        gates["atr"] = True
        ema = indicators.get("ema", {})
        if ema.get("trend") in ("chop", "insufficient_data"):
            gates["ema"] = False
            return False, "EMA chop — no clear trend", gates
        gates["ema"] = True
        return True, "all_passed", gates

    def evaluate(self, indicators: dict, has_position: bool, in_entry_window: bool,
                 btc_price: float = 0, strike_price: float = 0,
                 seconds_remaining: float = 0, market_price_up: float = 0.5,
                 market_price_down: float = 0.5) -> TradeSignal:
        """Evaluate whether to trade based on probability model vs market price."""

        passes, reason, gates = self._check_gates(indicators, has_position, in_entry_window)
        if not passes:
            return TradeSignal(action="SKIP", score=0.5, edge=0, kelly_size=0,
                               reason=reason, gate_results=gates)

        # Step 1: Base probability from BTC price vs strike + time + volatility
        atr_value = indicators.get("atr", {}).get("atr", 0)
        prob_up = self._compute_base_probability(btc_price, strike_price,
                                                  seconds_remaining, atr_value)

        # Step 2: Adjust with momentum from indicators (small nudge, not override)
        momentum = self._compute_momentum_adjustment(indicators)
        # Momentum shifts probability by up to ±15%
        prob_up = max(0.05, min(0.95, prob_up + momentum * self.momentum_weight))
        prob_down = 1.0 - prob_up

        # Step 3: Compute edge vs market price
        edge_up = prob_up - market_price_up      # Positive = Up is underpriced
        edge_down = prob_down - market_price_down  # Positive = Down is underpriced

        # Step 4: Trade the side with the bigger edge (if above minimum)
        if edge_up >= self.min_edge and edge_up >= edge_down:
            # Buy Up — model thinks Up is more likely than the market does
            kelly = self._kelly_size(prob_up, market_price_up)
            return TradeSignal(
                action="BUY_YES", score=prob_up, edge=edge_up,
                kelly_size=kelly, reason=f"Up mispriced: model={prob_up:.0%} market={market_price_up:.0%} edge={edge_up:+.0%}",
                gate_results=gates)

        elif edge_down >= self.min_edge and edge_down > edge_up:
            # Buy Down — model thinks Down is more likely than the market does
            kelly = self._kelly_size(prob_down, market_price_down)
            return TradeSignal(
                action="BUY_NO", score=prob_down, edge=edge_down,
                kelly_size=kelly, reason=f"Down mispriced: model={prob_down:.0%} market={market_price_down:.0%} edge={edge_down:+.0%}",
                gate_results=gates)

        else:
            better_edge = max(edge_up, edge_down)
            return TradeSignal(
                action="SKIP", score=max(prob_up, prob_down), edge=better_edge,
                kelly_size=0, reason=f"No edge: best={better_edge:+.0%} < min={self.min_edge:.0%}",
                gate_results=gates)

    def _kelly_size(self, probability: float, market_price: float) -> float:
        """Kelly criterion for binary outcome at given market price.

        f* = (p * b - q) / b
        where p = win probability, q = 1-p, b = payout odds = (1-price)/price
        """
        if market_price <= 0 or market_price >= 1:
            return 0
        b = (1.0 - market_price) / market_price  # Odds
        q = 1.0 - probability
        kelly_raw = (probability * b - q) / b
        # Quarter Kelly for safety
        return max(0, kelly_raw * self.kelly_fraction)
