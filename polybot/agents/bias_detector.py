from __future__ import annotations

import json
import math
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

INDICATOR_NAMES = ["rsi", "macd", "stochastic", "obv", "vwap"]
REGIME_NAMES = ["trending_up", "trending_down", "reverting", "volatile", "quiet", "neutral"]


def _get_gain_pct(o: dict[str, Any]) -> float:
    """Arithmetic return for binary outcomes. Uses stored gain_pct, falls back to price ratio."""
    if "gain_pct" in o:
        return o["gain_pct"]
    entry = o.get("entry_price", 0)
    exit_p = o.get("exit_price", 0)
    if entry > 0:
        return (exit_p - entry) / entry
    return 0.0


def _get_regime(o: dict[str, Any]) -> str:
    """Extract regime label from trade_context."""
    ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
    return ctx.get("regime_state", "neutral")


class BiasDetector:
    def __init__(self, biases_path: str) -> None:
        self.biases_path: Path = Path(biases_path)

    def detect(self, outcomes: list[dict[str, Any]], min_samples: int = 3) -> dict[str, Any]:
        """Produce a rich multi-dimensional analysis of trade outcomes.

        Returns a dict with sections: per_indicator, side_analysis,
        edge_calibration, time_patterns, volatility_patterns, overall,
        by_regime, edge_realization_quartiles, time_weighted.
        Gracefully handles outcomes that lack trade_context.
        """
        if len(outcomes) < min_samples:
            return {"per_indicator": {}, "side_analysis": {}, "edge_calibration": {},
                    "time_patterns": {}, "volatility_patterns": {}, "overall": {},
                    "by_regime": {}, "edge_realization_quartiles": [], "time_weighted": {}}

        return {
            "per_indicator": self._analyze_indicators(outcomes, min_samples),
            "side_analysis": self._analyze_sides(outcomes),
            "edge_calibration": self._analyze_edges(outcomes),
            "time_patterns": self._analyze_time(outcomes),
            "volatility_patterns": self._analyze_volatility(outcomes),
            "overall": self._analyze_overall(outcomes),
            "by_regime": self._analyze_by_regime(outcomes),
            "edge_realization_quartiles": self._analyze_edge_realization(outcomes),
            "time_weighted": self._analyze_time_weighted(outcomes),
        }

    def _analyze_indicators(self, outcomes: list[dict[str, Any]], min_samples: int) -> dict[str, Any]:
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

    def _analyze_sides(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Win rate and avg gain_pct per side (Up vs Down)."""
        sides: dict[str, list] = defaultdict(list)
        for o in outcomes:
            side = o.get("side", "").lower()
            if side in ("up", "down"):
                sides[side].append(o)

        result = {}
        for side, trades in sides.items():
            wins = sum(1 for t in trades if t.get("correct", False))
            returns = [_get_gain_pct(t) for t in trades]
            result[side] = {
                "win_rate": round(wins / len(trades), 4),
                "avg_gain_pct": round(sum(returns) / len(returns), 6),
                "count": len(trades),
            }
        return result

    def _analyze_edges(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Win rate bucketed by edge size at entry."""
        buckets = {"4-8%": [], "8-12%": [], "12-20%": [], "20%+": []}
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            edge = ctx.get("edge", 0)
            if edge <= 0:
                continue
            if edge < 0.08:
                buckets["4-8%"].append(o)
            elif edge < 0.12:
                buckets["8-12%"].append(o)
            elif edge < 0.20:
                buckets["12-20%"].append(o)
            else:
                buckets["20%+"].append(o)

        result = {}
        for label, trades in buckets.items():
            if not trades:
                continue
            wins = sum(1 for t in trades if t.get("correct", False))
            result[label] = {"win_rate": round(wins / len(trades), 4), "count": len(trades)}
        return result

    def _analyze_time(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
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

    def _analyze_volatility(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
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

    def _analyze_by_regime(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Per-regime stats: win rate, avg edge, avg gain, trade count."""
        buckets: dict[str, list] = defaultdict(list)
        for o in outcomes:
            regime = _get_regime(o)
            # Collapse trending_up/trending_down into "trending" for reporting
            if regime.startswith("trending"):
                buckets["trending"].append(o)
            elif regime in ("reverting", "mean_reverting", "volatile", "quiet"):
                buckets[regime].append(o)
            else:
                buckets["neutral"].append(o)

        result = {}
        for regime, trades in buckets.items():
            if len(trades) < 3:
                continue
            wins = sum(1 for t in trades if t.get("correct", False))
            returns = [_get_gain_pct(t) for t in trades]
            edges = [
                t.get("indicator_snapshot", {}).get("trade_context", {}).get("edge", 0)
                for t in trades
            ]
            edges = [e for e in edges if e > 0]
            result[regime] = {
                "n": len(trades),
                "win_rate": round(wins / len(trades), 4),
                "avg_edge": round(sum(edges) / len(edges), 4) if edges else 0,
                "avg_gain_pct": round(sum(returns) / len(returns), 6),
            }
        return result

    def _analyze_edge_realization(self, outcomes: list[dict[str, Any]]) -> list[float]:
        """Edge realization ratio by quartile of predicted edge.

        Returns [Q1_ratio, Q2_ratio, Q3_ratio, Q4_ratio] where ratio = realized/predicted.
        If predicted edge is 8% but realized is 5.7%, ratio is 0.71.
        """
        pairs = []  # (predicted_edge, realized_gain_pct)
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            predicted = ctx.get("edge", 0)
            if predicted > 0:
                realized = _get_gain_pct(o)
                pairs.append((predicted, realized))

        if len(pairs) < 8:
            return []

        pairs.sort(key=lambda x: x[0])
        q_size = len(pairs) // 4
        quartiles = []
        for qi in range(4):
            start = qi * q_size
            end = start + q_size if qi < 3 else len(pairs)
            q_pairs = pairs[start:end]
            avg_predicted = sum(p for p, _ in q_pairs) / len(q_pairs)
            avg_realized = sum(r for _, r in q_pairs) / len(q_pairs)
            ratio = avg_realized / avg_predicted if avg_predicted > 0 else 0
            quartiles.append(round(ratio, 3))
        return quartiles

    def _analyze_time_weighted(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Time-weighted overall stats using exponential decay (14-day half-life)."""
        try:
            from polybot.agents.pipeline_analytics import compute_sample_weights, weighted_win_rate, weighted_sharpe
        except ImportError:
            return {}

        weights = compute_sample_weights(outcomes, half_life_days=14.0)
        return {
            "win_rate": round(weighted_win_rate(outcomes, weights), 4),
            "sharpe": round(weighted_sharpe(outcomes, weights), 4),
        }

    def analyze_counterfactuals(self, counterfactuals: list[dict[str, Any]]) -> dict[str, Any]:
        """Analyze counterfactual outcomes for both scalps and holds.

        Scalp counterfactuals: was the early exit optimal, or would holding
        to resolution have been better?

        Hold counterfactuals: was holding to resolution optimal, or would
        scalping at the worst moment have been better?

        Returns metrics that tell the learning pipeline whether the exit
        threshold is too aggressive (scalping winners) or too loose.
        """
        if not counterfactuals:
            return {}

        # Split by type: scalps have "scalp_was_optimal", holds have "hold_was_optimal"
        scalps = [c for c in counterfactuals if "scalp_was_optimal" in c]
        holds = [c for c in counterfactuals if "hold_was_optimal" in c]

        result = {}

        # --- Scalp analysis ---
        if scalps:
            s_total = len(scalps)
            s_optimal = sum(1 for c in scalps if c.get("scalp_was_optimal", True))
            s_suboptimal = s_total - s_optimal
            s_suboptimal_records = [c for c in scalps if not c.get("scalp_was_optimal", True)]
            s_optimal_records = [c for c in scalps if c.get("scalp_was_optimal", True)]

            missed_gains = [c.get("delta_pnl", 0) for c in s_suboptimal_records]
            avg_missed_pnl = sum(missed_gains) / len(missed_gains) if missed_gains else 0

            missed_pcts = [
                c.get("counterfactual", {}).get("gain_pct", 0) - c.get("actual", {}).get("gain_pct", 0)
                for c in s_suboptimal_records
            ]
            avg_missed_gain_pct = sum(missed_pcts) / len(missed_pcts) if missed_pcts else 0

            def _avg_edge_scalp(records):
                edges = [c.get("context_at_scalp", {}).get("holding_edge", 0) for c in records]
                return round(sum(edges) / len(edges), 4) if edges else 0

            def _avg_secs_scalp(records):
                secs = [c.get("context_at_scalp", {}).get("seconds_remaining", 0) for c in records]
                return round(sum(secs) / len(secs), 1) if secs else 0

            time_buckets = {"0-30s": [], "30-90s": [], "90s+": []}
            for c in scalps:
                secs = c.get("context_at_scalp", {}).get("seconds_remaining", 0)
                if secs <= 30:
                    time_buckets["0-30s"].append(c)
                elif secs <= 90:
                    time_buckets["30-90s"].append(c)
                else:
                    time_buckets["90s+"].append(c)

            time_accuracy = {}
            for label, bucket in time_buckets.items():
                if not bucket:
                    continue
                opt = sum(1 for c in bucket if c.get("scalp_was_optimal", True))
                time_accuracy[label] = {
                    "scalp_accuracy": round(opt / len(bucket), 4),
                    "count": len(bucket),
                }

            result.update({
                "total_scalps_tracked": s_total,
                "scalp_accuracy": round(s_optimal / s_total, 4) if s_total > 0 else 0,
                "optimal_scalps": s_optimal,
                "suboptimal_scalps": s_suboptimal,
                "avg_missed_pnl": round(avg_missed_pnl, 4),
                "avg_missed_gain_pct": round(avg_missed_gain_pct, 4),
                "avg_holding_edge_optimal": _avg_edge_scalp(s_optimal_records),
                "avg_holding_edge_suboptimal": _avg_edge_scalp(s_suboptimal_records),
                "avg_seconds_remaining_optimal": _avg_secs_scalp(s_optimal_records),
                "avg_seconds_remaining_suboptimal": _avg_secs_scalp(s_suboptimal_records),
                "time_accuracy": time_accuracy,
            })

        # --- Hold analysis ---
        if holds:
            h_total = len(holds)
            h_optimal = sum(1 for c in holds if c.get("hold_was_optimal", True))
            h_suboptimal = h_total - h_optimal
            h_suboptimal_records = [c for c in holds if not c.get("hold_was_optimal", True)]

            hold_missed = [abs(c.get("delta_pnl", 0)) for c in h_suboptimal_records]
            avg_hold_cost = sum(hold_missed) / len(hold_missed) if hold_missed else 0

            def _avg_edge_hold(records):
                edges = [c.get("context_at_worst_moment", {}).get("holding_edge", 0) for c in records]
                return round(sum(edges) / len(edges), 4) if edges else 0

            result.update({
                "total_holds_tracked": h_total,
                "hold_accuracy": round(h_optimal / h_total, 4) if h_total > 0 else 0,
                "optimal_holds": h_optimal,
                "suboptimal_holds": h_suboptimal,
                "avg_hold_cost_when_suboptimal": round(avg_hold_cost, 4),
                "avg_worst_edge_optimal_holds": _avg_edge_hold([c for c in holds if c.get("hold_was_optimal", True)]),
                "avg_worst_edge_suboptimal_holds": _avg_edge_hold(h_suboptimal_records),
            })

        return result

    def _analyze_overall(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate statistics across all trades."""
        total = len(outcomes)
        wins = sum(1 for o in outcomes if o.get("correct", False))
        returns = [_get_gain_pct(o) for o in outcomes]

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
            "avg_gain_pct": round(avg_ret, 6),
            "sharpe": sharpe,
        }

    def save(self, analysis: dict[str, Any]) -> None:
        self.biases_path.parent.mkdir(parents=True, exist_ok=True)
        self.biases_path.write_text(json.dumps(analysis, indent=2))
