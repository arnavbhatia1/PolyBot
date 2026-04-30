"""TAEvolver: sends a distilled strategy analysis card to Claude and returns weight recommendations.

Builds a context including BiasDetector output, gate skip stats, realized edge, ghost
trade analysis, and 100 stratified trades (50 recent + 50 spaced). Falls back to
the deep local recommender (`LocalRecommender`) when Claude is unavailable — same
output shape, same guardrails, no LLM call.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.agents.local_recommender import LocalRecommender

logger = logging.getLogger(__name__)


class TAEvolver:
    def __init__(self, strategy_log_path: str, claude_client: Any = None) -> None:
        self.strategy_log_path: Path = Path(strategy_log_path)
        self.claude_client: Any = claude_client

    # --- Primary entry point (async, uses Claude) ---

    async def evolve(self, outcomes: list[dict[str, Any]], analysis: dict[str, Any],
                     current_config: dict[str, Any]) -> dict[str, Any]:
        """Compile data, call Claude for strategy analysis, fall back to local on failure.

        Returns a recommendations dict with `changes` (a list of {param, value, reason}),
        plus key_findings, risk_warnings, reasoning, confidence.
        """
        if not outcomes:
            return {}

        # Read recent strategy log entries for context.
        # Rotate if the file exceeds 40 KB — keep the last 30 KB so Claude
        # always receives recent entries rather than a random mid-entry slice.
        prev = ""
        if self.strategy_log_path.exists():
            text = self.strategy_log_path.read_text(encoding="utf-8")
            if len(text) > 40_000:
                trimmed = text[-30_000:]
                # Align to a line boundary so we don't start mid-entry
                newline_pos = trimmed.find("\n")
                trimmed = trimmed[newline_pos + 1:] if newline_pos != -1 else trimmed
                self.strategy_log_path.write_text(trimmed, encoding="utf-8")
                text = trimmed
            prev = text[-15000:] if len(text) > 15000 else text

        context = {
            "current_config": current_config,
            "analysis": analysis,
            "trades": outcomes,
            "previous_recommendations": prev,
        }

        # Try Claude first
        if self.claude_client:
            try:
                recommendations = await self.claude_client.analyze_strategy(context)
                self._save_claude_log(recommendations)
                logger.info(f"Claude strategy analysis complete (confidence: {recommendations.get('confidence', '?')})")
                return recommendations
            except Exception as e:
                logger.warning(f"Claude strategy analysis failed, falling back to local: {e}")

        # Fallback: deep local recommender — mirrors Claude's reasoning over the
        # same analysis dict (directional table, cumulative failures, noise floor,
        # adoption-floor sizing, family diversity). No LLM call.
        recs = LocalRecommender(analysis, current_config).recommend()
        self._save_local_log(self.analyze(outcomes), recs)
        return recs

    # --- Local helper methods (sync, no API) ---

    def analyze(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        if not outcomes:
            return {"win_rate": 0, "avg_gain_pct": 0, "total_trades": 0}
        wins = sum(1 for o in outcomes if o.get("correct", False))
        returns = []
        for o in outcomes:
            if "gain_pct" in o:
                returns.append(o["gain_pct"])
            elif o.get("entry_price", 0) > 0:
                returns.append((o.get("exit_price", 0) - o["entry_price"]) / o["entry_price"])
            else:
                returns.append(0)
        return {"win_rate": wins / len(outcomes),
                "avg_gain_pct": sum(returns) / len(returns),
                "total_trades": len(outcomes)}

    def recommend_weight_adjustments(self, outcomes: list[dict[str, Any]], current_weights: dict[str, float]) -> dict[str, float]:
        if len(outcomes) < 5:
            return current_weights.copy()
        indicator_names = ["rsi", "macd", "stochastic", "obv", "vwap"]
        win_scores = {name: [] for name in indicator_names}
        lose_scores = {name: [] for name in indicator_names}
        for o in outcomes:
            snap = o.get("indicator_snapshot", {})
            for name in indicator_names:
                score = snap.get(name, {}).get("score", 0)
                if o.get("correct"):
                    win_scores[name].append(abs(score))
                else:
                    lose_scores[name].append(abs(score))
        new_weights = {}
        for name in indicator_names:
            avg_win = sum(win_scores[name]) / len(win_scores[name]) if win_scores[name] else 0
            avg_lose = sum(lose_scores[name]) / len(lose_scores[name]) if lose_scores[name] else 0
            effectiveness = avg_win - avg_lose
            new_weights[name] = max(0.05, current_weights.get(name, 0.20) + effectiveness * 0.05)
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}
        return new_weights

    # --- Logging ---

    def _save_claude_log(self, recommendations: dict[str, Any]) -> None:
        """Append Claude's analysis to strategy_log.md (compact — kept under ~60 lines)."""
        self.strategy_log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        confidence = recommendations.get("confidence", "?")
        reasoning = recommendations.get("reasoning", "")
        findings = recommendations.get("key_findings", [])
        warnings = recommendations.get("risk_warnings", [])

        findings_str = "\n".join(f"- {f}" for f in findings) if findings else "- None"
        warnings_str = "\n".join(f"- {w}" for w in warnings) if warnings else "- None"

        changes_list = recommendations.get("changes", [])
        if changes_list:
            changes_str = "\n".join(
                f"  - {c.get('param', '?')}={c.get('value', '?')} ({c.get('reason', '')})"
                for c in changes_list
            )
        else:
            changes_str = "  - none"

        # Manual-lever observations (operator-only, never auto-applied)
        # Format matches the pipeline summary table: one param per block, two lines each.
        manual_obs = recommendations.get("manual_observations", []) or []
        if manual_obs:
            obs_lines = []
            for ob in manual_obs:
                obs_lines.append(
                    f"  - {ob.get('param', '?')}: {ob.get('current', '?')} -> "
                    f"{ob.get('suggested', '?')} [{ob.get('confidence', '?')}]"
                )
                reason = (ob.get("reason") or "").strip()
                if reason:
                    obs_lines.append(f"    {reason}")
            obs_str = "\n".join(obs_lines)
        else:
            obs_str = "  - none"

        entry = (
            f"\n## {now}\n\n"
            f"**Source:** Claude ({confidence})\n"
            f"**Proposed Changes ({len(changes_list)}):**\n{changes_str}\n\n"
            f"**Manual Suggestions ({len(manual_obs)}) [operator-only]:**\n{obs_str}\n\n"
            f"**Findings:**\n{findings_str}\n\n"
            f"**Warnings:**\n{warnings_str}\n\n"
            f"**Reasoning:** {reasoning}\n"
        )

        existing = self.strategy_log_path.read_text(encoding="utf-8") if self.strategy_log_path.exists() else "# Strategy Evolution Log\n"
        self.strategy_log_path.write_text(existing + entry, encoding="utf-8")

    def _save_local_log(self, analysis: dict[str, Any], recs: dict[str, Any] | None = None) -> None:
        """Append local fallback analysis to strategy_log.md."""
        self.strategy_log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        findings = recs.get("key_findings", []) if recs else []
        warnings = recs.get("risk_warnings", []) if recs else []
        changes = recs.get("changes", []) if recs else []
        findings_str = "\n".join(f"- {f}" for f in findings) if findings else "- None"
        warnings_str = "\n".join(f"- {w}" for w in warnings) if warnings else "- None"
        changes_str = "\n".join(f"  - {c.get('param')}={c.get('value')} ({c.get('reason', '')})"
                                for c in changes) if changes else "  - none"
        entry = (
            f"\n## {now}\n\n"
            f"**Source:** Local fallback (Claude unavailable)\n\n"
            f"**Analysis:** {analysis}\n\n"
            f"**Key Findings:**\n{findings_str}\n\n"
            f"**Risk Warnings:**\n{warnings_str}\n\n"
            f"**Proposed Changes ({len(changes)}):**\n{changes_str}\n"
        )
        existing = self.strategy_log_path.read_text(encoding="utf-8") if self.strategy_log_path.exists() else "# Strategy Evolution Log\n"
        self.strategy_log_path.write_text(existing + entry, encoding="utf-8")
