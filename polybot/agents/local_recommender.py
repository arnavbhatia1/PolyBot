"""LocalRecommender — deterministic fallback when Claude API is unavailable.

Inherits the always-on exploratory probe from BaseRecommender and adds five
reactive rules that override exploratory candidates when a specific empirical
pattern is detected (stronger predicted_delta wins the per-param dedupe).
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

        # Reactive overrides — stronger evidence wins per-param dedupe
        self._rule_scalp_overconfidence()
        self._rule_flow_stack()
        self._rule_momentum_regime()
        self._rule_gates_from_ghosts()
        self._rule_indicator_weights()

        # Operator-only suggestions
        self._manual_exit_threshold()
        self._manual_adverse_selection()
        self._manual_flip()

        return self._finalize()

    # ---- reactive rules ---- #

    def _rule_scalp_overconfidence(self) -> None:
        """Holds beating scalps → sharpen L1 (lower atr_sigma_ratio)."""
        cf = self.analysis.get("counterfactual_analysis", {})
        n_scalps = int(cf.get("total_scalps_tracked", 0))
        if n_scalps < 100 or float(cf.get("scalp_accuracy", 0)) >= 0.50:
            return
        if cf.get("net_exit_direction") != "scalp_early":
            return
        if not self._direction_ok("atr_sigma_ratio", "down"):
            return
        cur = float(self.cfg.get("atr_sigma_ratio", _d("atr_sigma_ratio")))
        self._propose("atr_sigma_ratio", max(1.2, cur - 0.10),
                      f"holds beat scalps (acc {cf.get('scalp_accuracy', 0):.0%}, n={n_scalps}) — sharpen L1",
                      predicted_delta=0.018, ci=(-0.008, 0.040))

    def _rule_flow_stack(self) -> None:
        """Push the flow_stack signal with the strongest historical bt_delta."""
        best: tuple[str, str, float] | None = None
        for param in ("flow_weight", "spot_flow_weight", "liquidation_weight"):
            for direction in ("up", "down"):
                e = self._dir_table.get((param, direction))
                if e and e.get("bt_delta") is not None and e["n"] >= 2 and not e["decays"]:
                    if best is None or e["bt_delta"] > best[2]:
                        best = (param, direction, e["bt_delta"])
        if best is None or best[2] <= 0.005:
            return
        param, direction, bt = best
        cur = float(self.cfg.get(param, _d(param)))
        step = max(0.02, 0.15 * abs(cur))
        new_val = cur + (step if direction == "up" else -step)
        self._propose(param, new_val,
                      f"{param} {direction} avg BT Δ {bt:+.3f}",
                      predicted_delta=max(0.012, bt * 0.5),
                      ci=(-0.008, max(0.025, bt)))

    def _rule_momentum_regime(self) -> None:
        """Regime Sharpe gap → adjust momentum/regime weights."""
        by_r = self.analysis.get("by_regime", {})
        trending, reverting = by_r.get("trending", {}), by_r.get("reverting", {})
        nt, nr = int(trending.get("n", 0)), int(reverting.get("n", 0))
        if nt < 50 and nr < 50:
            return
        ts, rs = float(trending.get("sharpe", 0)), float(reverting.get("sharpe", 0))
        if nr >= 50 and (rs - ts) > 0.05 and self._direction_ok("momentum_weight", "down"):
            cur = float(self.cfg.get("momentum_weight", _d("momentum_weight")))
            self._propose("momentum_weight", max(-0.10, cur - 0.04),
                          f"reverting Sharpe {rs:+.3f} > trending {ts:+.3f} — fade harder",
                          predicted_delta=0.014, ci=(-0.010, 0.030))
        elif nt >= 50 and (ts - rs) > 0.05 and self._direction_ok("regime_weight", "up"):
            cur = float(self.cfg.get("regime_weight", _d("regime_weight")))
            self._propose("regime_weight", cur + 0.015,
                          f"trending Sharpe {ts:+.3f} > reverting {rs:+.3f} — strengthen L2",
                          predicted_delta=0.015, ci=(-0.008, 0.035))

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

    def _rule_indicator_weights(self) -> None:
        """L4 mix — only adjust when an indicator is >65% accurate at n>=30."""
        per_ind = self.analysis.get("per_indicator", {})
        if not per_ind:
            return
        winners = {ind: s for ind, s in per_ind.items()
                   if float(s.get("accuracy", 0)) > 0.65 and int(s.get("sample_size", 0)) >= 30}
        if not winners:
            return
        cur = dict(self.cfg.get("weights", {}) or {})
        if not cur:
            return
        new_w = dict(cur)
        bonus = 0.03 * len(winners)
        for w in winners:
            new_w[w] = min(0.50, new_w.get(w, 0.20) + 0.03)
        losers = [k for k in new_w if k not in winners]
        if losers:
            dec = bonus / len(losers)
            for lo in losers:
                new_w[lo] = max(0.05, new_w[lo] - dec)
        total = sum(new_w.values())
        if total > 0:
            new_w = {k: round(v / total, 4) for k, v in new_w.items()}
            largest = max(new_w, key=new_w.get)
            new_w[largest] = round(1.0 - sum(v for k, v in new_w.items() if k != largest), 4)
        self._propose("weights", new_w,
                      f"indicators >65% accurate: {', '.join(winners)}",
                      predicted_delta=0.005, ci=(-0.005, 0.012))

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
