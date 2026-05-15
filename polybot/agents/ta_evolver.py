"""Sends the analysis card to Claude (or LocalRecommender on failure) and
returns the recommendations dict that the pipeline backtests + adopts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.agents.local_recommender import LocalRecommender
from polybot.agents.claude_recommender import ClaudeRecommender

logger = logging.getLogger(__name__)


class TAEvolver:
    def __init__(self, strategy_log_path: str, claude_client: Any = None) -> None:
        self.strategy_log_path: Path = Path(strategy_log_path)
        self.claude_client: Any = claude_client

    async def evolve(self, outcomes: list[dict[str, Any]], analysis: dict[str, Any],
                     current_config: dict[str, Any]) -> dict[str, Any]:
        if not outcomes:
            return {}

        # Strategy log: rotate at 40 KB, keep last 30 KB aligned to a line boundary.
        prev = ""
        if self.strategy_log_path.exists():
            text = self.strategy_log_path.read_text(encoding="utf-8")
            if len(text) > 40_000:
                trimmed = text[-30_000:]
                newline_pos = trimmed.find("\n")
                trimmed = trimmed[newline_pos + 1:] if newline_pos != -1 else trimmed
                self.strategy_log_path.write_text(trimmed, encoding="utf-8")
                text = trimmed
            prev = text[-15000:] if len(text) > 15000 else text

        # Both recommenders share the same BaseRecommender — same exploratory
        # probe, same dedupe, same envelope schema. Only difference is whether
        # reactive candidates come from Claude (API) or local rules.
        if self.claude_client:
            try:
                recommender = ClaudeRecommender(
                    analysis, current_config, self.claude_client,
                    trades=outcomes, previous_recommendations=prev,
                )
                recommendations = await recommender.recommend()
                recommendations["_pipeline_source"] = "claude"
                self._save_log(recommendations, source=f"Claude ({recommendations.get('confidence', '?')})")
                logger.info(
                    f"  [3/4] Claude done  |  confidence: {recommendations.get('confidence', '?')}  |  "
                    f"{len(recommendations.get('changes', []))} changes proposed"
                )
                return recommendations
            except Exception as e:
                logger.warning(f"Claude unavailable, using local recommender: {e}")

        recs = LocalRecommender(analysis, current_config).recommend()
        recs["_pipeline_source"] = "local"
        self._save_log(recs, source="Local")
        return recs

    def _save_log(self, recs: dict[str, Any], source: str) -> None:
        """Append a compact entry to strategy_log.md."""
        self.strategy_log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()

        changes = recs.get("changes", []) or []
        changes_str = "\n".join(
            f"  - {c.get('param', '?')}={c.get('value', '?')} ({c.get('reason', '')})"
            for c in changes
        ) if changes else "  - none"

        manual_obs = recs.get("manual_observations", []) or []
        if manual_obs:
            lines = []
            for ob in manual_obs:
                lines.append(
                    f"  - {ob.get('param', '?')}: {ob.get('current', '?')} -> "
                    f"{ob.get('suggested', '?')} [{ob.get('confidence', '?')}]"
                )
                reason = (ob.get("reason") or "").strip()
                if reason:
                    lines.append(f"    {reason}")
            obs_str = "\n".join(lines)
        else:
            obs_str = "  - none"

        findings = recs.get("key_findings", []) or []
        warnings = recs.get("risk_warnings", []) or []
        findings_str = "\n".join(f"- {f}" for f in findings) if findings else "- None"
        warnings_str = "\n".join(f"- {w}" for w in warnings) if warnings else "- None"

        entry = (
            f"\n## {now}\n\n"
            f"**Source:** {source}\n"
            f"**Proposed Changes ({len(changes)}):**\n{changes_str}\n\n"
            f"**Manual Suggestions ({len(manual_obs)}) [operator-only]:**\n{obs_str}\n\n"
            f"**Findings:**\n{findings_str}\n\n"
            f"**Warnings:**\n{warnings_str}\n\n"
            f"**Reasoning:** {recs.get('reasoning', '')}\n"
        )
        existing = self.strategy_log_path.read_text(encoding="utf-8") if self.strategy_log_path.exists() else "# Strategy Evolution Log\n"
        self.strategy_log_path.write_text(existing + entry, encoding="utf-8")
