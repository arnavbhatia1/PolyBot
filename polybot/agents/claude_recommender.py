"""ClaudeRecommender: wraps the Claude API call as a BaseRecommender subclass.

Same output schema and same always-on exploratory probe as LocalRecommender —
the ONLY difference is that the reactive candidates come from a Claude API
call (driven by ``ClaudeClient.analyze_strategy()``) instead of deterministic
rules. The base class handles everything else: directional table parsing,
exploratory probe, dedupe, family limits, clamping, envelope finalization.

Flow:
  1. Build context (current_config, analysis, trades, previous_recommendations)
  2. Call ClaudeClient — returns validated ``{changes, manual_observations,
     key_findings, risk_warnings, reasoning, confidence}``
  3. Merge Claude's ``changes`` into ``self.proposals``
  4. Run the inherited ``_rule_exploratory()`` so the pipeline always has
     candidates to test, even if Claude proposed nothing
  5. Dedupe + cap via the shared ``_finalize()`` — Claude's higher-conviction
     proposals override exploratory probes per-param; exploratory fills slots
  6. Preserve Claude's manual_observations, key_findings, risk_warnings,
     reasoning, confidence in the final envelope

If Claude's API call fails the caller (TAEvolver) catches the exception and
falls back to LocalRecommender — same shared base, identical exploration
cadence, no behavioural drift between modes.
"""
from __future__ import annotations

import logging
from typing import Any

from polybot.agents.recommender_base import BaseRecommender, _family_of

logger = logging.getLogger(__name__)


class ClaudeRecommender(BaseRecommender):
    SOURCE_NAME = "Claude"

    def __init__(self, analysis: dict[str, Any], current_config: dict[str, Any],
                 claude_client: Any,
                 trades: list[dict[str, Any]] | None = None,
                 previous_recommendations: str = "") -> None:
        super().__init__(analysis, current_config)
        self.claude_client = claude_client
        self.trades = trades or []
        self.previous_recommendations = previous_recommendations or ""
        # Captured from Claude's response, surfaced through the envelope.
        self._claude_reasoning: str = ""
        self._claude_confidence: str = "medium"

    async def recommend(self) -> dict[str, Any]:
        overall = self.analysis.get("overall", {})
        n = int(overall.get("total_trades", 0) or 0)
        if n < 50:
            self.warnings.append(f"Only {n} trades — insufficient data, no changes applied")
            return self._envelope(confidence="low", reasoning="Insufficient data (N<50).")

        # 1. Reactive candidates from Claude. The shared validator inside
        # ClaudeClient already clamps to CLAMP_RANGES and reroutes manual-only
        # params to manual_observations, so we can trust the structure.
        context = {
            "current_config": self.cfg,
            "analysis": self.analysis,
            "trades": self.trades,
            "previous_recommendations": self.previous_recommendations,
        }
        try:
            claude_response = await self.claude_client.analyze_strategy(context)
        except Exception as e:
            # Let the caller decide whether to fall back to LocalRecommender —
            # we don't silently swallow API failures here.
            logger.warning(f"Claude API failed inside recommender: {e}")
            raise

        # 2. Merge Claude's changes into self.proposals (validation already done).
        for change in (claude_response.get("changes") or []):
            if not isinstance(change, dict):
                continue
            param = change.get("param")
            if not param:
                continue
            # Skip blocked params (same rule that applies to Local + exploratory).
            if param in self._blocked_params:
                continue
            self.proposals.append({
                "param": param,
                "value": change.get("value"),
                "reason": change.get("reason", "Claude proposal"),
                "predicted_delta_sharpe_7d": round(
                    float(change.get("predicted_delta_sharpe_7d", 0.015) or 0.015), 4
                ),
                "confidence_interval": change.get("confidence_interval", [-0.01, 0.05]),
            })
            family = _family_of(param)
            if family:
                self._families_used.add(family)

        # 3. Carry Claude's manual_observations, key_findings, risk_warnings through.
        for ob in (claude_response.get("manual_observations") or []):
            if isinstance(ob, dict):
                self.manual_obs.append(ob)
        for f in (claude_response.get("key_findings") or []):
            if isinstance(f, str):
                self.findings.append(f)
        for w in (claude_response.get("risk_warnings") or []):
            if isinstance(w, str):
                self.warnings.append(w)
        self._claude_reasoning = str(claude_response.get("reasoning", "") or "")
        self._claude_confidence = str(claude_response.get("confidence", "medium") or "medium")

        # 4. Always-on exploratory probe — identical to LocalRecommender. Ensures
        # the pipeline never goes silent on a good-Sharpe day even when Claude
        # proposed zero changes.
        self._rule_exploratory()

        # 5. Dedupe + cap; Claude's higher-conviction proposals win per param.
        return self._finalize()

    # ------------------------------------------------------------------ #
    #  Override reasoning/confidence to surface Claude's own narrative   #
    #  (the operator wants to see what Claude said, not generic text).   #
    # ------------------------------------------------------------------ #

    def _compose_reasoning(self, changes: list[dict[str, Any]]) -> str:
        if self._claude_reasoning:
            n_explore = sum(1 for c in changes if "exploratory" in str(c.get("reason", "")))
            n_claude = len(changes) - n_explore
            tag = (f" [{n_claude} from Claude, {n_explore} exploratory]"
                   if n_explore and n_claude else
                   " [all exploratory]" if n_explore else "")
            return self._claude_reasoning + tag
        return super()._compose_reasoning(changes)

    def _confidence_label(self, changes: list[dict[str, Any]]) -> str:
        # Trust Claude's confidence assessment when available.
        if self._claude_confidence:
            return self._claude_confidence
        return super()._confidence_label(changes)
