import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class TradeSignal:
    action: str
    score: float
    reason: str
    gate_results: dict

class SignalEngine:
    def __init__(self, entry_threshold: float = 0.60, weights: dict | None = None):
        self.entry_threshold = entry_threshold
        self.weights = weights or {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20}

    def _check_gates(self, indicators: dict, has_position: bool, in_entry_window: bool) -> tuple[bool, str, dict]:
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
            return False, "EMA chop detected — no clear trend", gates
        gates["ema"] = True
        return True, "all_passed", gates

    def _compute_score(self, indicators: dict) -> float:
        w = self.weights
        score = (indicators["rsi"]["score"] * w.get("rsi", 0.20) +
                 indicators["macd"]["score"] * w.get("macd", 0.25) +
                 indicators["stochastic"]["score"] * w.get("stochastic", 0.20) +
                 indicators["obv"]["score"] * w.get("obv", 0.15) +
                 indicators["vwap"]["score"] * w.get("vwap", 0.20))
        return max(-1.0, min(1.0, score))

    def evaluate(self, indicators: dict, has_position: bool, in_entry_window: bool) -> TradeSignal:
        passes, reason, gates = self._check_gates(indicators, has_position, in_entry_window)
        if not passes:
            return TradeSignal(action="SKIP", score=0.0, reason=reason, gate_results=gates)
        score = self._compute_score(indicators)
        if score >= self.entry_threshold:
            return TradeSignal(action="BUY_YES", score=score, reason="Strong bullish signal", gate_results=gates)
        elif score <= -self.entry_threshold:
            return TradeSignal(action="BUY_NO", score=score, reason="Strong bearish signal", gate_results=gates)
        else:
            return TradeSignal(action="SKIP", score=score,
                               reason=f"Signal {score:.2f} below threshold {self.entry_threshold}", gate_results=gates)
