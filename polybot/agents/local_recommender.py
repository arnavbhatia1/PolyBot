"""LocalRecommender — deterministic fallback when Claude API is unavailable.

Inherits the always-on exploratory probe from BaseRecommender and adds a
ghost-gate loosening rule plus operator-only suggestions on top.
"""
from __future__ import annotations

from typing import Any

from polybot.config.param_registry import default_for as _d
from polybot.agents.recommender_base import BaseRecommender


class LocalRecommender(BaseRecommender):
    SOURCE_NAME = "Local"

    def recommend(self) -> dict[str, Any]:
        n = int(self.analysis.get("overall", {}).get("total_trades", 0) or 0)
        if n < 50:
            return self._insufficient(n)

        # Always-on exploratory probe (shared base class)
        self._rule_exploratory()

        # Reactive override — loosen gates blocking profitable ghosts
        self._rule_gates_from_ghosts()

        # Operator-only suggestions
        self._manual_exit_threshold()
        self._manual_adverse_selection()
        self._manual_flip()

        return self._finalize()

    # ---- reactive rules ---- #

    def _rule_gates_from_ghosts(self) -> None:
        """Loosen gates that are blocking profitable ghosts."""
        by_gate = (self.analysis.get("ghost_analysis", {}) or {}).get("by_gate", {})
        for gate_key, param in [("low_edge", "min_edge"),
                                ("low_kelly", "min_kelly"),
                                ("low_prob", "min_model_probability")]:
            stats = by_gate.get(gate_key)
            if not stats or stats.get("count", 0) < 100:
                continue
            if float(stats.get("pct_profitable", 0)) > 0.60 and float(stats.get("simulated_pnl", 0)) > 0:
                cur = float(self.cfg.get(param, 0.0))
                target = round(cur * 0.90, 4) if cur > 0 else cur
                if target != cur and not self._value_failed(param, target):
                    self._propose(param, target,
                                  f"{gate_key} ghosts {stats.get('pct_profitable', 0):.0%} profitable — loosen",
                                  predicted_delta=0.012, ci=(-0.008, 0.030))
                    return  # one gate per cycle

    # ---- manual-only suggestions ---- #

    def _manual_exit_threshold(self) -> None:
        cf = self.analysis.get("counterfactual_analysis", {})
        n = int(cf.get("total_scalps_tracked", 0))
        cur = self.cfg.get("exit_edge_threshold", _d("exit_edge_threshold"))
        net = cf.get("net_exit_direction", "calibrated")
        if net == "scalp_early":
            self._emit_manual("exit_edge_threshold", cur, max(-0.10, float(cur) - 0.02),
                              "holds beat scalps — make scalp threshold more negative", n)
        elif net == "hold_long":
            self._emit_manual("exit_edge_threshold", cur, round(min(-0.03, float(cur) + 0.02), 4),
                              "scalps beat holds — relax scalp threshold", n)

    def _manual_adverse_selection(self) -> None:
        gate = (self.analysis.get("ghost_analysis", {}) or {}).get("by_gate", {}).get("adverse_rate_30s")
        if not gate:
            return
        n = int(gate.get("count", 0))
        if float(gate.get("pct_profitable", 0)) > 0.60 and float(gate.get("simulated_pnl", 0)) > 0:
            cur = float(self.cfg.get("adverse_selection_threshold", _d("adverse_selection_threshold")))
            self._emit_manual("adverse_selection_threshold", cur, round(min(0.75, cur + 0.05), 3),
                              f"adverse-rate gate over-filtering ({gate.get('pct_profitable', 0):.0%} profitable)", n)

    def _manual_flip(self) -> None:
        flip_data = self.analysis.get("flip_analysis", {})
        base, flip = flip_data.get("base", {}), flip_data.get("flip", {})
        nb, nf = int(base.get("n", 0)), int(flip.get("n", 0))
        if nb < 50 or nf < 50:
            return
        bs, fs = float(base.get("sharpe", 0)), float(flip.get("sharpe", 0))
        gap = bs - fs
        if gap > 0.05 and fs < 0:
            self._emit_manual("flip_enabled", self.cfg.get("flip_enabled", True), False,
                              f"flip Sharpe {fs:+.3f} trails base by {gap:.3f} and negative — kill flips", nf)
        elif gap > 0.05:
            cur = float(self.cfg.get("flip_edge_premium", _d("flip_edge_premium")))
            self._emit_manual("flip_edge_premium", cur, round(min(0.05, cur + 0.01), 4),
                              f"flip Sharpe {fs:+.3f} trails base by {gap:.3f} — raise premium", nf)
