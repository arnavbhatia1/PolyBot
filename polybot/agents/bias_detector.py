"""BiasDetector: multi-dimensional analysis of trade outcomes for the learning pipeline.

Runs on the 60% training split each night. Produces per-indicator accuracy, side/time/
regime/volatility breakdowns, calibration curve, edge realization, counterfactual analysis,
ghost trade gate analysis, cross-window correlation, and time-to-resolution distributions.
"""
from __future__ import annotations

import json
import math
import logging
from collections import defaultdict
from typing import Any

from polybot.paths import FEED_STALENESS_PATH

logger = logging.getLogger(__name__)

INDICATOR_NAMES = ["rsi", "macd", "stochastic", "obv", "vwap"]
REGIME_NAMES = ["trending_up", "trending_down", "reverting", "volatile", "quiet", "neutral"]


def _load_feed_health() -> dict[str, Any]:
    """Read feed_staleness.json so the analysis card surfaces silent feed
    degradation. Without this, a feed creeping from P50=1s to P95=25s reads
    as fewer trades and the optimizer attributes the distribution shift to
    layer signals (e.g. spot_flow) rather than the upstream feed.
    """
    path = FEED_STALENESS_PATH
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception as e:
        logger.debug(f"feed_staleness.json unreadable: {e}")
        return {}
    feeds: dict[str, dict[str, Any]] = {}
    degraded: list[str] = []
    for entry in payload.get("feeds", []) or []:
        name = entry.get("name")
        if not name:
            continue
        feeds[name] = {
            "n": entry.get("n", 0),
            "n_total": entry.get("n_total"),
            "connected": entry.get("connected"),
            "p50": entry.get("p50"),
            "p95": entry.get("p95"),
            "p99": entry.get("p99"),
            "max": entry.get("max"),
        }
        p95 = entry.get("p95")
        if isinstance(p95, (int, float)) and p95 >= 10.0:
            degraded.append(f"{name}: p95={p95:.1f}s")
    return {
        "updated_at": payload.get("updated_at"),
        "feeds": feeds,
        "degraded_p95_ge_10s": degraded,
    }


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
            "by_entry_phase": self._analyze_by_entry_phase(outcomes),
            "flip_analysis": self._analyze_flips(outcomes),
            "edge_realization_quartiles": self._analyze_edge_realization(outcomes),
            "time_weighted": self._analyze_time_weighted(outcomes),
            "by_sprt_confidence": self._analyze_by_sprt_confidence(outcomes),
            "by_adverse_selection": self._analyze_by_adverse_selection(outcomes),
            "feed_health": _load_feed_health(),
        }

    def _analyze_by_entry_phase(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Win rate / Sharpe by entry phase (early/normal/late). Uses the phase
        the bot tagged at entry time, so the segmentation matches the runtime
        time-multiplier logic exactly."""
        from collections import defaultdict
        buckets: dict[str, list[float]] = defaultdict(list)
        wins: dict[str, int] = defaultdict(int)
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            phase = ctx.get("entry_phase", "unknown")
            buckets[phase].append(float(o.get("gain_pct", 0) or 0))
            if o.get("correct"):
                wins[phase] += 1
        result = {}
        for phase, gains in buckets.items():
            if not gains:
                continue
            n = len(gains)
            avg = sum(gains) / n
            std = (sum((g - avg) ** 2 for g in gains) / n) ** 0.5 if n > 1 else 0
            result[phase] = {
                "n": n,
                "win_rate": round(wins[phase] / n, 4),
                "avg_gain_pct": round(avg, 6),
                "sharpe": round(avg / std, 4) if std > 0 else 0.0,
            }
        return result

    def _analyze_flips(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Compare flip-trade outcomes vs base outcomes. Flip trades re-enter
        opposite side same window after a scalp — a separate population worth
        evaluating because they have a different edge premium and timing."""
        flips, base = [], []
        flip_wins, base_wins = 0, 0
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            gain = float(o.get("gain_pct", 0) or 0)
            won = bool(o.get("correct"))
            if ctx.get("is_flip"):
                flips.append(gain)
                if won:
                    flip_wins += 1
            else:
                base.append(gain)
                if won:
                    base_wins += 1
        def _stats(gains: list[float], wins: int) -> dict[str, float]:
            n = len(gains)
            if n == 0:
                return {"n": 0}
            avg = sum(gains) / n
            std = (sum((g - avg) ** 2 for g in gains) / n) ** 0.5 if n > 1 else 0
            return {
                "n": n,
                "win_rate": round(wins / n, 4),
                "avg_gain_pct": round(avg, 6),
                "sharpe": round(avg / std, 4) if std > 0 else 0.0,
            }
        return {"base": _stats(base, base_wins), "flip": _stats(flips, flip_wins)}

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
        """Per-regime stats: win rate, Sharpe, avg edge, avg gain, trade count.

        Collapses trending_up/trending_down into 'trending',
        reverting/mean_reverting into 'reverting', and everything else into 'neutral'.
        """
        buckets: dict[str, list] = defaultdict(list)
        for o in outcomes:
            regime = _get_regime(o)
            if regime.startswith("trending"):
                buckets["trending"].append(o)
            elif regime in ("reverting", "mean_reverting"):
                buckets["reverting"].append(o)
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
            avg_r = sum(returns) / len(returns)
            var_r = sum((r - avg_r) ** 2 for r in returns) / len(returns)
            std_r = math.sqrt(var_r) if var_r > 0 else 0.0
            sharpe = round(avg_r / std_r, 4) if std_r > 0 else 0.0
            result[regime] = {
                "n": len(trades),
                "win_rate": round(wins / len(trades), 4),
                "sharpe": sharpe,
                "avg_edge": round(sum(edges) / len(edges), 4) if edges else 0,
                "avg_gain_pct": round(avg_r, 6),
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
        """Time-weighted overall stats using the canonical RECENCY_DECAY_PER_DAY
        (0.94/day, ~11-day half-life) shared with the backtest and calibrator."""
        try:
            from polybot.agents.pipeline_analytics import compute_sample_weights, weighted_win_rate, weighted_sharpe
        except ImportError:
            return {}

        weights = compute_sample_weights(outcomes)
        return {
            "win_rate": round(weighted_win_rate(outcomes, weights), 4),
            "sharpe": round(weighted_sharpe(outcomes, weights), 4),
        }

    def _analyze_by_sprt_confidence(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Win rate / Sharpe segmented by SPRT confidence at entry.

        Buckets: low (0-0.33), medium (0.33-0.66), high (0.66+).
        Tells the pipeline whether high-confidence SPRT entries outperform —
        if not, min_confidence threshold may need raising.
        """
        buckets: dict[str, list] = {"low": [], "medium": [], "high": []}
        wins: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            conf = float(ctx.get("sprt_confidence", 0) or 0)
            if conf < 0.33:
                k = "low"
            elif conf < 0.66:
                k = "medium"
            else:
                k = "high"
            buckets[k].append(float(o.get("gain_pct", 0) or 0))
            if o.get("correct"):
                wins[k] += 1
        result = {}
        for k, gains in buckets.items():
            if not gains:
                continue
            n = len(gains)
            avg = sum(gains) / n
            std = (sum((g - avg) ** 2 for g in gains) / n) ** 0.5 if n > 1 else 0
            result[k] = {
                "n": n,
                "win_rate": round(wins[k] / n, 4),
                "avg_gain_pct": round(avg, 6),
                "sharpe": round(avg / std, 4) if std > 0 else 0.0,
            }
        return result

    def _analyze_by_adverse_selection(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Win rate / Sharpe segmented by adverse_selection_30s rate at entry.

        Buckets: low (<0.40), medium (0.40-0.60), high (>0.60).
        Tells the pipeline if the adverse_selection_threshold is well-positioned —
        trades entered at high adverse rates should underperform.
        """
        buckets: dict[str, list] = {"low": [], "medium": [], "high": []}
        wins: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
        for o in outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            rate = float(
                ctx.get("adverse_rate_at_30s",
                        ctx.get("adverse_selection_30s", 0.5)) or 0.5
            )
            if rate < 0.40:
                k = "low"
            elif rate <= 0.60:
                k = "medium"
            else:
                k = "high"
            buckets[k].append(float(o.get("gain_pct", 0) or 0))
            if o.get("correct"):
                wins[k] += 1
        result = {}
        for k, gains in buckets.items():
            if not gains:
                continue
            n = len(gains)
            avg = sum(gains) / n
            std = (sum((g - avg) ** 2 for g in gains) / n) ** 0.5 if n > 1 else 0
            result[k] = {
                "n": n,
                "win_rate": round(wins[k] / n, 4),
                "avg_gain_pct": round(avg, 6),
                "sharpe": round(avg / std, 4) if std > 0 else 0.0,
            }
        return result

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

            # Total P&L: actual scalp vs. counterfactual hold-to-resolution
            actual_scalp_pnl = sum(c.get("actual", {}).get("pnl", 0) for c in scalps)
            cf_hold_pnl = sum(c.get("counterfactual", {}).get("pnl", 0) for c in scalps)
            pnl_gap = cf_hold_pnl - actual_scalp_pnl  # positive = could have made more by holding

            # Holding-edge accuracy buckets: the direct signal for exit_edge_threshold tuning.
            # Bucket scalps by their holding_edge at scalp time, show scalp accuracy per bucket.
            # If scalp accuracy is low at e.g. -0.01 to -0.05, the threshold is too aggressive there.
            edge_buckets = {
                "0 to -0.02":    [],   # very close to threshold — should rarely scalp here
                "-0.02 to -0.05": [],  # threshold zone — key diagnostic bucket
                "-0.05 to -0.10": [],  # moderate edge remaining
                "< -0.10":        [],  # strong signal to exit — scalp should be correct here
            }
            for c in scalps:
                edge = c.get("context_at_scalp", {}).get("holding_edge", 0)
                optimal = c.get("scalp_was_optimal", True)
                if edge >= -0.02:
                    edge_buckets["0 to -0.02"].append(optimal)
                elif edge >= -0.05:
                    edge_buckets["-0.02 to -0.05"].append(optimal)
                elif edge >= -0.10:
                    edge_buckets["-0.05 to -0.10"].append(optimal)
                else:
                    edge_buckets["< -0.10"].append(optimal)

            holding_edge_accuracy = {}
            for label, vals in edge_buckets.items():
                if not vals:
                    continue
                acc = sum(vals) / len(vals)
                holding_edge_accuracy[label] = {
                    "scalp_accuracy": round(acc, 4),
                    "count": len(vals),
                    "signal": (
                        "scalp WRONG here — threshold too aggressive" if acc < 0.45 else
                        "borderline — monitor" if acc < 0.55 else
                        "scalp correct here" if acc >= 0.65 else
                        "roughly neutral"
                    ),
                }

            # Net recommendation: compare scalp_accuracy to a 50% baseline
            net_pnl_direction = "scalp_early" if pnl_gap > 0.5 else ("hold_long" if pnl_gap < -0.5 else "calibrated")
            result.update({
                "total_scalps_tracked": s_total,
                "scalp_accuracy": round(s_optimal / s_total, 4) if s_total > 0 else 0,
                "optimal_scalps": s_optimal,
                "suboptimal_scalps": s_suboptimal,
                "total_actual_scalp_pnl": round(actual_scalp_pnl, 2),
                "total_counterfactual_hold_pnl": round(cf_hold_pnl, 2),
                "pnl_gap_from_early_scalps": round(pnl_gap, 2),
                "net_exit_direction": net_pnl_direction,
                "avg_missed_pnl": round(avg_missed_pnl, 4),
                "avg_missed_gain_pct": round(avg_missed_gain_pct, 4),
                "avg_holding_edge_optimal": _avg_edge_scalp(s_optimal_records),
                "avg_holding_edge_suboptimal": _avg_edge_scalp(s_suboptimal_records),
                "avg_seconds_remaining_optimal": _avg_secs_scalp(s_optimal_records),
                "avg_seconds_remaining_suboptimal": _avg_secs_scalp(s_suboptimal_records),
                "time_accuracy": time_accuracy,
                "holding_edge_accuracy": holding_edge_accuracy,
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

            actual_hold_pnl = sum(c.get("actual", {}).get("pnl", 0) for c in holds)
            cf_scalp_pnl = sum(c.get("counterfactual", {}).get("pnl", 0) for c in holds)
            result.update({
                "total_holds_tracked": h_total,
                "hold_accuracy": round(h_optimal / h_total, 4) if h_total > 0 else 0,
                "optimal_holds": h_optimal,
                "suboptimal_holds": h_suboptimal,
                "total_actual_hold_pnl": round(actual_hold_pnl, 2),
                "total_counterfactual_scalp_pnl": round(cf_scalp_pnl, 2),
                "pnl_gap_from_holding": round(actual_hold_pnl - cf_scalp_pnl, 2),
                "avg_hold_cost_when_suboptimal": round(avg_hold_cost, 4),
                "avg_worst_edge_optimal_holds": _avg_edge_hold([c for c in holds if c.get("hold_was_optimal", True)]),
                "avg_worst_edge_suboptimal_holds": _avg_edge_hold(h_suboptimal_records),
            })

        # Segment analysis: cross-table of (time × edge × regime) → scalp accuracy
        segments = self._analyze_counterfactual_segments(counterfactuals)
        if segments:
            result["segments"] = segments

        return result

    def _analyze_counterfactual_segments(self, counterfactuals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Segment scalp counterfactuals by (time × holding_edge × regime) to surface
        actionable patterns: when is the bot scalping too early, and under what conditions
        should it hold vs exit quickly?

        Each row with N≥5 maps directly to a manual-only lever:
        - scalp_accuracy < 0.45 → exit_edge_threshold too aggressive in this segment
        - scalp_accuracy > 0.80 → scalping correct; loss_cut could be more aggressive
        - Regime + time patterns → timing adjustments (loss_cut_time_s)
        """
        scalps = [c for c in counterfactuals if "scalp_was_optimal" in c]
        if len(scalps) < 10:
            return []

        time_buckets = [("0-30s", 0, 30), ("30-90s", 30, 90), ("90-180s", 90, 180), ("180s+", 180, 9999)]
        edge_buckets = [("near_threshold", -0.08, 0.0), ("moderate", -0.15, -0.08), ("strong", -9.99, -0.15)]
        regimes = ["trending", "trending_up", "trending_down", "reverting", "volatile", "quiet", "neutral", "unknown"]

        def _regime_group(r: str) -> str:
            if r in ("trending", "trending_up", "trending_down"):
                return "trending"
            if r in ("reverting",):
                return "reverting"
            if r in ("volatile",):
                return "volatile"
            return "other"

        segments = []
        for t_label, t_lo, t_hi in time_buckets:
            for e_label, e_lo, e_hi in edge_buckets:
                for rg in ("trending", "reverting", "volatile", "other"):
                    bucket = []
                    for c in scalps:
                        ctx = c.get("context_at_scalp", {})
                        secs = ctx.get("seconds_remaining", 0)
                        edge = ctx.get("holding_edge", 0)
                        regime = _regime_group(ctx.get("regime", "unknown"))
                        if t_lo <= secs < t_hi and e_lo <= edge < e_hi and regime == rg:
                            bucket.append(c)
                    if len(bucket) < 5:
                        continue
                    opt = sum(1 for c in bucket if c.get("scalp_was_optimal", True))
                    acc = opt / len(bucket)
                    pnl_gap = sum(c.get("delta_pnl", 0) for c in bucket) / len(bucket)
                    if acc < 0.45:
                        signal = "scalping_too_early"
                        suggestion = "exit_edge_threshold more negative OR hold longer in this regime/time window"
                    elif acc > 0.80:
                        signal = "scalping_correct"
                        suggestion = "exit timing is well-calibrated here"
                    else:
                        signal = "neutral"
                        suggestion = ""
                    segments.append({
                        "time": t_label, "edge": e_label, "regime": rg,
                        "n": len(bucket), "scalp_accuracy": round(acc, 3),
                        "avg_pnl_delta": round(pnl_gap, 4),
                        "signal": signal, "suggestion": suggestion,
                    })
        return segments

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

    def analyze_ghosts(self, ghost_outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Analyze ghost trade outcomes: which rejected gates were actually profitable?

        Ghost trades are signals that passed model gates but failed a downstream entry
        gate. Their resolution tells us whether each gate is correctly filtering noise
        or blocking profitable trades.
        """
        if not ghost_outcomes:
            return {}

        by_gate: dict[str, list[dict]] = defaultdict(list)
        for g in ghost_outcomes:
            gate = g.get("gate_name", "unknown")
            if g.get("resolved", False):
                by_gate[gate].append(g)

        all_resolved = [g for gs in by_gate.values() for g in gs]
        total = len(all_resolved)
        if total == 0:
            return {}

        total_wins = sum(1 for g in all_resolved if g.get("ghost_correct", False))

        # Gates where LOW win_rate is expected/good — filtering informed flow.
        PROTECTIVE_GATES = {"adverse_rate_at_30s", "adverse_rate_30s",
                            "adverse_selection", "adverse_selection_30s"}

        def _market_price_gain(r: dict[str, Any]) -> float:
            """gain_pct re-derived from market_price_<side> so the bias detector
            shares units with `_ghost_to_outcome`'s backtest accounting."""
            side = (r.get("side") or "").lower()
            ctx = r.get("indicator_snapshot", {}).get("trade_context", {})
            mp = ctx.get("market_price_up", 0) if side == "up" else ctx.get("market_price_down", 0)
            if not mp or mp <= 0 or mp >= 1:
                return float(r.get("ghost_gain_pct", 0))
            return ((1.0 - mp) / mp) if r.get("ghost_correct") else -1.0

        by_gate_result: dict[str, Any] = {}
        for gate, records in by_gate.items():
            wins = sum(1 for r in records if r.get("ghost_correct", False))
            gain_pcts = [_market_price_gain(r) for r in records]
            avg_gain = sum(gain_pcts) / len(gain_pcts) if gain_pcts else 0.0
            wr = wins / len(records) if records else 0.0
            simulated_pnl = round(sum(
                r.get("indicator_snapshot", {}).get("trade_context", {}).get("size", 1.0)
                * _market_price_gain(r)
                for r in records
            ), 2)
            if gate in PROTECTIVE_GATES:
                if wr < 0.40:
                    interp = f"CORRECTLY filtering informed-flow losers ({wr:.0%} win) — DO NOT loosen"
                elif wr < 0.55:
                    interp = f"filtering mostly losers ({wr:.0%} win) — keep current threshold"
                else:
                    interp = f"WARNING: adverse-selection gate showing {wr:.0%} winners — investigate"
            else:
                if wr > 0.60:
                    interp = "blocking mostly winners — consider loosening"
                elif wr > 0.50:
                    interp = "blocking slight majority of winners — borderline, monitor"
                else:
                    interp = "correctly filtering — keep"
            by_gate_result[gate] = {
                "count": len(records),
                "pct_profitable": round(wr, 4),
                "avg_gain_pct": round(avg_gain, 4),
                "simulated_pnl": simulated_pnl,
                "interpretation": interp,
            }

        return {
            "total_ghosts": total,
            "pct_profitable": round(total_wins / total, 4) if total > 0 else 0.0,
            "by_gate": by_gate_result,
        }

    def analyze_execution_quality_detailed(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Slippage breakdown by spread bucket and time-in-window.

        Returns actionable data for max_edge, logit_scale, and kelly_fraction tuning.
        Slippage is always a cost — its distribution reveals WHERE execution quality degrades.
        """
        spread_buckets: dict[str, list[float]] = {
            "tight (<3%)":    [],
            "medium (3-7%)":  [],
            "wide (>7%)":     [],
        }
        time_buckets: dict[str, list[float]] = {
            "early (>180s)": [],
            "mid (60-180s)": [],
            "late (0-60s)":  [],
        }
        all_returns: list[float] = []
        all_slippages: list[float] = []

        for o in outcomes:
            slip = o.get("fill_slippage")
            if slip is None:
                continue
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            up_p = ctx.get("market_price_up", 0)
            down_p = ctx.get("market_price_down", 0)
            secs = ctx.get("seconds_remaining", 0)

            if up_p > 0 and down_p > 0:
                spread = max(0.0, 1.0 - up_p - down_p)
                if spread < 0.03:
                    spread_buckets["tight (<3%)"].append(slip)
                elif spread < 0.07:
                    spread_buckets["medium (3-7%)"].append(slip)
                else:
                    spread_buckets["wide (>7%)"].append(slip)

            if secs > 180:
                time_buckets["early (>180s)"].append(slip)
            elif secs > 60:
                time_buckets["mid (60-180s)"].append(slip)
            else:
                time_buckets["late (0-60s)"].append(slip)

            all_returns.append(o.get("gain_pct", 0))
            all_slippages.append(slip)

        def _avg(lst: list[float]) -> float | None:
            return round(sum(lst) / len(lst), 4) if lst else None

        slippage_by_spread = {
            k: {"avg_slippage": _avg(v), "count": len(v)}
            for k, v in spread_buckets.items() if v
        }
        slippage_by_time = {
            k: {"avg_slippage": _avg(v), "count": len(v)}
            for k, v in time_buckets.items() if v
        }

        # Sharpe impact: E[slippage] / std(returns) gives first-order Sharpe hit
        sharpe_impact = None
        if all_returns and all_slippages:
            avg_r = sum(all_returns) / len(all_returns)
            var_r = sum((r - avg_r) ** 2 for r in all_returns) / len(all_returns)
            std_r = math.sqrt(var_r) if var_r > 0 else 0.0
            avg_slip = sum(all_slippages) / len(all_slippages)
            if std_r > 0:
                sharpe_impact = round(avg_slip / std_r, 3)

        return {
            "slippage_by_spread": slippage_by_spread,
            "slippage_by_time": slippage_by_time,
            "sharpe_impact_from_slippage": sharpe_impact,
        }
