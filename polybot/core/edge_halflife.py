"""Edge half-life tracker: detects strategy-level edge decay over weeks/months.

Compares rolling realized edge (actual win rate - market price) across time windows.
If realized edge in the last 7 days < last 30 days, the strategy is being arbitraged.

edge(t) = edge_0 * exp(-lambda * t)
half_life = ln(2) / lambda

Thresholds:
- half_life > 90 days: healthy
- half_life 30-90 days: monitor closely
- half_life 14-30 days: reduce position sizes 50%
- half_life < 14 days: defensive mode (paper only recommended)
"""
from __future__ import annotations

import json
import math
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


class EdgeHalfLifeTracker:
    """Measures rolling edge decay rate from outcome history."""

    def __init__(self, outcomes_dir: str = "polybot/memory/outcomes") -> None:
        self.outcomes_dir = Path(outcomes_dir)

    def compute(self) -> dict:
        """Analyze all outcomes and compute edge decay metrics.

        Returns dict with:
            recent_7d_edge: avg realized edge in last 7 days
            prior_30d_edge: avg realized edge in days 8-30
            decay_rate: lambda in edge(t) = edge_0 * exp(-lambda * t)
            half_life_days: ln(2) / lambda (infinity if no decay)
            regime: "healthy" | "monitor" | "reduce" | "defensive"
            kelly_discount: multiplier on Kelly (1.0 for healthy, 0.5 for reduce)
        """
        outcomes = self._load_outcomes()
        if len(outcomes) < 30:
            return {
                "recent_7d_edge": 0.0, "prior_30d_edge": 0.0,
                "decay_rate": 0.0, "half_life_days": float("inf"),
                "regime": "insufficient_data", "kelly_discount": 1.0,
            }

        now = datetime.now(timezone.utc)
        day_7 = now - timedelta(days=7)
        day_30 = now - timedelta(days=30)

        recent = [o for o in outcomes if o["ts"] >= day_7]
        prior = [o for o in outcomes if day_30 <= o["ts"] < day_7]

        recent_edge = self._avg_realized_edge(recent) if len(recent) >= 10 else 0.0
        prior_edge = self._avg_realized_edge(prior) if len(prior) >= 10 else 0.0

        # Compute decay rate
        if prior_edge <= 0 or recent_edge <= 0:
            decay_rate = 0.0
            half_life = float("inf")
        else:
            # lambda = -ln(recent/prior) / delta_t
            ratio = recent_edge / prior_edge
            if ratio >= 1.0:
                decay_rate = 0.0  # edge growing, not decaying
                half_life = float("inf")
            else:
                delta_t = 14.0  # midpoint difference in days
                decay_rate = -math.log(ratio) / delta_t
                half_life = math.log(2) / decay_rate if decay_rate > 0 else float("inf")

        # Classify regime
        if half_life > 90:
            regime, discount = "healthy", 1.0
        elif half_life > 30:
            regime, discount = "monitor", 0.85
        elif half_life > 14:
            regime, discount = "reduce", 0.50
        else:
            regime, discount = "defensive", 0.25

        result = {
            "recent_7d_edge": round(recent_edge, 4),
            "prior_30d_edge": round(prior_edge, 4),
            "decay_rate": round(decay_rate, 6),
            "half_life_days": round(half_life, 1) if half_life != float("inf") else float("inf"),
            "regime": regime,
            "kelly_discount": discount,
            "recent_n": len(recent),
            "prior_n": len(prior),
        }

        if regime != "healthy":
            logger.warning(f"EDGE DECAY: {regime} — half_life={half_life:.0f}d, "
                          f"recent={recent_edge:.1%} vs prior={prior_edge:.1%}")

        return result

    def _load_outcomes(self) -> list[dict]:
        """Load all outcomes with timestamps and edge data."""
        results = []
        if not self.outcomes_dir.exists():
            return results
        for f in self.outcomes_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                ts_str = data.get("timestamp", "")
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                ctx = data.get("indicator_snapshot", {}).get("trade_context", {})
                edge = ctx.get("edge", 0)
                correct = data.get("correct", False)
                results.append({"ts": ts, "edge": edge, "correct": correct})
            except (json.JSONDecodeError, ValueError, KeyError):
                continue
        return sorted(results, key=lambda x: x["ts"])

    @staticmethod
    def _avg_realized_edge(outcomes: list[dict]) -> float:
        """Average realized edge: (win_rate - 0.5) as a proxy for true edge."""
        if not outcomes:
            return 0.0
        wins = sum(1 for o in outcomes if o["correct"])
        return (wins / len(outcomes)) - 0.5
