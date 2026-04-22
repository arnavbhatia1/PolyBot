"""TAEvolver: sends a distilled strategy analysis card to Claude and returns weight recommendations.

Builds a context including BiasDetector output, gate skip stats, realized edge, ghost
trade analysis, and 100 stratified trades (50 recent + 50 spaced). Falls back to
rule-based local recommendations when Claude is unavailable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

        # Read recent strategy log entries for context
        prev = ""
        if self.strategy_log_path.exists():
            text = self.strategy_log_path.read_text(encoding="utf-8")
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

        # Fallback: principled rule-based recommendations from bias report
        current_weights = current_config.get("weights", {})
        local_weights = self.recommend_weight_adjustments(outcomes, current_weights)
        recs = self._local_param_recommendations(analysis, current_config, local_weights)
        self._save_local_log(self.analyze(outcomes), recs)
        return recs

    def _local_param_recommendations(self, analysis: dict[str, Any],
                                      current_config: dict[str, Any],
                                      local_weights: dict[str, float]) -> dict[str, Any]:
        """Rule-based `changes` list when Claude is unavailable.

        Guardrails:
          - Max 2 scalar parameter changes per run (plus weights)
          - No scalar change more than 15% of current value
          - Only proposes BACKTESTABLE params (min_edge etc. are read-only)
        """
        findings: list[str] = []
        warnings: list[str] = []
        changes_list: list[dict[str, Any]] = []

        overall = analysis.get("overall", {})
        wr = overall.get("win_rate", 0)
        sharpe = overall.get("sharpe", 0)
        n = overall.get("total_trades", 0)
        avg_gain = overall.get("avg_gain_pct", 0)

        tw = analysis.get("time_weighted", {})
        tw_wr = tw.get("win_rate", wr)
        tw_sharpe = tw.get("sharpe", sharpe)

        er_q = analysis.get("edge_realization_quartiles", [])
        avg_realization = sum(er_q) / len(er_q) if er_q else None

        # Always propose the local-weights adjustment as one change
        if local_weights:
            changes_list.append({
                "param": "weights",
                "value": local_weights,
                "reason": "local fallback — indicator-effectiveness reweight",
            })

        # Rule 1: Win rate declining → reduce kelly_fraction (clamped to ±15%)
        cur_kelly = current_config.get("kelly_fraction", 0.15)
        if n >= 30 and tw_wr < 0.50:
            new_kelly = max(0.05, round(max(cur_kelly - 0.01, cur_kelly * 0.85), 4))
            if new_kelly != cur_kelly:
                changes_list.append({
                    "param": "kelly_fraction", "value": new_kelly,
                    "reason": f"Time-weighted WR {tw_wr:.0%} < 50% — trim Kelly",
                })
                findings.append(f"Time-weighted WR {tw_wr:.0%} < 50% — reducing kelly_fraction {cur_kelly}->{new_kelly}")
                warnings.append("Recent win rate declining")
        elif n >= 30 and tw_wr > 0.58 and sharpe > 0.3:
            new_kelly = min(0.25, round(min(cur_kelly + 0.01, cur_kelly * 1.15), 4))
            if new_kelly != cur_kelly:
                changes_list.append({
                    "param": "kelly_fraction", "value": new_kelly,
                    "reason": f"Strong WR {tw_wr:.0%} + Sharpe {sharpe:.2f} — raise Kelly",
                })
                findings.append(f"Strong WR {tw_wr:.0%} + Sharpe {sharpe:.2f} — raising kelly_fraction {cur_kelly}->{new_kelly}")

        # Rule 2: Poor edge realization → raise atr_sigma_ratio (min_edge is read-only now)
        cur_atr_sigma = current_config.get("atr_sigma_ratio", 1.4)
        if avg_realization is not None and avg_realization < 0.65 and n >= 30 and len(changes_list) < 3:
            new_sigma = min(2.5, round(cur_atr_sigma + 0.1, 3))
            if new_sigma != cur_atr_sigma:
                changes_list.append({
                    "param": "atr_sigma_ratio", "value": new_sigma,
                    "reason": f"Edge realization {avg_realization:.0%} < 65% — widen L1 sigma",
                })
                findings.append(f"Edge realization {avg_realization:.0%} < 65% — raising atr_sigma_ratio {cur_atr_sigma}->{new_sigma}")

        # Regime-pattern finding only (no change proposed — dir/magnitude uncertain without backtest)
        by_regime = analysis.get("by_regime", {})
        trending = by_regime.get("trending", {})
        reverting = by_regime.get("reverting", {})
        if trending.get("n", 0) >= 20 and reverting.get("n", 0) >= 20:
            t_wr = trending.get("win_rate", 0.5)
            r_wr = reverting.get("win_rate", 0.5)
            if abs(t_wr - r_wr) > 0.05:
                findings.append(f"Trending WR {t_wr:.0%} vs reverting {r_wr:.0%} — review flow_weight")

        if n >= 30:
            findings.append(f"Overall: {n} trades, WR {wr:.0%}, Sharpe {sharpe:+.3f}, avg gain {avg_gain:+.4f}")
            if tw_wr != wr:
                findings.append(f"Recent (14d-weighted): WR {tw_wr:.0%}, Sharpe {tw_sharpe:+.3f}")

        return {
            "changes": changes_list[:5],
            "key_findings": findings,
            "risk_warnings": warnings,
            "reasoning": "Local rule-based fallback (Claude unavailable)",
        }

    # --- Local fallback methods (sync, no API) ---

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

        entry = (
            f"\n## {now}\n\n"
            f"**Source:** Claude ({confidence})\n"
            f"**Proposed Changes ({len(changes_list)}):**\n{changes_str}\n\n"
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
