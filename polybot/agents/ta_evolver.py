import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class TAEvolver:
    def __init__(self, strategy_log_path: str, claude_client=None):
        self.strategy_log_path = Path(strategy_log_path)
        self.claude_client = claude_client

    # --- Primary entry point (async, uses Claude) ---

    async def evolve(self, outcomes: list[dict], analysis: dict,
                     current_config: dict) -> dict:
        """Compile data, call Claude for strategy analysis, fall back to local on failure.

        Returns a recommendations dict with at minimum 'recommended_weights'.
        May also include: recommended_momentum_weight, recommended_min_edge,
        recommended_kelly_fraction, key_findings, risk_warnings, reasoning, confidence.
        """
        if not outcomes:
            return {}

        # Read recent strategy log entries for context
        prev = ""
        if self.strategy_log_path.exists():
            text = self.strategy_log_path.read_text()
            prev = text[-2000:] if len(text) > 2000 else text

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

        # Fallback: local weight adjustment
        current_weights = current_config.get("weights", {})
        local_weights = self.recommend_weight_adjustments(outcomes, current_weights)
        self._save_local_log(self.analyze(outcomes), local_weights)
        return {"recommended_weights": local_weights}

    # --- Local fallback methods (sync, no API) ---

    def analyze(self, outcomes: list[dict]) -> dict:
        if not outcomes:
            return {"win_rate": 0, "avg_log_return": 0, "total_trades": 0}
        wins = sum(1 for o in outcomes if o.get("correct", False))
        returns = [o.get("log_return", 0) for o in outcomes]
        return {"win_rate": wins / len(outcomes),
                "avg_log_return": sum(returns) / len(returns),
                "total_trades": len(outcomes)}

    def recommend_weight_adjustments(self, outcomes: list[dict], current_weights: dict) -> dict:
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

    def _save_claude_log(self, recommendations: dict):
        """Append Claude's analysis to strategy_log.md."""
        self.strategy_log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        confidence = recommendations.get("confidence", "?")
        reasoning = recommendations.get("reasoning", "")
        findings = recommendations.get("key_findings", [])
        warnings = recommendations.get("risk_warnings", [])
        weights = recommendations.get("recommended_weights", {})
        mw = recommendations.get("recommended_momentum_weight", "?")
        me = recommendations.get("recommended_min_edge", "?")
        kf = recommendations.get("recommended_kelly_fraction", "?")

        findings_str = "\n".join(f"- {f}" for f in findings) if findings else "- None"
        warnings_str = "\n".join(f"- {w}" for w in warnings) if warnings else "- None"
        weights_str = ", ".join(f"{k}={v:.2f}" for k, v in weights.items()) if weights else "unchanged"

        entry = (
            f"\n## {now}\n\n"
            f"**Source:** Claude (confidence: {confidence})\n\n"
            f"**Key Findings:**\n{findings_str}\n\n"
            f"**Risk Warnings:**\n{warnings_str}\n\n"
            f"**Reasoning:** {reasoning}\n\n"
            f"**Recommended Weights:** {weights_str}\n"
            f"**Recommended Parameters:** momentum_weight={mw}, min_edge={me}, kelly_fraction={kf}\n"
        )

        existing = self.strategy_log_path.read_text() if self.strategy_log_path.exists() else "# Strategy Evolution Log\n"
        self.strategy_log_path.write_text(existing + entry)

    def _save_local_log(self, analysis: dict, recommended_weights: dict):
        """Append local fallback analysis to strategy_log.md."""
        self.strategy_log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        entry = (
            f"\n## {now}\n\n"
            f"**Source:** Local fallback (Claude unavailable)\n\n"
            f"**Analysis:** {analysis}\n\n"
            f"**Recommended Weights:** {recommended_weights}\n"
        )
        existing = self.strategy_log_path.read_text() if self.strategy_log_path.exists() else "# Strategy Evolution Log\n"
        self.strategy_log_path.write_text(existing + entry)

    def save_log(self, analysis: dict, recommended_weights: dict):
        """Legacy method for backward compatibility."""
        self._save_local_log(analysis, recommended_weights)
