"""LocalRecommender — deterministic fallback when Claude API is unavailable.

Inherits the always-on exploratory probe from BaseRecommender and adds a
ghost-gate loosening rule plus operator-only suggestions on top.
"""
from __future__ import annotations

from typing import Any

from polybot.config.param_registry import default_for as _d
from polybot.agents.recommender_base import BaseRecommender, _MIN_N


class LocalRecommender(BaseRecommender):
    SOURCE_NAME = "Local"

    def recommend(self) -> dict[str, Any]:
        n = int(self.analysis.get("overall", {}).get("total_trades", 0) or 0)
        if n < 50:
            return self._insufficient(n)

        # Forced structural probes (once per audit-identified value)
        self._rule_structural_probes()

        # Always-on exploratory probe (shared base class)
        self._rule_exploratory()

        # Reactive override — loosen gates blocking profitable ghosts
        self._rule_gates_from_ghosts()

        # Reactive tunable — exit_edge_threshold from counterfactual evidence
        self._rule_exit_threshold()

        # Operator-only suggestions (genuinely manual-only params)
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
            if not stats or stats.get("count", 0) < 50:
                continue
            if float(stats.get("pct_profitable", 0)) > 0.60 and float(stats.get("simulated_pnl", 0)) > 0:
                cur = float(self.cfg.get(param, 0.0))
                target = round(cur * 0.90, 4) if cur > 0 else cur
                if target != cur and not self._value_failed(param, target):
                    self._propose(param, target,
                                  f"{gate_key} ghosts {stats.get('pct_profitable', 0):.0%} profitable — loosen",
                                  predicted_delta=0.012, ci=(-0.008, 0.030))
                    return  # one gate per cycle

    def _rule_exit_threshold(self) -> None:
        """exit_edge_threshold is the one TUNABLE exit knob (PIPELINE_PARAMS, backtested
        via the counterfactual override). When the counterfactual tracker shows a net
        scalp-early / hold-long bias, propose a threshold move into `changes` so the
        WeightOptimizer adopts it on evidence — NOT a manual observation."""
        cf = self.analysis.get("counterfactual_analysis", {})
        n = int(cf.get("total_scalps_tracked", 0))
        if n < _MIN_N:
            return
        cur = float(self.cfg.get("exit_edge_threshold", _d("exit_edge_threshold")))
        net = cf.get("net_exit_direction", "calibrated")
        if net == "scalp_early":
            target = max(-0.10, cur - 0.02)
        elif net == "hold_long":
            target = round(min(-0.03, cur + 0.02), 4)
        else:
            return
        if not self._value_failed("exit_edge_threshold", target):
            self._propose("exit_edge_threshold", target,
                          f"counterfactual net '{net}' over {n} scalps — tune exit threshold",
                          predicted_delta=0.012, ci=(-0.008, 0.030))

    # ---- manual-only suggestions (genuinely manual params) ---- #

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
        if gap > 0.05:
            cur = float(self.cfg.get("flip_edge_premium", _d("flip_edge_premium")))
            note = (f"flip Sharpe {fs:+.3f} trails base by {gap:.3f}"
                    + (" and negative — raise premium aggressively" if fs < 0
                       else " — raise premium"))
            bump = 0.02 if fs < 0 else 0.01
            self._emit_manual("flip_edge_premium", cur, round(min(0.05, cur + bump), 4),
                              note, nf)
