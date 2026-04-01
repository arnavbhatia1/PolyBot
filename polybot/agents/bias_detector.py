import json
import math
import logging
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

INDICATOR_NAMES = ["rsi", "macd", "stochastic", "obv", "vwap"]


class BiasDetector:
    def __init__(self, biases_path: str):
        self.biases_path = Path(biases_path)

    def detect(self, outcomes: list[dict], min_samples: int = 3) -> dict:
        """Produce a rich multi-dimensional analysis of trade outcomes.

        Returns a dict with sections: per_indicator, side_analysis,
        edge_calibration, time_patterns, volatility_patterns, overall.
        Gracefully handles outcomes that lack trade_context.
        """
        if len(outcomes) < min_samples:
            return {"per_indicator": {}, "side_analysis": {}, "edge_calibration": {},
                    "time_patterns": {}, "volatility_patterns": {}, "overall": {}}

        return {
            "per_indicator": self._analyze_indicators(outcomes, min_samples),
            "side_analysis": self._analyze_sides(outcomes),
            "edge_calibration": self._analyze_edges(outcomes),
            "time_patterns": self._analyze_time(outcomes),
            "volatility_patterns": self._analyze_volatility(outcomes),
            "overall": self._analyze_overall(outcomes),
        }

    def _analyze_indicators(self, outcomes: list[dict], min_samples: int) -> dict:
        """Per-indicator accuracy with bullish/bearish breakdown."""
        result = {}
        for ind in INDICATOR_NAMES:
            bullish_wins, bullish_total = 0, 0
            bearish_wins, bearish_total = 0, 0

            for o in outcomes:
                snap = o.get("indicator_snapshot", {})
                score = snap.get(ind, {}).get("score", 0)
                correct = o.get("correct", False)

                if score > 0.1:
                    bullish_total += 1
                    if correct:
                        bullish_wins += 1
                elif score < -0.1:
                    bearish_total += 1
                    if correct:
                        bearish_wins += 1

            total = bullish_total + bearish_total
            if total < min_samples:
                continue

            wins = bullish_wins + bearish_wins
            result[ind] = {
                "accuracy": round(wins / total, 4) if total > 0 else 0.5,
                "bullish_accuracy": round(bullish_wins / bullish_total, 4) if bullish_total > 0 else 0.5,
                "bearish_accuracy": round(bearish_wins / bearish_total, 4) if bearish_total > 0 else 0.5,
                "sample_size": total,
            }
        return result

    def _analyze_sides(self, outcomes: list[dict]) -> dict:
        """Win rate and avg log_return per side (Up vs Down)."""
        sides: dict[str, list] = defaultdict(list)
        for o in outcomes:
            side = o.get("side", "").lower()
            if side in ("up", "down"):
                sides[side].append(o)

        result = {}
        for side, trades in sides.items():
            wins = sum(1 for t in trades if t.get("correct", False))
            returns = [t.get("log_return", 0) for t in trades]
            result[side] = {
                "win_rate": round(wins / len(trades), 4),
                "avg_log_return": round(sum(returns) / len(returns), 6),
                "count": len(trades),
            }
        return result

    def _analyze_edges(self, outcomes: list[dict]) -> dict:
        """Win rate bucketed by edge size at entry."""
        buckets = {"10-20%": [], "20-35%": [], "35%+": []}
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            edge = ctx.get("edge", 0)
            if edge <= 0:
                continue
            if edge < 0.20:
                buckets["10-20%"].append(o)
            elif edge < 0.35:
                buckets["20-35%"].append(o)
            else:
                buckets["35%+"].append(o)

        result = {}
        for label, trades in buckets.items():
            if not trades:
                continue
            wins = sum(1 for t in trades if t.get("correct", False))
            result[label] = {"win_rate": round(wins / len(trades), 4), "count": len(trades)}
        return result

    def _analyze_time(self, outcomes: list[dict]) -> dict:
        """Win rate by seconds remaining at entry."""
        buckets = {"0-60s": [], "60-180s": [], "180-300s": []}
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            secs = ctx.get("seconds_remaining", 0)
            if secs <= 0:
                continue
            if secs <= 60:
                buckets["0-60s"].append(o)
            elif secs <= 180:
                buckets["60-180s"].append(o)
            else:
                buckets["180-300s"].append(o)

        result = {}
        for label, trades in buckets.items():
            if not trades:
                continue
            wins = sum(1 for t in trades if t.get("correct", False))
            result[label] = {"win_rate": round(wins / len(trades), 4), "count": len(trades)}
        return result

    def _analyze_volatility(self, outcomes: list[dict]) -> dict:
        """Win rate by ATR regime (low/mid/high via percentiles)."""
        atrs = []
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            atr = ctx.get("atr", 0)
            if atr > 0:
                atrs.append((atr, o))

        if len(atrs) < 3:
            return {}

        atr_values = sorted(a for a, _ in atrs)
        p33 = atr_values[len(atr_values) // 3]
        p66 = atr_values[2 * len(atr_values) // 3]

        buckets: dict[str, list] = {"low_atr": [], "mid_atr": [], "high_atr": []}
        for atr_val, o in atrs:
            if atr_val <= p33:
                buckets["low_atr"].append(o)
            elif atr_val <= p66:
                buckets["mid_atr"].append(o)
            else:
                buckets["high_atr"].append(o)

        result = {}
        for label, trades in buckets.items():
            if not trades:
                continue
            wins = sum(1 for t in trades if t.get("correct", False))
            result[label] = {"win_rate": round(wins / len(trades), 4), "count": len(trades)}
        return result

    def _analyze_overall(self, outcomes: list[dict]) -> dict:
        """Aggregate statistics across all trades."""
        total = len(outcomes)
        wins = sum(1 for o in outcomes if o.get("correct", False))
        returns = [o.get("log_return", 0) for o in outcomes]

        edges = []
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            edge = ctx.get("edge", 0)
            if edge > 0:
                edges.append(edge)

        avg_ret = sum(returns) / len(returns) if returns else 0
        var = sum((r - avg_ret) ** 2 for r in returns) / len(returns) if len(returns) > 1 else 1
        std = math.sqrt(var) if var > 0 else 1
        sharpe = round(avg_ret / std, 4) if std > 0 else 0

        return {
            "total_trades": total,
            "win_rate": round(wins / total, 4) if total > 0 else 0,
            "avg_edge": round(sum(edges) / len(edges), 4) if edges else 0,
            "avg_log_return": round(avg_ret, 6),
            "sharpe": sharpe,
        }

    def save(self, analysis: dict):
        self.biases_path.parent.mkdir(parents=True, exist_ok=True)
        self.biases_path.write_text(json.dumps(analysis, indent=2))
