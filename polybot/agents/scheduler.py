"""Orchestrates the nightly learning pipeline.

Runs BiasDetector, Platt calibration (with recency-weighted MLE), distribution shift
detection, SPRT aggregation, TA Evolver (Claude), and WeightOptimizer in sequence.
Adopts parameter changes only when they pass: z = Δ_sharpe / JK_SE >= 0.3 (autocorr
adjusted, no static abs floor), n >= 100 candidate trades, regime-stratified Sharpe
check. After ≥2 adoptions: combined backtest interaction check (backs out weakest
if combined Δ < 0.7 × sum of individual Δ).
"""
from __future__ import annotations

import asyncio
import math
import logging
from datetime import datetime, timezone
from typing import Any
from polybot.config.loader import save_config
from polybot.config.param_registry import default_for as _d

logger = logging.getLogger(__name__)

def _format_pipeline_summary(pipeline_info: dict[str, Any]) -> str:
    """Human-readable nightly pipeline result — logged and sent to Discord."""
    wi: dict[str, Any] = pipeline_info.get("weights", {}) or {}
    platt: dict[str, Any] = pipeline_info.get("platt", {}) or {}
    source = pipeline_info.get("source", "?")
    _now = datetime.now(timezone.utc)
    ts = f"{_now.strftime('%b')} {_now.day}, {_now.strftime('%Y  %H:%M UTC')}"

    baseline = wi.get("old_sharpe", 0.0) or 0.0
    n_baseline = wi.get("n_baseline_trades", 0) or 0
    abs_floor = 0.010
    per_change = wi.get("per_change", []) or []
    se_val: float | None = None
    if n_baseline >= 2:
        se_val = math.sqrt((1.0 + 0.5 * baseline * baseline) / n_baseline)
    dyn_floor = max(abs_floor, 0.25 * se_val) if se_val is not None else abs_floor

    SEP = "═" * 60
    lines: list[str] = []
    lines.append(SEP)
    lines.append(f"  NIGHTLY RESULT — {ts}  [{source}]")
    lines.append(SEP)

    # Model health line
    sharpe_str = f"Sharpe {baseline:+.3f}" if n_baseline > 0 else "Sharpe n/a"
    n_str = f"{n_baseline:,} trades" if n_baseline > 0 else "no trades yet"
    lines.append(f"  Model:  {sharpe_str} on {n_str}  |  need +{dyn_floor:.3f} delta to adopt any change")
    lines.append("")

    # Parameter changes
    if per_change:
        adopted = [c for c in per_change if c.get("decision") == "adopted"]
        rejected = [c for c in per_change if c.get("decision") != "adopted"]
        if adopted:
            lines.append(f"  Parameters updated ({len(adopted)}):")
            for c in adopted:
                param = c.get("param", "?")
                old_val = c.get("old_value", "?")
                new_val = c.get("value", "?")
                cand_sharpe = c.get("candidate_sharpe")
                delta = (cand_sharpe - baseline) if isinstance(cand_sharpe, (int, float)) else None
                delta_str = f"  delta +{delta:.3f}" if delta is not None else ""
                lines.append(f"    [+] {param}  {old_val} → {new_val}{delta_str}")
        if rejected:
            lines.append(f"  Tested but not adopted ({len(rejected)}):")
            for c in rejected:
                param = c.get("param", "?")
                old_val = c.get("old_value", "?")
                new_val = c.get("value", "?")
                cand_sharpe = c.get("candidate_sharpe")
                delta = (cand_sharpe - baseline) if isinstance(cand_sharpe, (int, float)) else None
                if delta is not None and delta < 0:
                    why = "made things worse"
                elif delta is not None and delta < dyn_floor:
                    why = f"improvement too small ({delta:+.3f}, need {dyn_floor:.3f})"
                else:
                    why = c.get("reason", "didn't pass gates")[:50]
                lines.append(f"    [-] {param}  {old_val} → {new_val}  — {why}")
    else:
        reason = wi.get("reason", "all parameter combinations tested, none cleared the bar")
        lines.append(f"  No parameter changes — {reason}")

    # Platt calibration
    p_dec = platt.get("decision", "skipped")
    if p_dec in ("adopted", "rejected", "reverted"):
        id_loss = platt.get("identity_loss")
        cur_loss = platt.get("current_loss")
        new_loss = platt.get("new_loss")
        id_sharpe = platt.get("identity_sharpe")
        new_sharpe = platt.get("new_sharpe")
        reason = platt.get("reason", "")
        # Loss string: identity → current → new
        if id_loss and cur_loss and new_loss:
            loss_str = f"accuracy:  identity {id_loss:.3f}  →  current {cur_loss:.3f}  →  new {new_loss:.3f}"
        else:
            loss_str = ""
        cur_sharpe = platt.get("current_sharpe")
        sharpe_str = (f"  |  sizing:  identity {id_sharpe:.3f}  →  current {cur_sharpe:.3f}  →  new {new_sharpe:.3f}"
                      if id_sharpe is not None and cur_sharpe is not None and new_sharpe is not None else "")
        if p_dec == "adopted":
            lines.append(f"  Calibration:  updated  —  {loss_str}{sharpe_str}")
        elif p_dec == "reverted":
            lines.append(f"  Calibration:  reverted to identity  —  {reason}")
        else:
            lines.append(f"  Calibration:  kept existing  —  {reason}"
                         + (f"  |  {loss_str}{sharpe_str}" if loss_str else ""))
    elif p_dec == "skipped":
        reason = platt.get("reason", "")
        lines.append(f"  Calibration:  skipped" + (f" — {reason}" if reason else ""))

    # Manual actions needed
    manual_obs: list[dict[str, Any]] = pipeline_info.get("manual_observations", []) or []
    if manual_obs:
        lines.append("")
        lines.append(f"  {'─' * 56}")
        lines.append(f"  ACTION NEEDED — edit settings.yaml manually:")
        for ob in manual_obs:
            p = ob.get("param", "?")
            cur = ob.get("current", "?")
            sug = ob.get("suggested", "?")
            conf = ob.get("confidence", "low").upper()
            reason = (ob.get("reason", "") or "").strip()
            lines.append(f"    {p}  {cur} → {sug}  [{conf}]")
            if reason:
                # Wrap reason at ~70 chars
                words = reason.split()
                line_buf, wrapped = [], []
                for w in words:
                    if sum(len(x) + 1 for x in line_buf) + len(w) > 70:
                        wrapped.append("    " + " ".join(line_buf))
                        line_buf = [w]
                    else:
                        line_buf.append(w)
                if line_buf:
                    wrapped.append("    " + " ".join(line_buf))
                lines.extend(wrapped)

    lines.append(SEP)
    return "\n".join(lines)


class AgentScheduler:
    def __init__(self, outcome_reviewer: Any, bias_detector: Any, ta_evolver: Any, weight_optimizer: Any,
                 indicator_engine: Any = None, signal_engine: Any = None, alert_manager: Any = None,
                 outcome_interval_seconds: int = 3600, daily_pipeline_hour: int = 2,
                 daily_pipeline_minute: int = 0, math_config: dict[str, Any] | None = None,
                 claude_client: Any = None, market_scanner: Any = None,
                 config: dict[str, Any] | None = None, counterfactual_tracker: Any = None,
                 pipeline_tracker: Any = None) -> None:
        self.outcome_reviewer: Any = outcome_reviewer
        self.bias_detector: Any = bias_detector
        self.ta_evolver: Any = ta_evolver
        self.weight_optimizer: Any = weight_optimizer
        self.indicator_engine: Any = indicator_engine
        self.signal_engine: Any = signal_engine
        self.alert_manager: Any = alert_manager
        self.outcome_interval_seconds: int = outcome_interval_seconds
        self.daily_pipeline_hour: int = daily_pipeline_hour
        self.daily_pipeline_minute: int = daily_pipeline_minute
        self.math_config: dict[str, Any] = math_config or {}
        self.claude_client: Any = claude_client  # stored for future use + passed to ta_evolver
        self.market_scanner: Any = market_scanner
        self._config: dict[str, Any] | None = config  # Full config dict — written back to settings.yaml after pipeline adoption
        self.counterfactual_tracker: Any = counterfactual_tracker
        self.pipeline_tracker: Any = pipeline_tracker
        self.ghost_tracker: Any = None  # injected by main.py after construction
        self._exit_edge_threshold: float | None = None  # Set by main.py, updated by pipeline
        self._min_time_remaining: int | None = None   # Set by main.py, updated by pipeline
        self._trading_start: tuple[int, int] | None = None        # (hour, minute) ET — updated by pipeline
        self._trading_end: tuple[int, int] | None = None          # (hour, minute) ET — updated by pipeline
        self._running: bool = False
        self._auto_shutdown: bool = False
        self._last_per_change_results: list[str] = []  # per-parameter backtest results for Claude
        self._baseline_kelly_sharpe: float = 0.0  # current baseline Kelly-Sharpe for Claude context
        self._last_rerouted_params: list[str] = []  # manual-only params Claude tried to put in `changes` last cycle
        self._shutdown_requested: bool = False

        # Inject claude_client into ta_evolver if not already set
        if claude_client and not getattr(self.ta_evolver, 'claude_client', None):
            self.ta_evolver.claude_client = claude_client

    async def _run_bias_detector(self, outcomes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if outcomes is None:
            outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            logger.info("No outcomes to analyze for biases")
            return {}
        analysis = self.bias_detector.detect(outcomes)
        self.bias_detector.save(analysis)
        return analysis

    def _ghost_to_outcome(self, g: dict[str, Any]) -> dict[str, Any] | None:
        """Normalize a resolved ghost record into the same shape as outcomes/.

        Ghosts are trades rejected at live entry gates (min_edge, min_prob, min_kelly, etc.).
        Including them in the backtest population unlocks those gates from read-only:
        raising a gate now filters the same fills out of baseline and candidate equally,
        and lowering a gate includes the ghosts that would have fired — and we know how
        each ghost resolved.

        Gain_pct is re-derived from the recorded market_price_<side> so it matches the
        execution-price accounting used by real outcomes (ghost_gain_pct stored on-disk
        is computed against signal_prob, which doesn't match real fills).
        """
        if not g.get("resolved"):
            return None
        side = (g.get("side") or "").lower()
        if side not in ("up", "down"):
            return None
        ctx = g.get("indicator_snapshot", {}).get("trade_context", {}) or {}
        mp = ctx.get("market_price_up", 0) if side == "up" else ctx.get("market_price_down", 0)
        if not mp or mp <= 0 or mp >= 1:
            return None
        correct = bool(g.get("ghost_correct"))
        gain_pct = ((1.0 - mp) / mp) if correct else -1.0
        return {
            "side": side,
            "correct": correct,
            "gain_pct": round(gain_pct, 4),
            "indicator_snapshot": g.get("indicator_snapshot", {}),
            "entry_price": mp,
            "exit_price": 1.0 if correct else 0.0,
            "exit_timestamp": str(g.get("resolved_at") or g.get("timestamp") or ""),
            "timestamp": str(g.get("timestamp") or ""),
            "is_ghost": True,
        }

    def _load_combined_outcomes(self) -> list[dict[str, Any]]:
        """Real outcomes + normalized resolved ghosts, sorted by exit_timestamp."""
        real = self.outcome_reviewer.load_all_outcomes() if self.outcome_reviewer else []
        real = real or []
        ghost_outcomes: list[dict[str, Any]] = []
        gt = getattr(self, 'ghost_tracker', None)
        if gt is not None:
            try:
                for g in gt.load_all():
                    norm = self._ghost_to_outcome(g)
                    if norm is not None:
                        ghost_outcomes.append(norm)
            except Exception as e:
                logger.debug(f"Ghost load failed (non-critical): {e}")
        combined = real + ghost_outcomes
        combined.sort(key=lambda x: str(x.get("exit_timestamp") or x.get("timestamp") or ""))
        return combined

    def _precompute_baseline(self, all_outcomes: list[dict[str, Any]]) -> None:
        """Compute baseline Kelly-Sharpe + JK_SE + N and cache on self BEFORE Claude runs.

        Without this, first-cycle-after-restart Claude context shows `baseline_n_trades=None`
        because the values are only set inside `_run_weight_optimizer` which runs AFTER
        the TA evolver (and thus after context is built). Called once per pipeline cycle.
        """
        from polybot.agents.weight_optimizer import _sharpe as _s, _lag1_autocorr as _ac
        if not all_outcomes or len(all_outcomes) < 10:
            return
        n = len(all_outcomes)
        fold_boundaries = [0.60, 0.70, 0.80, 0.90, 1.0]
        all_returns: list[float] = []
        for i in range(len(fold_boundaries) - 1):
            start_idx = int(n * fold_boundaries[i])
            end_idx = int(n * fold_boundaries[i + 1])
            fold_test = all_outcomes[start_idx:end_idx]
            if len(fold_test) < 3:
                continue
            all_returns.extend(self._backtest_recommendations({}, fold_test))
        if not all_returns:
            return
        current_sharpe = _s(all_returns)
        self._baseline_kelly_sharpe = round(current_sharpe, 4)
        n_base = len(all_returns)
        base_se = math.sqrt((1.0 + 0.5 * current_sharpe ** 2) / n_base)
        if n_base >= 3:
            rho = _ac(all_returns)
            base_se *= math.sqrt(1.0 + 2.0 * max(0.0, rho))
        self._baseline_jk_se = round(base_se, 4)
        self._baseline_n_trades = n_base

    async def _run_ta_evolver(self, analysis: dict[str, Any], outcomes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if outcomes is None:
            outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            return {}

        # Build current config from live engines
        current_weights = self.indicator_engine.get_weights() if self.indicator_engine else {}
        current_config = {
            "weights": {k: v for k, v in current_weights.items()
                        if k in ["rsi", "macd", "stochastic", "obv", "vwap"]},
            "momentum_weight": getattr(self.signal_engine, 'momentum_weight', _d("momentum_weight")),
            "regime_weight": getattr(self.signal_engine, 'regime_weight', _d("regime_weight")),
            "flow_weight": getattr(self.signal_engine, 'flow_weight', _d("flow_weight")),
            "student_t_df": getattr(self.signal_engine, 'student_t_df', _d("student_t_df")),
            "min_edge": getattr(self.signal_engine, 'min_edge', _d("min_edge")),
            "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', _d("kelly_fraction")),
            "min_model_probability": getattr(self.signal_engine, 'min_model_probability', _d("min_model_probability")),
            "exit_edge_threshold": getattr(self, '_exit_edge_threshold', _d("exit_edge_threshold")),
            "min_time_remaining": getattr(self, '_min_time_remaining', 0),
            "trading_start_hour_et": self._trading_start[0] if self._trading_start else 0,
            "trading_end_hour_et": self._trading_end[0] if self._trading_end else 23,
            "trading_end_minute": self._trading_end[1] if self._trading_end else 59,
            "min_kelly": getattr(self.signal_engine, 'min_kelly', _d("min_kelly")),
            "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', _d("atr_sigma_ratio")),
            "spot_flow_weight": getattr(self.signal_engine, 'spot_flow_weight', _d("spot_flow_weight")),
            "prev_margin_weight": getattr(self.signal_engine, 'prev_margin_weight', _d("prev_margin_weight")),
            "logit_scale": getattr(self.signal_engine, 'logit_scale', _d("logit_scale")),
            "liquidation_weight": getattr(self.signal_engine, 'liquidation_weight', _d("liquidation_weight")),
            "adverse_selection_threshold": (self._config or {}).get("signal", {}).get("adverse_selection_threshold", _d("adverse_selection_threshold")),
            "normal_fraction": (self._config or {}).get("entry_timing", {}).get("normal_fraction", _d("normal_fraction")),
            "late_max_penalty": (self._config or {}).get("entry_timing", {}).get("late_max_penalty", _d("late_max_penalty")),
            "min_atr": getattr(self.signal_engine, 'min_atr', _d("min_atr")),
            "max_edge": getattr(self.signal_engine, 'max_edge', _d("max_edge")),
        }

        if hasattr(self, '_last_per_change_results') and self._last_per_change_results:
            analysis["last_per_change_results"] = self._last_per_change_results
        if hasattr(self, '_last_rerouted_params') and self._last_rerouted_params:
            analysis["last_rerouted_params"] = list(self._last_rerouted_params)

        # Adoption gate is a pure z-test (delta_sharpe / JK_SE >= ADOPTION_Z_FLOOR).
        # Surface baseline Sharpe and SE so Claude can size proposals to clear it.
        if hasattr(self, '_baseline_kelly_sharpe'):
            from polybot.agents.weight_optimizer import ADOPTION_Z_FLOOR
            analysis["baseline_kelly_sharpe"] = self._baseline_kelly_sharpe
            jk_se = getattr(self, '_baseline_jk_se', None)
            if jk_se is not None:
                dyn_floor = ADOPTION_Z_FLOOR * jk_se
                analysis["baseline_jk_se"] = jk_se
                analysis["baseline_n_trades"] = getattr(self, '_baseline_n_trades', None)
                analysis["adoption_z_floor"] = ADOPTION_Z_FLOOR
                analysis["adoption_dynamic_floor"] = round(dyn_floor, 4)
                analysis["adoption_target"] = round(self._baseline_kelly_sharpe + dyn_floor, 4)
        # Cumulative failures derived from pipeline_run_log.json (restart-safe, no duplicate state)
        if self.pipeline_tracker:
            try:
                cum = self.pipeline_tracker.get_cumulative_failures()
                if cum:
                    analysis["cumulative_failures"] = cum
            except Exception as e:
                logger.debug(f"Failed to derive cumulative failures: {e}")

        # Inject prediction accuracy, empirical directional table, and decay analysis
        if self.pipeline_tracker:
            try:
                pred_accuracy = self.pipeline_tracker.format_prediction_accuracy()
                if pred_accuracy:
                    analysis["prediction_accuracy"] = pred_accuracy
                dir_table = self.pipeline_tracker.format_directional_table()
                if dir_table:
                    analysis["directional_table"] = dir_table
                decay_analysis = self.pipeline_tracker.format_decay_analysis()
                if decay_analysis:
                    analysis["decay_analysis"] = decay_analysis
                recently_tested = self.pipeline_tracker.get_recently_tested_params(n_cycles=3)
                if recently_tested:
                    analysis["recently_tested_params"] = list(recently_tested)
            except Exception as e:
                logger.debug(f"Failed to build prediction/directional/decay context: {e}")

        # Build parameter change history for Claude
        if self.pipeline_tracker:
            try:
                history_lines = []
                records = self.pipeline_tracker.get_track_record()
                # Last 5 adoptions with Sharpe outcome
                adoptions = [r for r in records if r.get("changes")]
                for rec in adoptions[-5:]:
                    date = rec.get("date", "?")[:10]
                    baseline = rec.get("baseline_sharpe", 0)
                    predicted = rec.get("predicted_sharpe", 0)
                    changes_dict = rec.get("changes", {})
                    r7 = rec.get("review_7d")
                    actual_str = f"7d actual Sharpe={r7['sharpe']:.3f}" if r7 else "7d: pending"
                    change_strs = [f"{k}: {v[0]}->{v[1]}" for k, v in list(changes_dict.items())[:4]]
                    history_lines.append(
                        f"ADOPTED {date}: {', '.join(change_strs)} | "
                        f"predicted {baseline:.3f}->{predicted:.3f} | {actual_str}"
                    )

                # Rollback-recommended adoptions (7d review showed the change hurt)
                rollback_recs = [r for r in records if r.get("rollback_recommended")]
                for rec in rollback_recs[-3:]:
                    date = rec.get("date", "?")[:10]
                    r7 = rec.get("review_7d", {})
                    baseline = rec.get("baseline_sharpe", 0)
                    history_lines.append(
                        f"ROLLBACK {date}: 7d Sharpe={r7.get('sharpe', 0):.3f} trailed baseline {baseline:.3f} — "
                        f"changes: {', '.join(f'{k}: {v[0]}->{v[1]}' for k, v in list(rec.get('changes', {}).items())[:4])}"
                    )

                if history_lines:
                    analysis["parameter_history"] = "\n".join(history_lines)
            except Exception as e:
                logger.debug(f"Failed to build parameter history: {e}")

        # Active adoptions table — which past proposals are currently LIVE or ROLLED_BACK.
        # Prevents Claude wasting proposal slots on reversed params because it didn't know.
        if self.pipeline_tracker:
            try:
                from datetime import datetime as _dt, timezone as _tz
                now = _dt.now(_tz.utc)
                active_lines: list[str] = []
                rolled_lines: list[str] = []
                records = self.pipeline_tracker.get_track_record()
                for rec in records:
                    try:
                        rec_dt = _dt.fromisoformat(rec.get("date", ""))
                    except (ValueError, TypeError):
                        continue
                    age_days = (now - rec_dt).total_seconds() / 86400.0
                    if age_days > 30:
                        continue
                    for param, (old_val, new_val) in (rec.get("changes") or {}).items():
                        # Look up the current live value from the built current_config
                        if param == "weights":
                            continue  # multi-value; skip from this compact table
                        cur = current_config.get(param)
                        r7 = rec.get("review_7d") or {}
                        pred = rec.get("predicted_sharpe", 0)
                        baseline = rec.get("baseline_sharpe", 0)
                        pred_delta = round(pred - baseline, 3)
                        actual_delta = round(r7.get("sharpe", 0) - baseline, 3) if r7 else None
                        actual_str = f"7d_actual={actual_delta:+.3f}" if actual_delta is not None else "7d=pending"
                        # Rolled back = current live value no longer matches what was adopted
                        try:
                            drifted = (cur is None) or (abs(float(cur) - float(new_val)) > 1e-6)
                        except (TypeError, ValueError):
                            drifted = (cur != new_val)
                        if drifted:
                            rolled_lines.append(
                                f"  {param}: was adopted {old_val}->{new_val} {age_days:.0f}d ago, "
                                f"now at {cur} — {actual_str} vs pred={pred_delta:+.3f}"
                            )
                        else:
                            active_lines.append(
                                f"  {param}: {old_val}->{new_val} (adopted {age_days:.0f}d ago) "
                                f"{actual_str} vs pred={pred_delta:+.3f} — LIVE"
                            )
                sections_out: list[str] = []
                if active_lines:
                    sections_out.append("ACTIVE ADOPTIONS (last 30 days, currently LIVE):\n" + "\n".join(active_lines))
                if rolled_lines:
                    sections_out.append("ROLLED BACK (adopted value no longer live):\n" + "\n".join(rolled_lines))
                if sections_out:
                    analysis["active_adoptions"] = "\n\n".join(sections_out)
            except Exception as e:
                logger.debug(f"Failed to build active adoptions table: {e}")

        recommendations = await self.ta_evolver.evolve(outcomes, analysis, current_config)
        # Capture which manual-only params Claude tried to propose in `changes` this cycle
        # (the validator rerouted them to manual_observations). Surfaced next cycle so
        # Claude sees the misclassification and stops repeating it.
        rerouted = [
            o.get("param", "") for o in (recommendations.get("manual_observations") or [])
            if isinstance(o, dict) and o.get("source_channel") == "rerouted"
        ]
        self._last_rerouted_params = [p for p in rerouted if p]
        return recommendations

    def _kelly_bankroll_returns(
        self,
        outcomes: list[dict[str, Any]],
        recommended_weights: dict[str, float],
        momentum_weight: float,
        atr_sigma_ratio: float,
        student_t_df: int,
        min_edge: float,
        calibrator: Any,
        kelly_fraction: float,
        min_kelly: float,
        min_prob: float,
        regime_weight: float | None = None,
        flow_weight: float | None = None,
        spot_flow_weight: float | None = None,
        liquidation_weight: float | None = None,
        prev_margin_weight: float | None = None,
        logit_scale: float | None = None,
        min_atr: float | None = None,
    ) -> list[float]:
        """Replay the full logit composition used in production for a candidate
        config and return the Kelly-sized per-trade returns. Sharpe of the result
        is the candidate's adoption metric.
        """
        # Pull every optional default from the registry — keeps this method
        # in lockstep with settings.yaml / param_registry.
        if regime_weight is None: regime_weight = _d("regime_weight")
        if flow_weight is None: flow_weight = _d("flow_weight")
        if spot_flow_weight is None: spot_flow_weight = _d("spot_flow_weight")
        if liquidation_weight is None: liquidation_weight = _d("liquidation_weight")
        if prev_margin_weight is None: prev_margin_weight = _d("prev_margin_weight")
        if logit_scale is None: logit_scale = _d("logit_scale")
        if min_atr is None: min_atr = _d("min_atr")
        from scipy.stats import t as t_dist
        max_flow_logit = 0.35
        realism_factor = 1.0
        if self._config:
            realism_factor = float(self._config.get("execution", {}).get("backtest_realism_factor", 1.0))
        # Recency: 0.97/day decay (~23-day half-life). Applied symmetrically to
        # baseline and candidate, so relative Sharpe comparisons remain valid.
        now_ts = datetime.now(timezone.utc).timestamp()
        returns: list[float] = []

        for o in outcomes:
            snap = o.get("indicator_snapshot", {})
            if not snap:
                continue
            ctx = snap.get("trade_context", {})

            stored_raw = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
            if stored_raw <= 0 or stored_raw >= 1:
                continue

            side = (o.get("side") or "").lower()
            if side not in ("up", "down"):
                continue

            market_price_side = ctx.get("market_price_up", 0) if side == "up" else ctx.get("market_price_down", 0)
            if market_price_side <= 0 or market_price_side >= 1:
                continue

            # L1 — re-derive raw prob_up from CDF; fall back to stored when incomplete.
            btc = ctx.get("btc_price", 0)
            strike = ctx.get("strike_price", 0)
            atr_raw = ctx.get("atr", 0)
            atr = max(atr_raw, min_atr)
            secs = ctx.get("seconds_remaining", 0)


            raw_prob_up = stored_raw
            if btc > 0 and strike > 0 and secs > 0 and student_t_df > 2:
                minutes = secs / 60.0
                vol = (atr / atr_sigma_ratio) * math.sqrt(minutes)
                if vol > 0:
                    z = ((btc - strike) / vol) * math.sqrt(student_t_df / (student_t_df - 2))
                    raw_prob_up = float(t_dist.cdf(z, student_t_df))
            raw_prob_up = max(1e-6, min(1 - 1e-6, raw_prob_up))
            logit_p = math.log(raw_prob_up / (1.0 - raw_prob_up))

            # L2 — regime × direction. Use stored autocorr float when available (exact),
            # fall back to the regime_state string approximation for old outcomes.
            stored_autocorr = ctx.get("regime_autocorr")
            if stored_autocorr is not None:
                regime_factor = float(stored_autocorr)
            else:
                regime_str = (ctx.get("regime_state") or "").lower()
                if regime_str.startswith("trending"):
                    regime_factor = 0.20
                elif regime_str.startswith("mean"):
                    regime_factor = -0.20
                else:
                    regime_factor = 0.0
            prev_margin = ctx.get("prev_resolution_margin", 0.0)
            # Direction: prefer the actual last-1min-return sign captured at signal time
            # (stored from signal_engine.last_regime_direction). Fall back to
            # sign(prev_resolution_margin) for outcomes recorded before that field
            # was added — the proxy is noisy but the field is now exact for new trades.
            stored_direction = ctx.get("regime_direction")
            if stored_direction is not None:
                direction = float(stored_direction)
            else:
                direction = 1.0 if prev_margin > 0 else (-1.0 if prev_margin < 0 else 0.0)
            logit_p += regime_factor * direction * (regime_weight * logit_scale)

            # L3 + L3b — CLOB flow + spot flow, capped to prevent triple-counting
            logit_before_flow = logit_p
            flow_signal = ctx.get("flow_score", 0.0)
            logit_p += flow_signal * (flow_weight * logit_scale)
            spot_flow = ctx.get("spot_flow_signal", 0.0)
            logit_p += spot_flow * (spot_flow_weight * logit_scale)
            flow_total = logit_p - logit_before_flow
            if abs(flow_total) > max_flow_logit:
                logit_p = logit_before_flow + max_flow_logit * (1.0 if flow_total > 0 else -1.0)

            # L3e — liquidation pressure
            liq = ctx.get("liquidation_pressure", 0.0)
            if liq != 0.0:
                logit_p += liq * (liquidation_weight * logit_scale)

            # L5 — previous-window margin carry (tanh-normalized by ATR)
            if prev_margin != 0.0 and atr_raw > 0:
                normalized = prev_margin / max(atr_raw, 1.0)
                logit_p += math.tanh(normalized) * (prev_margin_weight * logit_scale)

            # L4 — indicator momentum (recomputed from stored norm_scores × candidate weights).
            momentum_score = sum(
                snap.get(ind, {}).get("norm_score", snap.get(ind, {}).get("score", 0)) * recommended_weights.get(ind, 0)
                for ind in ("rsi", "macd", "stochastic", "obv", "vwap")
            )
            momentum_score = max(-1.0, min(1.0, momentum_score))
            logit_p += momentum_score * momentum_weight * logit_scale

            prob_up_adj = 1.0 / (1.0 + math.exp(-logit_p))
            if calibrator is not None and hasattr(calibrator, "calibrate"):
                calibrated_up = calibrator.calibrate(prob_up_adj)
            else:
                calibrated_up = prob_up_adj

            if side == "up":
                prob_side = calibrated_up
            else:
                prob_side = 1.0 - calibrated_up
            edge = prob_side - market_price_side

            if edge < min_edge:
                continue
            if prob_side < min_prob:
                continue
            full_kelly = edge / (1.0 - market_price_side)
            kelly_frac = kelly_fraction * full_kelly
            if kelly_frac < min_kelly:
                continue

            # Recency weight: recent trades count more (0.97^days_ago decay, ~23d half-life)
            ts_str = o.get("exit_timestamp", o.get("timestamp", ""))
            try:
                trade_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() if ts_str else now_ts
                days_ago = max(0.0, (now_ts - trade_ts) / 86400.0)
            except Exception:
                days_ago = 0.0
            recency_w = 0.97 ** days_ago
            returns.append(kelly_frac * o.get("gain_pct", 0.0) * realism_factor * recency_w)

        return returns

    def _config_for_helper(self, recommendations: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resolve config for `_kelly_bankroll_returns` — recommendation first, live engine fallback.

        Entry gates (min_model_probability, min_edge, min_kelly) are now pipeline-tunable:
        the backtest sample includes resolved ghosts (trades rejected at live gates), so
        raising or lowering any gate filters both baseline and candidate identically and
        the comparison stays clean.
        """
        from polybot.config.param_registry import PIPELINE_PARAMS
        rec = recommendations or {}
        live_weights = self.indicator_engine.get_weights() if self.indicator_engine else {}
        cfg: dict[str, Any] = {
            "weights": rec.get("recommended_weights") or {
                k: live_weights.get(k, 0.0) for k in ("rsi", "macd", "stochastic", "obv", "vwap")
            },
        }
        for _spec in PIPELINE_PARAMS:
            cfg[_spec.name] = _spec.cast(
                rec.get(f"recommended_{_spec.name}",
                        getattr(self.signal_engine, _spec.name, _spec.default))
            )
        return cfg

    def _backtest_recommendations(self, recommendations: dict[str, Any],
                                    outcomes: list[dict[str, Any]]) -> list[float]:
        """Kelly-sized portfolio returns under candidate recommendations.

        Uses ``self.signal_engine.calibrator`` — which is the *just-adopted* Platt for
        this cycle, since Platt fitting + adoption runs earlier in the pipeline (see
        `run_daily_pipeline`). So calibration is part of the optimization loop: every
        cycle, Platt is re-fit on the train split and adopted if it improves
        Kelly-Sharpe on the holdout, then weight backtests run against that fresh
        calibrator. Within a single weight backtest the calibrator is held fixed
        (one variable at a time), but across cycles both layers are continually
        improving in lockstep.
        """
        cfg = self._config_for_helper(recommendations)
        calibrator = self.signal_engine.calibrator if self.signal_engine else None

        return self._kelly_bankroll_returns(
            outcomes=outcomes,
            recommended_weights=cfg["weights"],
            momentum_weight=cfg["momentum_weight"],
            atr_sigma_ratio=cfg["atr_sigma_ratio"],
            student_t_df=cfg["student_t_df"],
            min_edge=cfg["min_edge"],
            calibrator=calibrator,
            kelly_fraction=cfg["kelly_fraction"],
            min_kelly=cfg["min_kelly"],
            min_prob=cfg["min_model_probability"],
            regime_weight=cfg["regime_weight"],
            flow_weight=cfg["flow_weight"],
            spot_flow_weight=cfg["spot_flow_weight"],
            liquidation_weight=cfg["liquidation_weight"],
            prev_margin_weight=cfg["prev_margin_weight"],
            logit_scale=cfg["logit_scale"],
            min_atr=cfg["min_atr"],
        )

    def _backtest_single_change(self, change: dict[str, Any],
                                outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Run a Kelly-backtest with exactly ONE change applied on top of live values.

        Builds a synthetic recommendations dict that only contains the single change,
        so _config_for_helper applies that change while all other params remain at
        their live engine values. Returns {"returns": [...], "sharpe": float,
        "candidate_trades": int}.
        """
        from polybot.agents.weight_optimizer import _sharpe as _s

        param = change.get("param", "")
        value = change.get("value")

        # Clamp BEFORE backtest so we test the exact value that would be applied.
        if param != "weights" and value is not None:
            from polybot.config.param_registry import CLAMP_RANGES
            if param in CLAMP_RANGES:
                lo, hi, cast = CLAMP_RANGES[param]
                try:
                    value = cast(max(lo, min(hi, cast(value))))
                except (TypeError, ValueError):
                    pass

        # Build a thin recommendations dict for _config_for_helper
        single_rec: dict[str, Any] = {}
        from polybot.config.param_registry import TUNABLE_NAMES
        if param == "weights":
            single_rec["recommended_weights"] = value
        elif param in TUNABLE_NAMES:
            single_rec[f"recommended_{param}"] = value
        else:
            # Param not plumbed through _config_for_helper — backtest runs with
            # baseline config unchanged and cannot show improvement.
            logger.warning(
                f"Backtest for '{param}' falls back to baseline config (param not in TUNABLE_NAMES). "
                f"It cannot show improvement and will always be rejected by the z-test."
            )

        cfg = self._config_for_helper(single_rec)
        calibrator = self.signal_engine.calibrator if self.signal_engine else None

        returns = self._kelly_bankroll_returns(
            outcomes=outcomes,
            recommended_weights=cfg["weights"],
            momentum_weight=cfg["momentum_weight"],
            atr_sigma_ratio=cfg["atr_sigma_ratio"],
            student_t_df=cfg["student_t_df"],
            min_edge=cfg["min_edge"],
            calibrator=calibrator,
            kelly_fraction=cfg["kelly_fraction"],
            min_kelly=cfg["min_kelly"],
            min_prob=cfg["min_model_probability"],
            regime_weight=cfg["regime_weight"],
            flow_weight=cfg["flow_weight"],
            spot_flow_weight=cfg["spot_flow_weight"],
            liquidation_weight=cfg["liquidation_weight"],
            prev_margin_weight=cfg["prev_margin_weight"],
            logit_scale=cfg["logit_scale"],
            min_atr=cfg["min_atr"],
        )
        return {"returns": returns, "sharpe": _s(returns), "candidate_trades": len(returns)}

    def _check_regime_adoption(
        self,
        change: dict[str, Any],
        all_outcomes: list[dict[str, Any]],
        baseline_sharpe: float,
    ) -> tuple[bool, str]:
        """Regime-stratified adoption gate.

        Segments outcomes into trending / reverting / neutral buckets.
        The change must either:
          a) improve Sharpe in ≥2 of the 3 populated regimes, OR
          b) improve the dominant regime AND not degrade any other regime by >0.10 Sharpe

        Skipped (returns True) when fewer than 2 regimes have ≥ 20 qualifying trades.
        """
        from polybot.agents.weight_optimizer import _sharpe as _s

        # Segment outcomes by regime
        regime_buckets: dict[str, list] = {"trending": [], "reverting": [], "neutral": []}
        for o in all_outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            r = (ctx.get("regime_state") or "neutral").lower()
            if r.startswith("trending"):
                regime_buckets["trending"].append(o)
            elif r in ("reverting", "mean_reverting"):
                regime_buckets["reverting"].append(o)
            else:
                regime_buckets["neutral"].append(o)

        MIN_REGIME_N = 20
        populated = {k: v for k, v in regime_buckets.items() if len(v) >= MIN_REGIME_N}
        if len(populated) < 2:
            return True, "regime check skipped (insufficient per-regime sample)"

        dominant = max(populated, key=lambda k: len(populated[k]))

        baseline_by_regime: dict[str, float] = {}
        candidate_by_regime: dict[str, float] = {}
        for regime, outcomes_r in populated.items():
            base_result = self._backtest_single_change({}, outcomes_r)  # empty = baseline
            cand_result = self._backtest_single_change(change, outcomes_r)
            baseline_by_regime[regime] = base_result["sharpe"]
            candidate_by_regime[regime] = cand_result["sharpe"]

        regressed_hard = [
            r for r in populated
            if baseline_by_regime[r] - candidate_by_regime[r] > 0.10
        ]
        # Gate: dominant regime must improve AND no regime may degrade >0.10 Sharpe.
        dom_improved = candidate_by_regime[dominant] > baseline_by_regime[dominant]
        detail = " | ".join(
            f"{r}: {baseline_by_regime[r]:+.3f}->{candidate_by_regime[r]:+.3f}"
            for r in sorted(populated)
        )
        if dom_improved and not regressed_hard:
            return True, f"regime check passed (dominant {dominant} improved, no hard regression) [{detail}]"
        if regressed_hard:
            return False, f"regime check failed: {regressed_hard} regressed >0.10 Sharpe [{detail}]"
        return False, f"regime check failed: dominant {dominant} did not improve [{detail}]"

    async def _run_weight_optimizer(self, recommendations: dict[str, Any],
                                    all_outcomes: list[dict[str, Any]] | None = None,
                                    pipeline_source: str = "local") -> dict[str, Any]:
        """Run per-parameter walk-forward backtests and adopt each change independently.

        Walk-forward folds (each fold's test set is genuinely out-of-sample):
          Fold 1: Test [60%:70%]
          Fold 2: Test [70%:80%]
          Fold 3: Test [80%:90%]
          Fold 4: Test [90%:100%]

        Each proposed change is backtested in isolation against the baseline so the
        signal-to-noise of any single parameter is measured cleanly. Changes that pass
        the adoption gates are applied and persisted; the rest are logged as rejected.

        Returns info dict with per-change decision details.
        """
        from polybot.agents.weight_optimizer import _sharpe

        info: dict[str, Any] = {"decision": "skipped", "reason": "", "per_change": []}
        if all_outcomes is None:
            all_outcomes = self.outcome_reviewer.load_all_outcomes()
        if not all_outcomes or len(all_outcomes) < 10:
            info["reason"] = f"only {len(all_outcomes) if all_outcomes else 0} outcomes (need 10)"
            return info

        # Get the changes list (new format) or fall back to checking for recommended_weights
        changes_list: list[dict[str, Any]] = recommendations.get("changes", [])

        if not changes_list:
            info["reason"] = "no changes proposed by evolver"
            _cn = getattr(self, '_baseline_n_trades', None)
            _cs = getattr(self, '_baseline_kelly_sharpe', None)
            if _cn and _cs is not None:
                info["old_sharpe"] = round(float(_cs), 4)
                info["n_baseline_trades"] = _cn
            return info

        def _clamp(val, lo, hi): return max(lo, min(hi, val))

        # --- Baseline: reuse the cached values from `_precompute_baseline` if available
        # (computed once per cycle to feed Claude's prompt). Recomputing here would just
        # repeat the same 4-fold backtest with the same data and same calibrator.
        n = len(all_outcomes)
        fold_boundaries = [0.60, 0.70, 0.80, 0.90, 1.0]
        cached_n = getattr(self, '_baseline_n_trades', None)
        cached_sharpe = getattr(self, '_baseline_kelly_sharpe', None)
        if cached_n and cached_n > 0 and cached_sharpe is not None:
            current_sharpe = float(cached_sharpe)
            info["old_sharpe"] = round(current_sharpe, 4)
            info["n_baseline_trades"] = cached_n
            # Candidate-trade returns aren't cached, but win rate isn't needed for
            # adoption — only for `record_score` telemetry. Skip it here; the directional
            # win-rate per candidate is captured per-change below.
        else:
            all_current_returns: list[float] = []
            baseline_request: dict[str, Any] = {}
            for i in range(len(fold_boundaries) - 1):
                start_idx = int(n * fold_boundaries[i])
                end_idx = int(n * fold_boundaries[i + 1])
                fold_test = all_outcomes[start_idx:end_idx]
                if len(fold_test) < 3:
                    continue
                current_fold_returns = self._backtest_recommendations(baseline_request, fold_test)
                all_current_returns.extend(current_fold_returns)
            current_sharpe = _sharpe(all_current_returns) if all_current_returns else 0.0
            info["old_sharpe"] = round(current_sharpe, 4)
            info["n_baseline_trades"] = len(all_current_returns)

            # Cache JK SE for Claude's adoption-target context (next cycle).
            from polybot.agents.weight_optimizer import _lag1_autocorr as _ac
            n_base = len(all_current_returns)
            if n_base >= 2:
                base_se = math.sqrt((1.0 + 0.5 * current_sharpe ** 2) / n_base)
                if n_base >= 3:
                    rho = _ac(all_current_returns)
                    base_se *= math.sqrt(1.0 + 2.0 * max(0.0, rho))
                self._baseline_jk_se = round(base_se, 4)
                self._baseline_n_trades = n_base
                self._baseline_kelly_sharpe = round(current_sharpe, 4)

        # --- Per-change walk-forward backtests ---
        adopted_changes: list[dict[str, Any]] = []
        any_adopted = False

        for change in changes_list[:5]:
            param = change.get("param", "")
            value = change.get("value")
            reason_str = change.get("reason", "")
            change_info: dict[str, Any] = {"param": param, "value": value}

            # Capture old value for directional tracking (before any adoption mutates the engine)
            if self.signal_engine and param != "weights":
                old_val = getattr(self.signal_engine, param, None)
                if old_val is not None:
                    change_info["old_value"] = old_val

            # Pass through Claude's per-change predictions
            for pred_key in ("predicted_delta_sharpe_7d", "confidence_interval"):
                if pred_key in change:
                    change_info[pred_key] = change[pred_key]

            fold_sharpes: list[float] = []
            all_candidate_returns: list[float] = []

            for i in range(len(fold_boundaries) - 1):
                start_idx = int(n * fold_boundaries[i])
                end_idx = int(n * fold_boundaries[i + 1])
                fold_test = all_outcomes[start_idx:end_idx]
                if len(fold_test) < 3:
                    continue
                fold_result = self._backtest_single_change(change, fold_test)
                fold_returns = fold_result["returns"]
                if len(fold_returns) < 3:
                    continue
                fold_sharpes.append(_sharpe(fold_returns))
                all_candidate_returns.extend(fold_returns)

            if len(all_candidate_returns) < 10:
                msg = f"only {len(all_candidate_returns)} hypothetical trades (need 10)"
                change_info.update({"decision": "rejected", "reason": msg})
                logger.debug(f"REJECTED {param}: {msg}")
                info["per_change"].append(change_info)
                continue

            candidate_sharpe = _sharpe(all_candidate_returns)

            # Fold consistency: reject if >1 fold has non-positive Sharpe.
            # Prevents a single lucky period inflating the aggregate z-score.
            n_negative_folds = sum(1 for s in fold_sharpes if s <= 0)
            if len(fold_sharpes) >= 2 and n_negative_folds > 1:
                msg = (f"fold inconsistency: {n_negative_folds}/{len(fold_sharpes)} folds non-positive "
                       f"({[f'{s:+.3f}' for s in fold_sharpes]})")
                change_info.update({"decision": "rejected", "reason": msg})
                logger.debug(f"REJECTED {param}: {msg}")
                info["per_change"].append(change_info)
                continue

            candidate_win_rate = sum(1 for r in all_candidate_returns if r > 0) / len(all_candidate_returns)
            change_info.update({
                "candidate_sharpe": round(candidate_sharpe, 4),
                "candidate_win_rate": round(candidate_win_rate, 4),
                "fold_sharpes": [round(s, 4) for s in fold_sharpes],
                "n_candidate_trades": len(all_candidate_returns),
            })

            adopt, adopt_reason, z_score = self.weight_optimizer.should_adopt(
                current_sharpe, candidate_sharpe,
                n_trades=len(all_candidate_returns),
                candidate_returns=all_candidate_returns,
            )
            change_info["z_score"] = round(z_score, 3)

            # Regime-stratified check: a change that passes aggregate stats
            # but hurts a specific regime is likely overfitting to the dominant sample.
            # Use only the validation fold (40%) — same data the z-test used.
            validation_outcomes = all_outcomes[int(len(all_outcomes) * 0.60):]
            if adopt and validation_outcomes:
                regime_ok, regime_reason = self._check_regime_adoption(change, validation_outcomes, current_sharpe)
                if not regime_ok:
                    adopt = False
                    adopt_reason = f"regime gate: {regime_reason}"
                else:
                    adopt_reason += f" | {regime_reason}"

            if adopt:
                change_info.update({"decision": "adopted", "reason": adopt_reason})
                adopted_changes.append(change)
                any_adopted = True
                old_val_str = ""
                if self.signal_engine and param != "weights":
                    old_val = getattr(self.signal_engine, param, None)
                    if old_val is not None:
                        old_val_str = f"{old_val}->"
                n_trades = len(all_candidate_returns)
                # Detailed line demoted to DEBUG — user-facing summary renders once at pipeline end.
                logger.debug(f"ADOPTED {param}: {old_val_str}{value} ({adopt_reason}, n={n_trades} candidates, baseline={current_sharpe:.3f}, candidate={candidate_sharpe:.3f})")
            else:
                change_info.update({"decision": "rejected", "reason": adopt_reason})
                n_trades = len(all_candidate_returns)
                logger.debug(f"REJECTED {param}: {value} — {adopt_reason} (n={n_trades} candidates, baseline={current_sharpe:.3f}, candidate={candidate_sharpe:.3f})")

            info["per_change"].append(change_info)

        # Store baseline sharpe for Claude's context
        self._baseline_kelly_sharpe = round(current_sharpe, 4)

        # Store per-change results for Claude's next cycle
        self._last_per_change_results = [
            f"{c['param']}={c.get('value', '?')}: {c['decision'].upper()} — {c['reason']} "
            f"(baseline={current_sharpe:.3f}, candidate={c.get('candidate_sharpe', 'N/A')}, "
            f"n={c.get('n_candidate_trades', '?')})"
            for c in info["per_change"]
        ]

        # Record all changes tested this cycle (adopted + rejected) for directional table
        if self.pipeline_tracker:
            try:
                self.pipeline_tracker.record_pipeline_run(
                    source=pipeline_source,
                    baseline_sharpe=current_sharpe,
                    per_change_results=info["per_change"],
                )
            except Exception as e:
                logger.debug(f"Failed to record pipeline run: {e}")

        if not any_adopted:
            info["decision"] = "no_change"
            info["reason"] = "no changes passed adoption gates"
            return info

        # --- Pairwise interaction check ---
        # If ≥2 changes were adopted, run ONE combined backtest on the validation fold.
        # If combined Δ < sum(individual Δ) × 0.7, the changes interact and one is riding
        # on the other's signal. Back out the lowest-conviction change (smallest z-score).
        if len(adopted_changes) >= 2:
            try:
                # Iterative interaction back-out: keep removing the weakest
                # adopted change until the combined Sharpe delta clears 70% of
                # the sum of remaining individual deltas. The original logic
                # backed out only the single weakest change once, which left
                # multi-way interactions among the survivors untreated and
                # silently degraded combined performance.
                MAX_BACKOUT_ITERATIONS = len(adopted_changes)  # at most n-1 useful passes
                backed_out_params: list[str] = []
                combined_sharpe = current_sharpe
                combined_delta = 0.0
                sum_individual_delta = 0.0

                for iteration in range(MAX_BACKOUT_ITERATIONS):
                    combined_rec: dict[str, Any] = {}
                    sum_individual_delta = 0.0
                    for c in adopted_changes:
                        param = c["param"]
                        value = c["value"]
                        if param == "weights":
                            combined_rec["recommended_weights"] = value
                        elif param in (
                            "momentum_weight", "atr_sigma_ratio", "student_t_df", "kelly_fraction",
                            "regime_weight", "flow_weight", "spot_flow_weight",
                            "liquidation_weight", "prev_margin_weight", "logit_scale", "min_atr",
                        ):
                            combined_rec[f"recommended_{param}"] = value
                        ci = next((x for x in info["per_change"]
                                   if x.get("param") == param and x.get("decision") == "adopted"), {})
                        sum_individual_delta += (ci.get("candidate_sharpe", current_sharpe) - current_sharpe)

                    cfg_combined = self._config_for_helper(combined_rec)
                    calibrator = self.signal_engine.calibrator if self.signal_engine else None
                    # Use validation fold only — same data the per-change z-tests used.
                    # Using all_outcomes here inflated combined Sharpe (includes training data)
                    # making the 0.7 threshold almost never trigger.
                    _val_fold = all_outcomes[int(len(all_outcomes) * 0.60):]
                    combined_returns = self._kelly_bankroll_returns(
                        outcomes=_val_fold,
                        recommended_weights=cfg_combined["weights"],
                        momentum_weight=cfg_combined["momentum_weight"],
                        atr_sigma_ratio=cfg_combined["atr_sigma_ratio"],
                        student_t_df=cfg_combined["student_t_df"],
                        min_edge=cfg_combined["min_edge"],
                        calibrator=calibrator,
                        kelly_fraction=cfg_combined["kelly_fraction"],
                        min_kelly=cfg_combined["min_kelly"],
                        min_prob=cfg_combined["min_model_probability"],
                        regime_weight=cfg_combined["regime_weight"],
                        flow_weight=cfg_combined["flow_weight"],
                        spot_flow_weight=cfg_combined["spot_flow_weight"],
                        liquidation_weight=cfg_combined["liquidation_weight"],
                        prev_margin_weight=cfg_combined["prev_margin_weight"],
                        logit_scale=cfg_combined["logit_scale"],
                        min_atr=cfg_combined["min_atr"],
                    )
                    combined_sharpe = _sharpe(combined_returns) if combined_returns else 0.0
                    combined_delta = combined_sharpe - current_sharpe

                    if not info.get("combined_sharpe"):
                        info["combined_sharpe"] = round(combined_sharpe, 4)
                        info["combined_delta"] = round(combined_delta, 4)
                        info["sum_individual_delta"] = round(sum_individual_delta, 4)

                    # Stop if no interaction OR only one change left to keep.
                    if not (sum_individual_delta > 0
                            and combined_delta < sum_individual_delta * 0.7
                            and len(adopted_changes) >= 2):
                        break

                    z_scores = {
                        c["param"]: float(c.get("z_score", 0.0))
                        for c in info["per_change"]
                        if c.get("decision") == "adopted" and c["param"] in {
                            cc["param"] for cc in adopted_changes
                        }
                    }
                    weakest_param = min(z_scores, key=z_scores.get) if z_scores else None
                    if not weakest_param:
                        break

                    adopted_changes = [c for c in adopted_changes if c["param"] != weakest_param]
                    backed_out_params.append(weakest_param)
                    for c in info["per_change"]:
                        if c.get("param") == weakest_param and c.get("decision") == "adopted":
                            c["decision"] = "backed_out"
                            c["reason"] = (
                                f"interaction back-out pass {iteration + 1}: "
                                f"combined d={combined_delta:+.3f} < "
                                f"sum_individual d={sum_individual_delta:+.3f} * 0.7 — "
                                f"weakest remaining change (z={z_scores[weakest_param]:.2f}) removed"
                            )
                    logger.info(
                        f"Interaction back-out pass {iteration + 1}: combined d={combined_delta:+.3f} "
                        f"vs sum_individual d={sum_individual_delta:+.3f}. "
                        f"Removing {weakest_param} (z={z_scores.get(weakest_param, 0):.2f})"
                    )

                if backed_out_params:
                    info["interaction_detected"] = True
                    info["backed_out_params"] = backed_out_params
                    info["backed_out_param"] = backed_out_params[0]
                    info["final_combined_sharpe"] = round(combined_sharpe, 4)
                    info["final_combined_delta"] = round(combined_delta, 4)

            except Exception as e:
                logger.debug(f"Combined backtest failed (non-critical): {e}")

        if not adopted_changes:
            info["decision"] = "no_change"
            info["reason"] = "all changes backed out (interactions)"
            return info

        # --- Apply and persist all adopted changes ---
        info["decision"] = "adopted"
        info["adopted_params"] = [c["param"] for c in adopted_changes]

        weights_change = next((c for c in adopted_changes if c["param"] == "weights"), None)
        new_weights: dict[str, Any] = dict(weights_change["value"]) if weights_change else {}
        if weights_change and self.indicator_engine:
            self.indicator_engine.set_weights({
                k: v for k, v in new_weights.items()
                if k in ("rsi", "macd", "stochastic", "obv", "vwap")
            })

        # Apply adopted changes to signal_engine. Registry is the single source of
        # truth for clamp ranges — no inline literals that diverge from param_registry.
        if self.signal_engine:
            from polybot.config.param_registry import BY_NAME as _BY_NAME
            for change in adopted_changes:
                param = change["param"]
                value = change["value"]
                if param == "weights":
                    self.signal_engine.weights = {k: v for k, v in new_weights.items()
                                                   if k in ["rsi", "macd", "stochastic", "obv", "vwap"]}
                elif param == "exit_edge_threshold":
                    spec = _BY_NAME[param]
                    self._exit_edge_threshold = spec.cast(max(spec.lo, min(spec.hi, spec.cast(value))))
                elif param in _BY_NAME:
                    spec = _BY_NAME[param]
                    clamped = spec.cast(max(spec.lo, min(spec.hi, spec.cast(value))))
                    if hasattr(self.signal_engine, param):
                        setattr(self.signal_engine, param, clamped)
                elif param == "adverse_selection_threshold" and self._config:
                    self._config.setdefault("signal", {})["adverse_selection_threshold"] = _clamp(float(value), 0.45, 0.75)
                elif param == "max_edge":
                    self.signal_engine.max_edge = _clamp(float(value), 0.15, 0.30)
                elif param == "trading_start_hour_et":
                    self._trading_start = (int(value), 0)
                elif param == "trading_end_hour_et":
                    self._trading_end = (int(value), 59)

        # Persist to settings.yaml — registry yaml_key drives section routing.
        if self._config:
            from polybot.config.param_registry import BY_NAME as _BY_NAME
            sig = self._config.setdefault("signal", {})
            sched = self._config.setdefault("schedule", {})

            if weights_change:
                sig["weights"] = {k: v for k, v in new_weights.items()
                                   if k in ("rsi", "macd", "stochastic", "obv", "vwap")}

            for change in adopted_changes:
                param = change["param"]
                value = change["value"]
                if param == "weights":
                    pass  # handled above
                elif param in _BY_NAME:
                    spec = _BY_NAME[param]
                    clamped = spec.cast(max(spec.lo, min(spec.hi, spec.cast(value))))
                    section, field = spec.yaml_key.split(".", 1)
                    self._config.setdefault(section, {})[field] = clamped
                elif param == "adverse_selection_threshold":
                    sig["adverse_selection_threshold"] = _clamp(float(value), 0.45, 0.75)
                elif param == "max_edge":
                    sig["max_edge"] = _clamp(float(value), 0.15, 0.30)
                elif param == "trading_start_hour_et":
                    sched["trading_start_hour_et"] = int(value)
                elif param == "trading_end_hour_et":
                    sched["trading_end_hour_et"] = int(value)
                elif param == "trading_end_minute":
                    sched["trading_end_minute"] = int(value)

            try:
                config_to_save = dict(self._config)
                save_config(config_to_save)
                logger.info("Pipeline parameters persisted to settings.yaml")
            except Exception as e:
                logger.error(f"Failed to persist config: {e}")

        # Track adoption in pipeline_tracker (one record per run, listing all adopted changes)
        if self.pipeline_tracker and adopted_changes:
            tracker_changes: dict[str, tuple] = {}
            for change in adopted_changes:
                param = change["param"]
                value = change["value"]
                if param != "weights" and self.signal_engine:
                    old_val = getattr(self.signal_engine, param, None)
                    tracker_changes[param] = (old_val, value)
                elif param == "weights":
                    tracker_changes["weights"] = ("(prev)", "(new)")

            best_candidate_sharpe = max(
                (ci.get("candidate_sharpe", current_sharpe) for ci in info["per_change"]
                 if ci.get("decision") == "adopted"),
                default=current_sharpe,
            )
            # Sum of Claude's per-change predicted deltas for adopted changes
            adopted_preds = [
                c["predicted_delta_sharpe_7d"]
                for c in info["per_change"]
                if c.get("decision") == "adopted" and c.get("predicted_delta_sharpe_7d") is not None
            ]
            run_predicted_delta = round(sum(adopted_preds), 4) if adopted_preds else None
            self.pipeline_tracker.record_adoption(
                source=pipeline_source,
                version="params",
                baseline_sharpe=current_sharpe,
                predicted_sharpe=best_candidate_sharpe,
                changes=tracker_changes,
                reason=f"{len(adopted_changes)} change(s) adopted",
                run_predicted_delta=run_predicted_delta,
            )

        return info

    def _apply_revert_adoptions(self) -> None:
        """Auto-revert adoptions flagged as rollback_recommended by pipeline_tracker.

        Works newest-first. For each flagged-but-not-yet-reverted record, reverts
        params to their pre-adoption values unless a newer adoption already changed
        the same param (in which case the newer adoption takes precedence).
        Updates both signal_engine and settings.yaml so the revert is live immediately.
        """
        if not self.pipeline_tracker:
            return
        records = self.pipeline_tracker._load()
        if not records:
            return

        def _clamp(v, lo, hi):
            return max(lo, min(hi, v))

        already_handled: set[str] = set()  # params touched by records processed so far
        reverted_any = False

        for rec in reversed(records):  # newest first
            changes_raw = rec.get("changes", {})  # {param: [old_val, new_val]}

            if not rec.get("rollback_recommended") or rec.get("reverted"):
                # Not flagged or already reverted — mark its params as handled
                already_handled.update(changes_raw.keys())
                continue

            # Build revert list using old (pre-adoption) values
            revert_changes: list[dict[str, Any]] = []
            for param, vals in changes_raw.items():
                if param in already_handled or param == "weights":
                    continue
                old_val = vals[0] if isinstance(vals, list) and len(vals) >= 2 else None
                if old_val is None:
                    continue
                revert_changes.append({"param": param, "value": old_val})

            if not revert_changes:
                rec["reverted"] = True
                reverted_any = True
                already_handled.update(changes_raw.keys())
                continue

            # Apply to signal_engine (takes effect in the 45-min window before restart)
            if self.signal_engine:
                from polybot.config.param_registry import BY_NAME as _BY_NAME
                for rc in revert_changes:
                    p, v = rc["param"], rc["value"]
                    if p == "exit_edge_threshold":
                        spec = _BY_NAME[p]
                        self._exit_edge_threshold = spec.cast(max(spec.lo, min(spec.hi, spec.cast(v))))
                    elif p in _BY_NAME and hasattr(self.signal_engine, p):
                        spec = _BY_NAME[p]
                        setattr(self.signal_engine, p, spec.cast(max(spec.lo, min(spec.hi, spec.cast(v)))))
                    elif p == "max_edge":
                        self.signal_engine.max_edge = _clamp(float(v), 0.15, 0.30)

            # Apply to config dict and persist to settings.yaml
            if self._config:
                from polybot.config.param_registry import BY_NAME as _BY_NAME
                for rc in revert_changes:
                    p, v = rc["param"], rc["value"]
                    if p in _BY_NAME:
                        spec = _BY_NAME[p]
                        clamped = spec.cast(max(spec.lo, min(spec.hi, spec.cast(v))))
                        section, field = spec.yaml_key.split(".", 1)
                        self._config.setdefault(section, {})[field] = clamped
                    elif p == "max_edge":
                        self._config.setdefault("signal", {})["max_edge"] = _clamp(float(v), 0.15, 0.30)
                try:
                    from polybot.config.loader import save_config
                    save_config(dict(self._config))
                except Exception as e:
                    logger.error(f"Auto-revert: failed to persist settings.yaml: {e}")
                    continue

            rec["reverted"] = True
            rec["reverted_at"] = datetime.now(timezone.utc).isoformat()
            reverted_any = True
            already_handled.update(changes_raw.keys())

            summary = ", ".join(f"{rc['param']}→{rc['value']}" for rc in revert_changes)
            logger.warning(
                "[AUTO-REVERT] %s rolled back: %s | reason: %s",
                rec.get("version", "?"), summary,
                rec.get("rollback_reason", "performance regression"),
            )

        if reverted_any:
            self.pipeline_tracker._save(records)

    async def run_daily_pipeline(self) -> None:
        _now_utc = datetime.now(timezone.utc)
        now_et_str = f"{_now_utc.strftime('%b')} {_now_utc.day}, {_now_utc.strftime('%Y  %I:%M %p UTC')}"
        logger.info(f"\n{'═' * 60}\n  Nightly pipeline starting — {now_et_str}\n{'═' * 60}")

        pipeline_info: dict[str, Any] = {}

        # Snapshot current config before changes
        old_config = {}
        if self.signal_engine:
            old_config = {
                "min_edge": getattr(self.signal_engine, 'min_edge', _d("min_edge")),
                "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', _d("kelly_fraction")),
                "momentum_weight": getattr(self.signal_engine, 'momentum_weight', _d("momentum_weight")),
                "min_model_probability": getattr(self.signal_engine, 'min_model_probability', _d("min_model_probability")),
                "exit_edge_threshold": self._exit_edge_threshold,
                "min_time_remaining": self._min_time_remaining,
                "trading_start": self._trading_start,
                "trading_end": self._trading_end,
                "min_kelly": getattr(self.signal_engine, 'min_kelly', _d("min_kelly")),
                "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', _d("atr_sigma_ratio")),
            }

        # Walk-forward validation: train on first 60%, validate across 4 expanding
        # folds of the remaining 40% (each fold is genuinely out-of-sample).
        rolled = self.outcome_reviewer.rollup_old_outcomes()
        ghost_rolled = self.ghost_tracker.rollup_old_ghosts() if self.ghost_tracker else 0
        cf_rolled = self.counterfactual_tracker.rollup_old_counterfactuals() if self.counterfactual_tracker else 0

        _raw_outcomes = self._load_combined_outcomes()
        # Bound active dataset to the last PIPELINE_WINDOW_DAYS so weight
        # candidates aren't judged against probability machines that no longer
        # exist. Walk-forward 60/40 is preserved INSIDE the window.
        PIPELINE_WINDOW_DAYS = 60
        _cutoff_ts = datetime.now(timezone.utc).timestamp() - PIPELINE_WINDOW_DAYS * 86400.0
        def _otime(o: dict) -> float:
            s = o.get("exit_timestamp", o.get("timestamp", "")) or ""
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0
        _windowed = [o for o in _raw_outcomes if _otime(o) >= _cutoff_ts]
        if len(_windowed) >= 500:  # need ≥500 for the 4-fold expanding test
            all_outcomes = _windowed
            _window_note = f"  |  bounded to last {PIPELINE_WINDOW_DAYS}d (was {len(_raw_outcomes):,})"
        else:
            all_outcomes = _raw_outcomes
            _window_note = (f"  |  window {PIPELINE_WINDOW_DAYS}d had only "
                            f"{len(_windowed)} trades — using full history {len(_raw_outcomes):,}")
        n_ghosts = sum(1 for o in all_outcomes if o.get("is_ghost"))
        split_idx = max(1, int(len(all_outcomes) * 0.6))
        train_outcomes = all_outcomes[:split_idx]
        validation_outcomes = all_outcomes[split_idx:]

        logger.info(
            f"  [1/4] Data loaded  |  {len(all_outcomes):,} trades "
            f"({len(train_outcomes):,} train / {len(validation_outcomes):,} val)"
            + _window_note
            + (f"  |  rolled up: {rolled} outcomes, {cf_rolled} scalps, {ghost_rolled} ghosts"
               if rolled or cf_rolled or ghost_rolled else "")
        )
        pipeline_info["total_outcomes"] = len(all_outcomes)
        pipeline_info["train_count"] = len(train_outcomes)
        pipeline_info["validation_count"] = len(all_outcomes) - len(train_outcomes)

        # Review past pipeline adoptions (fill in actual 7d/30d Sharpe),
        # then auto-revert any that tanked within 1d or 7d.
        if self.pipeline_tracker:
            self.pipeline_tracker.review_past_adoptions(all_outcomes)
            self._apply_revert_adoptions()

        # Bias detector uses all_outcomes so it sees current regime (including recent
        # test-split data), not just the older 60% training window.
        analysis = await self._run_bias_detector(all_outcomes)

        # Gate skip stats: how often did each entry gate fire?
        # Tells Claude which gates are over-filtering and whether adverse selection /
        # pre-submit drift / late-window guards are actually affecting trade count.
        from pathlib import Path as _Path
        import json as _json
        _gate_stats_path = _Path("polybot/memory/gate_stats.json")
        if _gate_stats_path.exists():
            try:
                gate_stats = _json.loads(_gate_stats_path.read_text())
                # Flatten nested {"counts": {...}, "total_skips": N} → {"gate": N, "total_skips": N}
                # so claude_client can iterate flat k/v pairs without knowing the schema.
                counts = gate_stats.get("counts", gate_stats)
                total = gate_stats.get("total_skips", sum(v for v in counts.values() if isinstance(v, (int, float))))
                analysis["gate_skip_stats"] = {**counts, "total_skips": total}
                pipeline_info["gate_total_skips"] = total
            except Exception:
                pass

        # Realized edge, fill slippage, and live fill rate stats
        realized_edges = [o.get("realized_edge", 0) for o in all_outcomes if o.get("realized_edge") is not None]
        fill_slippages = [o.get("fill_slippage", 0) for o in all_outcomes if o.get("fill_slippage") is not None]
        exec_quality: dict[str, Any] = {}
        if realized_edges:
            exec_quality.update({
                "avg_realized_edge": round(sum(realized_edges) / len(realized_edges), 4),
                "avg_fill_slippage": round(sum(fill_slippages) / len(fill_slippages), 4) if fill_slippages else 0,
                "n_trades_with_data": len(realized_edges),
                "pct_positive_slippage": round(sum(1 for s in fill_slippages if s > 0.001) / len(fill_slippages), 3) if fill_slippages else 0,
            })
        _fill_stats_path = _Path("polybot/memory/fill_stats.json")
        if _fill_stats_path.exists():
            try:
                fill_stats = _json.loads(_fill_stats_path.read_text())
                exec_quality["fok_fill_rate"] = fill_stats.get("fill_rate", None)
                exec_quality["fok_total_attempts"] = fill_stats.get("total_attempts", 0)
                exec_quality["fok_buy_fill_rate"] = round(
                    fill_stats.get("buy_fills", 0) / max(fill_stats.get("buy_attempts", 1), 1), 4)
            except Exception:
                pass
        # Slippage breakdown by spread and time-in-window (actionable for max_edge, logit_scale, kelly_fraction)
        try:
            exec_detail = self.bias_detector.analyze_execution_quality_detailed(all_outcomes)
            if exec_detail:
                exec_quality.update(exec_detail)
        except Exception as e:
            logger.debug(f"Execution quality detail failed: {e}")

        if exec_quality:
            analysis["execution_quality"] = exec_quality

        # Counterfactual analysis: how accurate are our scalp exits?
        cf_info: dict[str, Any] = {}
        if self.counterfactual_tracker:
            counterfactuals = self.counterfactual_tracker.load_all()
            if counterfactuals:
                cf_analysis = self.bias_detector.analyze_counterfactuals(counterfactuals)
                analysis["counterfactual_analysis"] = cf_analysis
                cf_info = {
                    "total": cf_analysis.get("total_scalps_tracked", 0),
                    "accuracy": cf_analysis.get("scalp_accuracy", 0),
                }
                pass  # rolled into [2/4] summary below
        pipeline_info["counterfactual"] = cf_info

        # Ghost trade analysis: which downstream gates are blocking profitable trades?
        ghost_tracker = getattr(self, 'ghost_tracker', None)
        if ghost_tracker:
            ghosts = ghost_tracker.load_all()
            resolved_ghosts = [g for g in ghosts if g.get("resolved", False)]
            if resolved_ghosts:
                analysis["ghost_analysis"] = self.bias_detector.analyze_ghosts(resolved_ghosts)
                pass  # rolled into [2/4] summary below

        # Platt re-fit. Gate: new fit must beat current on log-loss (full 7d pool) by ≥0.010
        # AND not hurt Kelly-Sharpe vs identity on holdout.
        platt_info: dict[str, Any] = {"decision": "skipped"}
        from polybot.agents.weight_optimizer import _sharpe
        from polybot.core.calibrator import PlattCalibrator, compute_log_loss
        MIN_PLATT_VALIDATION_TRADES = 50
        _pending_cal_save: PlattCalibrator | None = None
        _PLATT_WINDOW_DAYS = 7
        _platt_cutoff = datetime.now(timezone.utc).timestamp() - _PLATT_WINDOW_DAYS * 86400.0
        def _ts(o: dict) -> float:
            s = o.get("exit_timestamp", o.get("timestamp", "")) or ""
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0
        _platt_pool = [o for o in all_outcomes if _ts(o) >= _platt_cutoff]
        if len(_platt_pool) >= 125:
            _split = max(1, int(len(_platt_pool) * 0.6))
            platt_train = _platt_pool[:_split]
            platt_val = _platt_pool[_split:]
            logger.info(
                f"  Platt window: last {_PLATT_WINDOW_DAYS}d "
                f"({len(platt_train)} train / {len(platt_val)} val)"
            )
        else:
            platt_train = []
            platt_val = []
            platt_info["reason"] = (
                f"only {len(_platt_pool)} trades in last {_PLATT_WINDOW_DAYS}d (need 125) — skipping calibration"
            )
            logger.info(
                f"  Platt window: only {len(_platt_pool)} trades in last {_PLATT_WINDOW_DAYS}d — skipping calibration"
            )

        if len(platt_train) >= 75 and self.signal_engine:
            cal_probs: list[float] = []
            cal_outcomes: list[int] = []
            cal_weights: list[float] = []
            platt_now_ts = datetime.now(timezone.utc).timestamp()
            for o in platt_train:
                ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
                mp = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
                if mp <= 0:
                    continue
                cal_probs.append(mp)
                cal_outcomes.append(1 if o.get("correct", False) else 0)
                ts2 = o.get("exit_timestamp", o.get("timestamp", ""))
                try:
                    t2 = datetime.fromisoformat(ts2.replace("Z", "+00:00")).timestamp() if ts2 else platt_now_ts
                    w2 = 0.97 ** max(0.0, (platt_now_ts - t2) / 86400.0)
                except Exception:
                    w2 = 1.0
                cal_weights.append(w2)

            if len(cal_probs) >= 60:
                cal = PlattCalibrator()
                if self.signal_engine.calibrator:
                    cal.a = self.signal_engine.calibrator.a
                    cal.b = self.signal_engine.calibrator.b
                if cal.fit(cal_probs, cal_outcomes, min_samples=60, sample_weights=cal_weights):
                    # Log-loss on full 7-day pool (more data = more reliable calibration signal).
                    # Hierarchy: identity (no-cal) → current (live) → new (today's fit).
                    # Each tier should beat the one below it.
                    all_pool_probs, all_pool_outs = [], []
                    for o in _platt_pool:
                        ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
                        mp = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
                        if mp > 0:
                            all_pool_probs.append(mp)
                            all_pool_outs.append(1 if o.get("correct", False) else 0)

                    identity_loss = compute_log_loss(all_pool_probs, all_pool_outs) if all_pool_probs else float("nan")
                    new_loss_full = compute_log_loss([cal.calibrate(p) for p in all_pool_probs], all_pool_outs) if all_pool_probs else float("nan")
                    cur_cal = self.signal_engine.calibrator
                    if cur_cal and not getattr(cur_cal, 'is_identity', False):
                        current_loss = compute_log_loss([cur_cal.calibrate(p) for p in all_pool_probs], all_pool_outs) if all_pool_probs else float("nan")
                    else:
                        current_loss = identity_loss

                    # Kelly-Sharpe on holdout only (sizing sanity check).
                    cfg = self._config_for_helper()
                    helper_kwargs = dict(
                        outcomes=platt_val,
                        recommended_weights=cfg["weights"],
                        momentum_weight=cfg["momentum_weight"],
                        atr_sigma_ratio=cfg["atr_sigma_ratio"],
                        student_t_df=cfg["student_t_df"],
                        min_edge=cfg["min_edge"],
                        kelly_fraction=cfg["kelly_fraction"],
                        min_kelly=cfg["min_kelly"],
                        min_prob=cfg["min_model_probability"],
                        regime_weight=cfg["regime_weight"],
                        flow_weight=cfg["flow_weight"],
                        spot_flow_weight=cfg["spot_flow_weight"],
                        liquidation_weight=cfg["liquidation_weight"],
                        prev_margin_weight=cfg["prev_margin_weight"],
                        logit_scale=cfg["logit_scale"],
                        min_atr=cfg["min_atr"],
                    )
                    identity_returns = self._kelly_bankroll_returns(calibrator=None, **helper_kwargs)
                    new_returns = self._kelly_bankroll_returns(calibrator=cal, **helper_kwargs)
                    current_returns = self._kelly_bankroll_returns(calibrator=cur_cal, **helper_kwargs)
                    identity_sharpe = _sharpe(identity_returns)
                    new_sharpe = _sharpe(new_returns)
                    current_sharpe = _sharpe(current_returns)

                    LOG_LOSS_FLOOR = 0.010
                    platt_info = {
                        "identity_loss": round(identity_loss, 4) if all_pool_probs else None,
                        "current_loss": round(current_loss, 4) if all_pool_probs else None,
                        "new_loss": round(new_loss_full, 4) if all_pool_probs else None,
                        "identity_sharpe": round(identity_sharpe, 4),
                        "current_sharpe": round(current_sharpe, 4),
                        "new_sharpe": round(new_sharpe, 4),
                        "n_pool": len(all_pool_probs),
                        "n_val": len(new_returns),
                        "a": round(cal.a, 4),
                        "b": round(cal.b, 4),
                    }

                    insufficient = len(new_returns) < MIN_PLATT_VALIDATION_TRADES
                    # Gate 1: new fit must beat current on log-loss by ≥ 0.010
                    new_beats_current = (not (math.isnan(new_loss_full) or math.isnan(current_loss))
                                         and new_loss_full < current_loss - LOG_LOSS_FLOOR)
                    # Gate 2: new fit must not hurt sizing vs current (parallel structure to log-loss gate)
                    sizing_ok = new_sharpe >= current_sharpe
                    # Revert check: if current calibrator is worse than identity, revert
                    current_worse_than_identity = (not (math.isnan(current_loss) or math.isnan(identity_loss))
                                                    and current_loss > identity_loss
                                                    and cur_cal and not getattr(cur_cal, 'is_identity', False))

                    if insufficient:
                        platt_info["decision"] = "rejected"
                        platt_info["reason"] = f"only {len(new_returns)} validation trades (need {MIN_PLATT_VALIDATION_TRADES})"
                    elif current_worse_than_identity:
                        # Current calibrator hurts accuracy vs identity. Try the new fit first —
                        # it may already beat identity directly, skipping the revert step.
                        new_beats_identity_loss = (not (math.isnan(new_loss_full) or math.isnan(identity_loss))
                                                   and new_loss_full < identity_loss - LOG_LOSS_FLOOR)
                        new_beats_identity_sharpe = new_sharpe >= identity_sharpe
                        if new_beats_identity_loss and new_beats_identity_sharpe:
                            platt_info["decision"] = "adopted"
                            platt_info["reason"] = (f"current calibrator (loss={current_loss:.3f}) worse than identity "
                                                    f"(loss={identity_loss:.3f}); new fit beats identity — upgrading directly")
                            _pending_cal_save = cal
                            self.signal_engine.calibrator = cal
                            self._baseline_kelly_sharpe = None
                            self._baseline_n_trades = None
                            self._baseline_jk_se = None
                            logger.info(f"Platt adopted (bypassing bad current): loss {identity_loss:.4f} → {new_loss_full:.4f}, "
                                        f"sharpe {identity_sharpe:.4f} → {new_sharpe:.4f}")
                        else:
                            identity_cal = PlattCalibrator(a=-1.0, b=0.0)
                            _pending_cal_save = identity_cal
                            self.signal_engine.calibrator = identity_cal
                            platt_info["decision"] = "reverted"
                            platt_info["reason"] = (f"current calibrator (loss={current_loss:.3f}) worse than "
                                                    f"identity (loss={identity_loss:.3f}); new fit also doesn't beat identity — reverting")
                            logger.warning(f"Platt reverted to identity: current loss {current_loss:.4f} > identity {identity_loss:.4f}")
                    elif new_beats_current and sizing_ok:
                        # New fit beats current on accuracy AND doesn't hurt sizing — adopt
                        platt_info["decision"] = "adopted"
                        _pending_cal_save = cal
                        self.signal_engine.calibrator = cal
                        self._baseline_kelly_sharpe = None
                        self._baseline_n_trades = None
                        self._baseline_jk_se = None
                        logger.debug(f"Platt adopted: loss {current_loss:.4f} → {new_loss_full:.4f} "
                                     f"(identity {identity_loss:.4f}), sharpe {identity_sharpe:.4f} → {new_sharpe:.4f}")
                    elif new_beats_current and not sizing_ok:
                        platt_info["decision"] = "rejected"
                        platt_info["reason"] = (f"new fit improves accuracy (loss {current_loss:.3f}→{new_loss_full:.3f}) "
                                                f"but hurts sizing vs current ({new_sharpe:.3f} < {current_sharpe:.3f})")
                    else:
                        platt_info["decision"] = "rejected"
                        gap = current_loss - new_loss_full if not math.isnan(new_loss_full) else 0
                        platt_info["reason"] = (f"new fit doesn't beat current by enough "
                                                f"(loss gap {gap:+.4f}, need -{LOG_LOSS_FLOOR})")
        # Save rejected Platt fits so next cycle can skip identical ones quickly
        if platt_info.get("decision") == "rejected" and "a" in platt_info:
            _pr_path = _Path("polybot/memory/calibration/platt_rejected.json")
            try:
                _pr = _json.loads(_pr_path.read_text()) if _pr_path.exists() else []
                _pr.append({"a": platt_info["a"], "b": platt_info["b"]})
                _pr_path.write_text(_json.dumps(_pr[-30:], indent=2))  # keep last 30
            except Exception:
                pass

        pipeline_info["platt"] = platt_info
        # Expose Platt meta-check (raw-vs-calibrated) so Claude sees the diagnostic
        if platt_info.get("meta_warning"):
            analysis["platt_meta_warning"] = platt_info["meta_warning"]

        # SPRT aggregate evidence
        from polybot.agents.pipeline_analytics import aggregate_sprt_evidence, format_trends
        sprt_agg = aggregate_sprt_evidence(all_outcomes, recent_n=50)
        analysis["sprt_aggregate"] = sprt_agg
        pipeline_info["sprt"] = sprt_agg

        # Trends across the last ~5 buckets of recent trades — let Claude see whether
        # a metric is self-resolving (so it doesn't propose fixes for IMPROVING trends)
        trends_str = format_trends(all_outcomes, n_buckets=5, min_per_bucket=50)
        if trends_str:
            analysis["trends"] = trends_str

        # Current-regime snapshot: most recent 100 trades regardless of train/test split.
        # Claude only receives train_outcomes as its trade sample (first 60%), so without
        # this it can't see a regime shift that happened in the last few days.
        recent_window = all_outcomes[-100:] if len(all_outcomes) >= 100 else all_outcomes
        if recent_window:
            rw_gains = [o.get("gain_pct", 0) for o in recent_window]
            rw_wr = sum(1 for o in recent_window if o.get("correct", False)) / len(recent_window)
            rw_pnl = sum(o.get("pnl", 0) for o in recent_window)
            analysis["current_regime"] = {
                "n_trades": len(recent_window),
                "win_rate": round(rw_wr, 4),
                "total_pnl": round(rw_pnl, 4),
                "mean_gain_pct": round(sum(rw_gains) / len(rw_gains), 6) if rw_gains else 0,
                "note": "Most recent 100 trades (all_outcomes tail) — use to detect active regime shifts",
            }

        # Emit [2/4] analysis summary now that bias/calibration/shifts are all done
        _cf_acc = cf_info.get("accuracy", 0) if cf_info else None
        _cf_total = cf_info.get("total", 0) if cf_info else 0
        _shifts = pipeline_info.get("distribution_shifts", [])
        _gate_skips = pipeline_info.get("gate_total_skips", 0)
        _real_trades = [o for o in all_outcomes if not o.get("is_ghost")]
        _res_acc = (sum(1 for o in _real_trades if o.get("correct")) / len(_real_trades)) if _real_trades else None
        _analysis_parts = []
        if _res_acc is not None:
            _analysis_parts.append(f"resolution accuracy {_res_acc:.0%}")
        if _cf_total:
            _analysis_parts.append(f"scalp accuracy {_cf_acc:.0%} on {_cf_total:,}" if _cf_acc is not None else f"{_cf_total:,} scalps tracked")
        if _shifts:
            _analysis_parts.append(f"market shifts: {', '.join(_shifts)}")
        if _gate_skips:
            _analysis_parts.append(f"{_gate_skips:,} gate skips")
        logger.info(f"  [2/4] Analysis done" + (f"  |  {' | '.join(_analysis_parts)}" if _analysis_parts else ""))

        # Gate: need at least 200 trades before running TAEvolver and WeightOptimizer.
        MIN_TRADES_FOR_LEARNING = 200
        weight_info: dict[str, Any] = {"decision": "skipped"}
        if len(all_outcomes) < MIN_TRADES_FOR_LEARNING:
            logger.info(f"Skipping learning pipeline: only {len(all_outcomes)} trades, need {MIN_TRADES_FOR_LEARNING}")
            recommendations = {}
            weight_info["reason"] = f"only {len(all_outcomes)} trades (need {MIN_TRADES_FOR_LEARNING})"
        else:
            # Precompute baseline Sharpe/SE/N so Claude's context shows real numbers
            # instead of None on the first cycle after restart.
            self._precompute_baseline(all_outcomes)

            # Build Claude context including pipeline track record
            if self.pipeline_tracker:
                track_record = self.pipeline_tracker.format_for_claude()
                if track_record:
                    analysis["pipeline_track_record"] = track_record

            # Pass all_outcomes so Claude sees the current regime (last 40% includes recent
            # trades the train split excluded). The backtest is purely mathematical so
            # Claude seeing recent trades doesn't create lookahead in the adoption gate.
            recommendations = await self._run_ta_evolver(analysis, all_outcomes)
            source = recommendations.get("_pipeline_source", "local")
            pipeline_info["source"] = source
            # Manual-lever observations — evidence-backed suggestions for operator-only
            # params. These are never auto-applied; just surfaced in the summary table,
            # Discord alert, and strategy_log.md for the operator to review.
            pipeline_info["manual_observations"] = recommendations.get("manual_observations", []) or []

            # Crisis mode: when recent WR < 48% AND baseline Sharpe < 0.10, lower the
            # adoption floor so the pipeline keeps adapting instead of going silent.
            _recent_50 = all_outcomes[-50:] if len(all_outcomes) >= 50 else all_outcomes
            _recent_wr = sum(1 for o in _recent_50 if o.get("correct", False)) / max(len(_recent_50), 1)
            _in_crisis = _recent_wr < 0.48 and (self._baseline_kelly_sharpe or 0.0) < 0.10

            # Sustained crisis (≥3 cycles) → halve kelly_fraction, restore on first non-crisis.
            from pathlib import Path as _Path
            import json as _json
            _crisis_state_path = _Path("polybot/memory/crisis_state.json")
            _crisis_state = {"streak": 0, "kelly_reduced": False, "original_kelly": None}
            try:
                if _crisis_state_path.exists():
                    _crisis_state.update(_json.loads(_crisis_state_path.read_text()))
            except Exception:
                pass

            if _in_crisis:
                _crisis_state["streak"] = int(_crisis_state.get("streak", 0)) + 1
                pipeline_info["crisis_mode"] = True
                pipeline_info["crisis_streak"] = _crisis_state["streak"]
                logger.info(
                    f"Pipeline CRISIS MODE (streak={_crisis_state['streak']}): "
                    f"recent WR={_recent_wr:.1%}, Sharpe={self._baseline_kelly_sharpe:.3f}"
                )

                # Sustained crisis (3+ consecutive runs) → auto-reduce kelly_fraction.
                # Floor at 0.04 so we never disable sizing entirely.
                if _crisis_state["streak"] >= 3 and not _crisis_state.get("kelly_reduced") \
                        and self.signal_engine and self._config:
                    _orig = float(self.signal_engine.kelly_fraction)
                    _reduced = max(0.04, _orig * 0.5)
                    # Persist kelly_reduced BEFORE applying the cut so a crash
                    # mid-pipeline can't compound the halving on restart.
                    _crisis_state["original_kelly"] = _orig
                    _crisis_state["kelly_reduced"] = True
                    try:
                        _crisis_state_path.parent.mkdir(parents=True, exist_ok=True)
                        _crisis_state_path.write_text(_json.dumps(_crisis_state, indent=2))
                    except Exception as e:
                        logger.error(f"Auto Kelly reduction: failed to persist crisis_state: {e}")
                    self.signal_engine.kelly_fraction = _reduced
                    self._config.setdefault("math", {})["kelly_fraction"] = _reduced
                    try:
                        from polybot.config.loader import save_config
                        save_config(dict(self._config))
                    except Exception as e:
                        logger.error(f"Auto Kelly reduction: failed to persist: {e}")
                    logger.warning(
                        f"[AUTO KELLY REDUCTION] kelly_fraction {_orig:.3f} → {_reduced:.3f} "
                        f"after {_crisis_state['streak']} consecutive crisis cycles. "
                        f"Will restore on first non-crisis cycle."
                    )
                    pipeline_info["kelly_auto_reduced"] = True
            else:
                pipeline_info["crisis_mode"] = False

                # Recovery: if we previously auto-reduced kelly, restore it
                if _crisis_state.get("kelly_reduced") and _crisis_state.get("original_kelly") is not None \
                        and self.signal_engine and self._config:
                    _orig = float(_crisis_state["original_kelly"])
                    self.signal_engine.kelly_fraction = _orig
                    self._config.setdefault("math", {})["kelly_fraction"] = _orig
                    try:
                        from polybot.config.loader import save_config
                        save_config(dict(self._config))
                    except Exception as e:
                        logger.error(f"Auto Kelly restore: failed to persist: {e}")
                    logger.info(
                        f"[AUTO KELLY RESTORE] kelly_fraction restored to {_orig:.3f} "
                        f"after crisis ended (was reduced for {_crisis_state.get('streak', 0)} cycles)."
                    )
                    pipeline_info["kelly_auto_restored"] = True

                _crisis_state = {"streak": 0, "kelly_reduced": False, "original_kelly": None}

            try:
                _crisis_state_path.parent.mkdir(parents=True, exist_ok=True)
                _crisis_state_path.write_text(_json.dumps(_crisis_state, indent=2))
            except Exception as e:
                logger.debug(f"Failed to persist crisis_state: {e}")

            weight_info = await self._run_weight_optimizer(recommendations, all_outcomes, pipeline_source=source)
            # Commit point: weight optimizer has persisted its config changes,
            # so it's now safe to flush the Platt calibrator to disk. Doing
            # this AFTER weights means a crash at any earlier step leaves the
            # old Platt + old weights on disk (coherent) rather than the new
            # Platt + old weights (mismatched).
            if _pending_cal_save is not None:
                try:
                    _pending_cal_save.save()
                except Exception as e:
                    logger.error(f"Failed to persist Platt calibrator: {e}")
        pipeline_info["weights"] = weight_info

        # All-time stats
        all_gains = [o.get("gain_pct", 0) for o in all_outcomes]
        all_pnl = sum(o.get("pnl", 0) for o in all_outcomes)
        all_wins = sum(1 for o in all_outcomes if o.get("correct", False))
        if all_gains:
            avg_g = sum(all_gains) / len(all_gains)
            var_g = sum((r - avg_g) ** 2 for r in all_gains) / len(all_gains) if len(all_gains) > 1 else 1
            std_g = math.sqrt(var_g) if var_g > 0 else 1
            all_sharpe = avg_g / std_g if std_g > 0 else 0
        else:
            all_sharpe = 0
        pipeline_info["all_time"] = {
            "total_trades": len(all_outcomes),
            "win_rate": round(all_wins / len(all_outcomes), 4) if all_outcomes else 0,
            "sharpe": round(all_sharpe, 4),
            "total_pnl": round(all_pnl, 2),
        }

        # Current config snapshot (post-pipeline values)
        if self.signal_engine:
            pipeline_info["current_config"] = {
                "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', _d("kelly_fraction")),
                "min_edge": getattr(self.signal_engine, 'min_edge', _d("min_edge")),
                "min_model_prob": getattr(self.signal_engine, 'min_model_probability', _d("min_model_probability")),
                "momentum_weight": getattr(self.signal_engine, 'momentum_weight', _d("momentum_weight")),
                "regime_weight": getattr(self.signal_engine, 'regime_weight', _d("regime_weight")),
                "flow_weight": getattr(self.signal_engine, 'flow_weight', _d("flow_weight")),
                "spot_flow_weight": getattr(self.signal_engine, 'spot_flow_weight', _d("spot_flow_weight")),
                "student_t_df": getattr(self.signal_engine, 'student_t_df', _d("student_t_df")),
                "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', _d("atr_sigma_ratio")),
                "exit_edge_threshold": self._exit_edge_threshold,
                "min_kelly": getattr(self.signal_engine, 'min_kelly', _d("min_kelly")),
            }

        # Compute config diff
        config_changes = {}
        if self.signal_engine and old_config:
            new_vals = {
                "min_edge": getattr(self.signal_engine, 'min_edge', _d("min_edge")),
                "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', _d("kelly_fraction")),
                "momentum_weight": getattr(self.signal_engine, 'momentum_weight', _d("momentum_weight")),
                "min_model_probability": getattr(self.signal_engine, 'min_model_probability', _d("min_model_probability")),
                "exit_edge_threshold": self._exit_edge_threshold,
                "min_time_remaining": self._min_time_remaining,
                "trading_start": self._trading_start,
                "trading_end": self._trading_end,
                "min_kelly": getattr(self.signal_engine, 'min_kelly', _d("min_kelly")),
                "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', _d("atr_sigma_ratio")),
            }
            for k, old_v in old_config.items():
                new_v = new_vals.get(k)
                if old_v != new_v:
                    config_changes[k] = {"old": old_v, "new": new_v}

        # Human-readable summary — logged AND used by Discord report.
        pipeline_info["summary_block"] = _format_pipeline_summary(pipeline_info)
        for line in pipeline_info["summary_block"].splitlines():
            logger.info(line)

        # Send daily report
        if self.alert_manager:
            try:
                await self.alert_manager.send_daily_report(
                    all_outcomes, analysis, recommendations, config_changes, pipeline_info)
            except Exception as e:
                logger.error(f"Failed to send daily report: {e}")

        logger.info("Daily learning pipeline complete")

    async def run_outcome_loop(self) -> None:
        """Periodic outcome review — outcomes are recorded inline by the trading loop.
        This loop exists for future periodic analysis tasks."""
        while self._running:
            await asyncio.sleep(self.outcome_interval_seconds)

    async def run_daily_loop(self) -> None:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        while self._running:
            now = datetime.now(ET)
            if now.hour == self.daily_pipeline_hour and self.daily_pipeline_minute <= now.minute < self.daily_pipeline_minute + 5:
                try:
                    await self.run_daily_pipeline()
                except Exception as e:
                    logger.error(f"Daily pipeline error: {e}")
                    if self.alert_manager:
                        await self.alert_manager.send_error(f"Daily pipeline failed: {e}")
                if self._auto_shutdown:
                    logger.info("PIPELINE COMPLETE — auto-shutdown enabled, exiting for restart cycle")
                    self._shutdown_requested = True
                    return
                await asyncio.sleep(3600)
            await asyncio.sleep(60)

    async def start(self) -> None:
        self._running = True
        logger.debug("Agent scheduler started")

    async def stop(self) -> None:
        self._running = False
        logger.debug("Agent scheduler stopped")
