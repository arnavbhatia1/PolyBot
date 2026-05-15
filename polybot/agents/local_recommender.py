"""LocalRecommender: deterministic fallback for the pipeline evolver.

Inherits all shared logic (init, exploratory probe, dedupe, clamping, noise, manual
emission) from :class:`BaseRecommender` — the ONLY thing this subclass adds is
five reactive pattern-detection rules that mirror Claude's analytical themes
(scalp counterfactual, flow stack, regime split, ghost gates, indicator mix),
plus three manual-only triggers (exit threshold, adverse selection, flip).

The exploratory probe runs FIRST every cycle (via the base class), so the
pipeline never goes silent on a good-Sharpe day. Reactive rules then OVERRIDE
those exploratory candidates with stronger evidence where applicable — the
shared dedupe-by-param keeps the highest-predicted-delta proposal per param.

Same output schema and same exploration cadence as ClaudeRecommender; the
only meaningful difference is whether reactive proposals come from the local
rules or from Claude's prompt-driven reasoning.
"""
from __future__ import annotations

import logging
from typing import Any

from polybot.config.param_registry import default_for as _d
from polybot.agents.recommender_base import BaseRecommender, _family_of

logger = logging.getLogger(__name__)


class LocalRecommender(BaseRecommender):
    SOURCE_NAME = "Local"

    # -------- public entry point -------- #

    def recommend(self) -> dict[str, Any]:
        overall = self.analysis.get("overall", {})
        n = int(overall.get("total_trades", 0) or 0)
        if n < 50:
            self.warnings.append(f"Only {n} trades — insufficient data, no changes applied")
            return self._envelope(confidence="low", reasoning="Insufficient data (N<50).")

        # Exploratory first — every tunable param gets a small probe so the
        # pipeline always has candidates to test. Walk-forward backtest decides
        # which (if any) actually improve Sharpe.
        self._rule_exploratory()

        # Reactive rules — pattern-specific overrides with stronger evidence.
        # When these fire they replace the exploratory proposal for the same
        # param via the shared dedupe (higher predicted_delta wins).
        self._rule_scalp_overconfidence()
        self._rule_flow_stack()
        self._rule_momentum_regime()
        self._rule_gates_from_ghosts()
        self._rule_indicator_weights()

        # Manual-only triggers (operator review queue, not adopted automatically)
        self._manual_rule_exit_threshold()
        self._manual_rule_adverse_selection()
        self._manual_rule_flip()

        # Diagnostic findings for the operator
        self._collect_key_findings()

        return self._finalize()

    # -------- reactive rules: pattern detectors -------- #

    def _rule_scalp_overconfidence(self) -> None:
        """Map counterfactual scalp/hold outcomes to a backtestable proposal.

        When holds beat scalps the entry signal decays during the window, so
        sharpen L1 (lower atr_sigma_ratio). When scalps beat holds the entry
        is overconfident; Platt corrects that — no proposal needed here.
        """
        cf = self.analysis.get("counterfactual_analysis", {})
        n_scalps = int(cf.get("total_scalps_tracked", 0))
        if n_scalps < 100:
            return
        scalp_acc = float(cf.get("scalp_accuracy", 0))
        if scalp_acc >= 0.50:
            return
        if cf.get("net_exit_direction") != "scalp_early":
            return
        cur = float(self.cfg.get("atr_sigma_ratio", _d("atr_sigma_ratio")))
        ok, _why = self._direction_ok("atr_sigma_ratio", "down")
        if not ok:
            return
        new_val = max(1.2, cur - 0.10)
        if self._propose(
            "atr_sigma_ratio", new_val,
            f"scalp_accuracy {scalp_acc:.0%} with holds beating scalps over {n_scalps} trades — "
            f"sharpen L1 so the entry signal holds longer to resolution",
            predicted_delta=0.018,
            ci=(-0.008, 0.040),
        ):
            self.findings.append(
                f"Holds beat scalps (acc {scalp_acc:.0%}, n={n_scalps}) → "
                f"atr_sigma_ratio {cur:.2f}→{new_val:.2f}"
            )

    def _rule_flow_stack(self) -> None:
        """flow_weight / spot_flow_weight / liquidation_weight — chase the strongest
        empirical signal in flow_stack."""
        best: tuple[str, str, float] | None = None
        for param in ("flow_weight", "spot_flow_weight", "liquidation_weight"):
            for direction in ("up", "down"):
                entry = self._dir_table.get((param, direction))
                if entry and entry["bt_delta"] is not None and entry["n"] >= 2 and not entry["decays"]:
                    if best is None or entry["bt_delta"] > best[2]:
                        best = (param, direction, entry["bt_delta"])
        if best is None:
            return
        param, direction, bt = best
        if bt <= 0.005:
            return
        cur = float(self.cfg.get(param, _d(param) if param in {"flow_weight", "spot_flow_weight", "liquidation_weight"} else 0.04))
        new_val = self._decisive_value(param, cur, direction, min_step=0.02)
        self._propose(
            param, new_val,
            f"empirical table shows {param} {direction} avg BT Δ {bt:+.3f} — strongest flow_stack signal",
            predicted_delta=max(0.012, bt * 0.5),
            ci=(-0.008, max(0.025, bt)),
        )

    def _rule_momentum_regime(self) -> None:
        """momentum_weight / regime_weight — adjust on regime breakdown."""
        by_regime = self.analysis.get("by_regime", {})
        trending = by_regime.get("trending", {})
        reverting = by_regime.get("reverting", {})
        if trending.get("n", 0) < 50 and reverting.get("n", 0) < 50:
            return
        t_sharpe = float(trending.get("sharpe", 0))
        r_sharpe = float(reverting.get("sharpe", 0))
        if reverting.get("n", 0) >= 50 and r_sharpe - t_sharpe > self._noise["sharpe_2x"]:
            cur = float(self.cfg.get("momentum_weight", _d("momentum_weight")))
            target = max(-0.10, cur - 0.04)
            ok, _why = self._direction_ok("momentum_weight", "down")
            if ok and target != cur:
                self._propose(
                    "momentum_weight", target,
                    f"reverting Sharpe {r_sharpe:+.3f} > trending {t_sharpe:+.3f} by >2σ — fade harder",
                    predicted_delta=0.014,
                    ci=(-0.010, 0.030),
                )
        elif trending.get("n", 0) >= 50 and t_sharpe - r_sharpe > self._noise["sharpe_2x"]:
            cur = float(self.cfg.get("regime_weight", _d("regime_weight")))
            target = self._decisive_value("regime_weight", cur, "up", min_step=0.015)
            ok, _why = self._direction_ok("regime_weight", "up")
            if ok:
                self._propose(
                    "regime_weight", target,
                    f"trending Sharpe {t_sharpe:+.3f} > reverting {r_sharpe:+.3f} by >2σ — strengthen L2",
                    predicted_delta=0.015,
                    ci=(-0.008, 0.035),
                )

    def _rule_gates_from_ghosts(self) -> None:
        """Entry gates — open up gates that block profitable ghosts."""
        ghost = self.analysis.get("ghost_analysis", {})
        by_gate = (ghost or {}).get("by_gate", {})
        if not by_gate:
            return
        for gate_key, param, _lo_dir in [
            ("low_edge",  "min_edge",              "down"),
            ("low_kelly", "min_kelly",             "down"),
            ("low_prob",  "min_model_probability", "down"),
        ]:
            stats = by_gate.get(gate_key)
            if not stats or stats.get("count", 0) < 100:
                continue
            pct_profit = float(stats.get("pct_profitable", 0))
            sim_pnl = float(stats.get("simulated_pnl", 0))
            if pct_profit > 0.60 and sim_pnl > 0:
                cur = float(self.cfg.get(param, 0.0))
                target = round(cur * 0.90, 4) if cur > 0 else cur
                if target != cur and not self._value_already_failed(param, target):
                    self._propose(
                        param, target,
                        f"{gate_key} ghosts: {pct_profit:.0%} profitable, sim_pnl=${sim_pnl:+.1f} — gate over-filtering",
                        predicted_delta=0.012,
                        ci=(-0.008, 0.030),
                    )
                    self.findings.append(f"{gate_key} blocked {stats['count']} profitable ghosts → loosen {param}")
                    break

    def _rule_indicator_weights(self) -> None:
        """L4 indicator mix — only act when an indicator is consistently >65% accurate."""
        per_ind = self.analysis.get("per_indicator", {})
        if not per_ind:
            return
        threshold = 0.50 + self._noise["per_ind_2x"]
        winners = {ind: stats for ind, stats in per_ind.items()
                   if float(stats.get("accuracy", 0)) > max(threshold, 0.65)
                   and int(stats.get("sample_size", 0)) >= 30}
        if not winners:
            return
        cur = dict(self.cfg.get("weights", {}) or {})
        if not cur:
            return
        new_w = dict(cur)
        bonus = 0.03 * len(winners)
        for w_ind in winners:
            new_w[w_ind] = min(0.50, new_w.get(w_ind, 0.20) + 0.03)
        losers = [k for k in new_w if k not in winners]
        if losers:
            decrement = bonus / len(losers)
            for lo in losers:
                new_w[lo] = max(0.05, new_w[lo] - decrement)
        total = sum(new_w.values())
        if total > 0:
            new_w = {k: round(v / total, 4) for k, v in new_w.items()}
            largest = max(new_w, key=new_w.get)
            new_w[largest] = round(1.0 - sum(v for k, v in new_w.items() if k != largest), 4)
        self._propose(
            "weights", new_w,
            f"indicators >65% accurate at N>=30: {', '.join(winners)} — reweight toward winners",
            predicted_delta=0.005,
            ci=(-0.005, 0.012),
        )

    # -------- manual-only suggestions -------- #

    def _manual_rule_exit_threshold(self) -> None:
        cf = self.analysis.get("counterfactual_analysis", {})
        n_scalps = int(cf.get("total_scalps_tracked", 0))
        if n_scalps < 50:
            return
        scalp_acc = float(cf.get("scalp_accuracy", 0))
        net_dir = cf.get("net_exit_direction", "calibrated")
        cur = self.cfg.get("exit_edge_threshold", _d("exit_edge_threshold"))
        if net_dir == "scalp_early":
            self._emit_manual(
                "exit_edge_threshold", cur, max(-0.10, float(cur) - 0.02),
                "Counterfactual: holds beat scalps — make scalp threshold more negative (harder to scalp)",
                {"metric": "net_exit_direction", "value": net_dir, "n": n_scalps,
                 "source": "counterfactual_analysis"},
                confidence="medium" if scalp_acc < 0.45 else "low",
            )
        elif net_dir == "hold_long":
            self._emit_manual(
                "exit_edge_threshold", cur, round(min(-0.03, float(cur) + 0.02), 4),
                "Counterfactual: scalps beat holds — relax scalp threshold (easier to scalp)",
                {"metric": "net_exit_direction", "value": net_dir, "n": n_scalps,
                 "source": "counterfactual_analysis"},
                confidence="medium",
            )

    def _manual_rule_adverse_selection(self) -> None:
        ghost = self.analysis.get("ghost_analysis", {})
        gate = ghost.get("by_gate", {}).get("adverse_rate_30s")
        if not gate:
            return
        n = int(gate.get("count", 0))
        if n < 50:
            return
        pct_profit = float(gate.get("pct_profitable", 0))
        sim_pnl = float(gate.get("simulated_pnl", 0))
        cur = float(self.cfg.get("adverse_selection_threshold", _d("adverse_selection_threshold")))
        if pct_profit > 0.60 and sim_pnl > 0:
            self._emit_manual(
                "adverse_selection_threshold", cur, round(min(0.75, cur + 0.05), 3),
                f"adverse_rate_30s gate: {pct_profit:.0%} profitable ghosts, sim_pnl=${sim_pnl:+.1f} — gate over-filtering",
                {"metric": "ghost.adverse_rate_30s.pct_profitable", "value": pct_profit, "n": n,
                 "source": "ghost_analysis.by_gate.adverse_rate_30s"},
                confidence="medium",
            )

    def _manual_rule_flip(self) -> None:
        flip_data = self.analysis.get("flip_analysis", {})
        if not flip_data:
            return
        base = flip_data.get("base", {})
        flip = flip_data.get("flip", {})
        if flip.get("n", 0) < 50 or base.get("n", 0) < 50:
            return
        flip_sharpe = float(flip.get("sharpe", 0))
        base_sharpe = float(base.get("sharpe", 0))
        gap = base_sharpe - flip_sharpe
        if gap > 2 * self._noise["sharpe_2x"] and flip_sharpe < 0:
            self._emit_manual(
                "flip_enabled", self.cfg.get("flip_enabled", True), False,
                f"Flip-trade Sharpe {flip_sharpe:+.3f} (n={flip.get('n', 0)}) trails "
                f"base {base_sharpe:+.3f} by {gap:.3f} and is negative — kill flips",
                {"metric": "flip_analysis.sharpe_gap", "value": gap,
                 "n": flip.get("n", 0), "source": "flip_analysis"},
                confidence="high",
            )
        elif gap > 2 * self._noise["sharpe_2x"]:
            cur = float(self.cfg.get("flip_edge_premium", _d("flip_edge_premium")))
            self._emit_manual(
                "flip_edge_premium", cur, round(min(0.05, cur + 0.01), 4),
                f"Flip-trade Sharpe {flip_sharpe:+.3f} trails base {base_sharpe:+.3f} "
                f"by {gap:.3f} — raise the re-entry edge premium",
                {"metric": "flip_analysis.sharpe_gap", "value": gap,
                 "n": flip.get("n", 0), "source": "flip_analysis"},
                confidence="medium",
            )

    # -------- key findings (diagnostics) -------- #

    def _collect_key_findings(self) -> None:
        cur_reg = self.analysis.get("current_regime", {})
        overall_wr = float(self.analysis.get("overall", {}).get("win_rate", 0))
        if cur_reg and int(cur_reg.get("n_trades", 0)) >= 30 and overall_wr:
            reg_wr = float(cur_reg.get("win_rate", 0))
            if abs(reg_wr - overall_wr) > 2 * self._noise["wr_2x"]:
                direction = "improving" if reg_wr > overall_wr else "deteriorating"
                self.findings.append(
                    f"Recent {cur_reg['n_trades']} trades {direction}: "
                    f"WR {reg_wr:.0%} vs overall {overall_wr:.0%}"
                )

        side = self.analysis.get("side_analysis", {})
        if len(side) >= 2:
            items = list(side.items())
            wr0 = float(items[0][1].get("win_rate", 0))
            wr1 = float(items[1][1].get("win_rate", 0))
            n0 = int(items[0][1].get("count", 0))
            n1 = int(items[1][1].get("count", 0))
            if min(n0, n1) >= 50 and abs(wr0 - wr1) > 2 * self._noise["wr_2x"]:
                better = items[0][0] if wr0 > wr1 else items[1][0]
                worse = items[1][0] if wr0 > wr1 else items[0][0]
                self.findings.append(
                    f"Side imbalance: {better} ({max(wr0, wr1):.0%} WR) >> "
                    f"{worse} ({min(wr0, wr1):.0%} WR) — check directional model bias"
                )

        platt_meta = str(self.analysis.get("platt_meta_warning", "") or "")
        if platt_meta:
            self.findings.append(f"Platt meta: {platt_meta[:120]}")

        eq = self.analysis.get("execution_quality", {})
        if eq:
            avg_slip = float(eq.get("avg_fill_slippage", 0) or 0)
            if avg_slip > 0.005:
                self.warnings.append(
                    f"avg_fill_slippage {avg_slip:+.4f} > 0.005 — slippage eating realized edge"
                )
                self.findings.append(
                    f"High slippage {avg_slip:+.4f}: raise logit_scale to self-filter "
                    f"marginal entries, or review kelly_fraction"
                )
            fok_rate = eq.get("fok_fill_rate")
            if fok_rate is not None and float(fok_rate) < 0.80:
                self.findings.append(
                    f"FOK fill rate {float(fok_rate):.0%} "
                    f"({eq.get('fok_total_attempts', 0)} attempts) — many orders rejected"
                )

        gate_stats = self.analysis.get("gate_skip_stats", {})
        if gate_stats:
            counts = {k: v for k, v in gate_stats.items()
                      if k != "total_skips" and isinstance(v, (int, float)) and v > 0}
            if counts:
                total_skips = gate_stats.get("total_skips", sum(counts.values()))
                top_gate, top_count = max(counts.items(), key=lambda x: x[1])
                manual_only = {"adverse_selection", "adverse_rate_30s"}
                note = "manual-only lever" if top_gate in manual_only else "consider loosening"
                self.findings.append(
                    f"Top gate: {top_gate} blocks {top_count}/{total_skips} skips "
                    f"({top_count / max(total_skips, 1):.0%}) — {note}"
                )

        if self._blocked_params:
            blocked_list = sorted(self._blocked_params)[:4]
            self.findings.append(
                f"Skipped (negative delta last cycle): {', '.join(blocked_list)}"
            )
