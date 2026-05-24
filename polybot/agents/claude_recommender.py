"""ClaudeRecommender — wraps the Claude API as a BaseRecommender subclass.

Same exploratory probe, dedupe, and envelope as LocalRecommender. The only
difference is that reactive candidates come from a Claude API call (via
``ClaudeClient.analyze_strategy``) instead of deterministic rules.
"""
from __future__ import annotations

import logging
from typing import Any

from polybot.agents.recommender_base import BaseRecommender

logger = logging.getLogger(__name__)


class ClaudeRecommender(BaseRecommender):
    SOURCE_NAME = "Claude"

    def __init__(self, analysis: dict[str, Any], current_config: dict[str, Any],
                 claude_client: Any, trades: list[dict[str, Any]] | None = None,
                 previous_recommendations: str = "") -> None:
        super().__init__(analysis, current_config)
        self.claude_client = claude_client
        self.trades = trades or []
        self.previous_recommendations = previous_recommendations or ""

    async def recommend(self) -> dict[str, Any]:
        n = int(self.analysis.get("overall", {}).get("total_trades", 0) or 0)
        if n < 50:
            return self._insufficient(n)

        context = {
            "current_config": self.cfg,
            "analysis": self.analysis,
            "trades": self.trades,
            "previous_recommendations": self.previous_recommendations,
        }
        try:
            resp = await self.claude_client.analyze_strategy(context)
        except Exception as e:
            logger.debug(f"Claude API failed: {e}")
            raise  # caller falls back to LocalRecommender

        # Merge Claude's validated changes into self.proposals.
        for c in (resp.get("changes") or []):
            if not isinstance(c, dict) or not c.get("param"):
                continue
            self.proposals.append({
                "param": c["param"],
                "value": c.get("value"),
                "reason": c.get("reason", "Claude proposal"),
                "predicted_delta_sharpe_7d": round(
                    float(c.get("predicted_delta_sharpe_7d", 0.015) or 0.015), 4
                ),
                "confidence_interval": c.get("confidence_interval", [-0.01, 0.05]),
            })

        # Carry Claude's manual_obs and warnings through.
        for ob in (resp.get("manual_observations") or []):
            if isinstance(ob, dict):
                self.manual_obs.append(ob)
        for w in (resp.get("risk_warnings") or []):
            if isinstance(w, str):
                self.warnings.append(w)

        # Always-on exploratory probe — same as LocalRecommender.
        self._rule_exploratory()

        return self._finalize(
            reasoning=str(resp.get("reasoning", "") or ""),
            confidence=str(resp.get("confidence", "medium") or "medium"),
        )
