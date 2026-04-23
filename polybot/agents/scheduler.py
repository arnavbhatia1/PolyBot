"""AgentScheduler: orchestrates the nightly learning pipeline (12:05 AM ET).

Runs BiasDetector, Platt calibration (with recency-weighted MLE), distribution shift
detection, SPRT aggregation, TA Evolver (Claude), and WeightOptimizer in sequence.
Adopts parameter changes only when they pass: z >= 1.0 (Jobson-Korkie), delta >=
min_improvement (0.02–0.05, SPRT-modulated), n >= 100 candidate trades, 3/4 walk-forward
folds positive, regime-stratified Sharpe check. 2-day cooldown after last adoption.
After ≥2 adoptions: combined backtest interaction check (backs out weakest if combined
Δ < 0.7 × sum of individual Δ).
"""
from __future__ import annotations

import asyncio
import math
import logging
from datetime import datetime, timezone
from typing import Any

from polybot.config.loader import save_config

logger = logging.getLogger(__name__)


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
            "momentum_weight": getattr(self.signal_engine, 'momentum_weight', 0.04),
            "regime_weight": getattr(self.signal_engine, 'regime_weight', 0.05),
            "flow_weight": getattr(self.signal_engine, 'flow_weight', 0.06),
            "student_t_df": getattr(self.signal_engine, 'student_t_df', 4),
            "min_edge": getattr(self.signal_engine, 'min_edge', 0.20),
            "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', 0.15),
            "min_model_probability": getattr(self.signal_engine, 'min_model_probability', 0.65),
            "exit_edge_threshold": getattr(self, '_exit_edge_threshold', -0.10),
            "min_time_remaining": getattr(self, '_min_time_remaining', 0),
            "trading_start_hour_et": self._trading_start[0] if self._trading_start else 0,
            "trading_end_hour_et": self._trading_end[0] if self._trading_end else 23,
            "trading_end_minute": self._trading_end[1] if self._trading_end else 59,
            "min_kelly": getattr(self.signal_engine, 'min_kelly', 0.015),
            "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', 1.7),
            "spot_flow_weight": getattr(self.signal_engine, 'spot_flow_weight', 0.04),
            "wall_weight": getattr(self.signal_engine, 'wall_weight', 0.05),
            "prev_margin_weight": getattr(self.signal_engine, 'prev_margin_weight', 0.02),
            "logit_scale": getattr(self.signal_engine, 'logit_scale', 4.0),
            "probability_compression": getattr(self.signal_engine, 'probability_compression', 1.0),
            "liquidation_weight": getattr(self.signal_engine, 'liquidation_weight', 0.03),
            "adverse_selection_threshold": (self._config or {}).get("signal", {}).get("adverse_selection_threshold", 0.65),
            "normal_fraction": (self._config or {}).get("entry_timing", {}).get("normal_fraction", 0.60),
            "late_max_penalty": (self._config or {}).get("entry_timing", {}).get("late_max_penalty", 0.60),
            "min_atr": getattr(self.signal_engine, 'min_atr', 8.0),
            "max_edge": getattr(self.signal_engine, 'max_edge', 0.20),
            "active_weights_version": getattr(self.indicator_engine, 'active_version', 'weights_v001')
                                      if self.indicator_engine else "weights_v001",
        }

        if hasattr(self, '_last_per_change_results') and self._last_per_change_results:
            analysis["last_per_change_results"] = self._last_per_change_results
        if hasattr(self, '_baseline_kelly_sharpe'):
            analysis["baseline_kelly_sharpe"] = self._baseline_kelly_sharpe
            # Compute the actual dynamic floor Claude's change must clear:
            #   delta >= max(min_improvement, 0.25 × JK_SE)
            abs_floor = self.weight_optimizer.min_improvement
            jk_se = getattr(self, '_baseline_jk_se', None)
            if jk_se is not None:
                dyn_floor = max(abs_floor, 0.25 * jk_se)
                analysis["baseline_jk_se"] = jk_se
                analysis["baseline_n_trades"] = getattr(self, '_baseline_n_trades', None)
                analysis["adoption_abs_floor"] = abs_floor
                analysis["adoption_dynamic_floor"] = round(dyn_floor, 4)
                analysis["adoption_target"] = round(self._baseline_kelly_sharpe + dyn_floor, 4)
            else:
                analysis["adoption_target"] = round(self._baseline_kelly_sharpe + abs_floor, 4)
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

        # Active adoptions table — which past proposals are currently LIVE, IN_COOLDOWN,
        # or ROLLED_BACK. Prevents Claude wasting proposal slots on cooldowned or reversed
        # params because it didn't know they were off-limits.
        if self.pipeline_tracker:
            try:
                from datetime import datetime as _dt, timezone as _tz
                now = _dt.now(_tz.utc)
                cooldown_set = self.pipeline_tracker.params_in_cooldown(cooldown_days=2.0)
                active_lines: list[str] = []
                rolled_lines: list[str] = []
                cooldown_lines: list[str] = []
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
                        elif param in cooldown_set:
                            cooldown_end_days = max(0.0, 2.0 - age_days)
                            cooldown_lines.append(
                                f"  {param}: {old_val}->{new_val} (adopted {age_days:.1f}d ago, "
                                f"IN_COOLDOWN ~{cooldown_end_days:.1f}d more) — LIVE"
                            )
                        else:
                            active_lines.append(
                                f"  {param}: {old_val}->{new_val} (adopted {age_days:.0f}d ago) "
                                f"{actual_str} vs pred={pred_delta:+.3f} — LIVE"
                            )
                # Also list cooldown params with no adoption in last 30d (edge case: older adoption still blocking)
                seen_in_sections = {ln.strip().split(":")[0] for ln in (active_lines + rolled_lines + cooldown_lines)}
                extra_cooldown = [p for p in cooldown_set if p not in seen_in_sections]

                sections_out: list[str] = []
                if active_lines:
                    sections_out.append("ACTIVE ADOPTIONS (last 30 days, currently LIVE):\n" + "\n".join(active_lines))
                if cooldown_lines:
                    sections_out.append("IN COOLDOWN (cannot re-propose):\n" + "\n".join(cooldown_lines))
                if rolled_lines:
                    sections_out.append("ROLLED BACK (adopted value no longer live):\n" + "\n".join(rolled_lines))
                if extra_cooldown:
                    sections_out.append("ALSO IN COOLDOWN: " + ", ".join(sorted(extra_cooldown)))
                if sections_out:
                    analysis["active_adoptions"] = "\n\n".join(sections_out)
            except Exception as e:
                logger.debug(f"Failed to build active adoptions table: {e}")

        recommendations = await self.ta_evolver.evolve(outcomes, analysis, current_config)
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
        regime_weight: float = 0.03,
        flow_weight: float = 0.04,
        spot_flow_weight: float = 0.04,
        wall_weight: float = 0.00,
        liquidation_weight: float = 0.03,
        prev_margin_weight: float = 0.02,
        logit_scale: float = 4.0,
        min_atr: float = 8.0,
        probability_compression: float = 1.0,
    ) -> list[float]:
        """Simulate Kelly-sized portfolio returns for a given config + calibrator.

        Replays the full 8-layer logit composition used in production (`signal_engine`):
        L1 Student-t CDF, L2 regime×direction, L3 CLOB flow (with 0.35-logit cap),
        L3b spot flow, L3c wall pressure (subtractive), L3e liquidation pressure,
        L5 previous-window margin, L4 indicator momentum — then Platt calibration,
        then side-specific edge, then production gates (edge/prob/Kelly), then
        ``kelly_fraction * full_kelly * gain_pct`` as the trade's bankroll-weighted return.

        L2's `direction` and autocorr aren't stored per trade; we approximate from
        ``regime_state`` and ``prev_resolution_margin`` sign. This is a known fidelity
        gap — the approximation applies symmetrically to baseline and candidate so
        *relative* Sharpe comparisons remain valid.
        """
        from scipy.stats import t as t_dist

        # logit_scale, min_atr, probability_compression passed as params (not read from engine)
        # so candidate values differ from baseline in the backtest
        max_flow_logit = 0.35  # same multicollinearity cap signal_engine uses
        realism_factor = 1.0
        if self._config:
            realism_factor = float(self._config.get("execution", {}).get("backtest_realism_factor", 1.0))
        # Recency weighting: more recent trades carry more weight in the Sharpe estimate.
        # Decay 0.995/day ≈ 50% half-life at 140 days. Applied symmetrically to baseline
        # and candidate so relative comparisons remain valid; both see the same weighting.
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
            iv_ratio = ctx.get("iv_ratio", 1.0)

            raw_prob_up = stored_raw
            if btc > 0 and strike > 0 and secs > 0 and student_t_df > 2:
                minutes = secs / 60.0
                vol = (atr / atr_sigma_ratio) * math.sqrt(minutes) * iv_ratio
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
            direction = 1.0 if prev_margin > 0 else (-1.0 if prev_margin < 0 else 0.0)
            logit_p += regime_factor * direction * (regime_weight * logit_scale)

            # L3 — CLOB flow (with the production multicollinearity cap on the total flow adj).
            logit_before_flow = logit_p
            flow_signal = ctx.get("flow_score", 0.0)
            logit_p += flow_signal * (flow_weight * logit_scale)
            # L3b — spot flow
            spot_flow = ctx.get("spot_flow_signal", 0.0)
            logit_p += spot_flow * (spot_flow_weight * logit_scale)
            # L3c — wall pressure is SUBTRACTED (positive wall = bearish for Up)
            wall = ctx.get("wall_pressure", 0.0)
            logit_p -= wall * (wall_weight * logit_scale)
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
            # Apply probability compression (shrink toward 0.5)
            if probability_compression < 1.0:
                prob_up_adj = 0.5 + (prob_up_adj - 0.5) * probability_compression
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

            # Recency weight: recent trades count more (0.995^days_ago decay)
            ts_str = o.get("exit_timestamp", o.get("timestamp", ""))
            try:
                trade_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() if ts_str else now_ts
                days_ago = max(0.0, (now_ts - trade_ts) / 86400.0)
            except Exception:
                days_ago = 0.0
            recency_w = 0.995 ** days_ago
            returns.append(kelly_frac * o.get("gain_pct", 0.0) * realism_factor * recency_w)

        return returns

    def _config_for_helper(self, recommendations: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resolve config for `_kelly_bankroll_returns` — recommendation first, live engine fallback.

        Entry gates (min_model_probability, min_edge, min_kelly) are now pipeline-tunable:
        the backtest sample includes resolved ghosts (trades rejected at live gates), so
        raising or lowering any gate filters both baseline and candidate identically and
        the comparison stays clean.
        """
        rec = recommendations or {}
        live_weights = self.indicator_engine.get_weights() if self.indicator_engine else {}
        return {
            "weights": rec.get("recommended_weights") or {
                k: live_weights.get(k, 0.0) for k in ("rsi", "macd", "stochastic", "obv", "vwap")
            },
            "momentum_weight": rec.get("recommended_momentum_weight",
                getattr(self.signal_engine, 'momentum_weight', -0.02)),
            "atr_sigma_ratio": rec.get("recommended_atr_sigma_ratio",
                getattr(self.signal_engine, 'atr_sigma_ratio', 1.4)),
            "student_t_df": int(rec.get("recommended_student_t_df",
                getattr(self.signal_engine, 'student_t_df', 5))),
            # Entry gates — candidate-overridable since the backtest includes ghosts
            # (resolved rejections) alongside real fills.
            "min_edge": rec.get("recommended_min_edge",
                getattr(self.signal_engine, 'min_edge', 0.04)),
            "min_kelly": rec.get("recommended_min_kelly",
                getattr(self.signal_engine, 'min_kelly', 0.015)),
            "min_model_probability": rec.get("recommended_min_model_probability",
                getattr(self.signal_engine, 'min_model_probability', 0.58)),
            "kelly_fraction": rec.get("recommended_kelly_fraction",
                getattr(self.signal_engine, 'kelly_fraction', 0.15)),
            "regime_weight": rec.get("recommended_regime_weight",
                getattr(self.signal_engine, 'regime_weight', 0.03)),
            "flow_weight": rec.get("recommended_flow_weight",
                getattr(self.signal_engine, 'flow_weight', 0.04)),
            "spot_flow_weight": rec.get("recommended_spot_flow_weight",
                getattr(self.signal_engine, 'spot_flow_weight', 0.04)),
            "wall_weight": rec.get("recommended_wall_weight",
                getattr(self.signal_engine, 'wall_weight', 0.0)),
            "liquidation_weight": rec.get("recommended_liquidation_weight",
                getattr(self.signal_engine, 'liquidation_weight', 0.03)),
            "prev_margin_weight": rec.get("recommended_prev_margin_weight",
                getattr(self.signal_engine, 'prev_margin_weight', 0.02)),
            "logit_scale": rec.get("recommended_logit_scale",
                getattr(self.signal_engine, 'logit_scale', 4.0)),
            "min_atr": rec.get("recommended_min_atr",
                getattr(self.signal_engine, 'min_atr', 8.0)),
            "probability_compression": rec.get("recommended_probability_compression",
                getattr(self.signal_engine, 'probability_compression', 1.0)),
        }

    def _backtest_recommendations(self, recommendations: dict[str, Any],
                                    outcomes: list[dict[str, Any]]) -> list[float]:
        """Kelly-sized portfolio returns under candidate recommendations.

        Uses the currently adopted calibrator (``self.signal_engine.calibrator``) — Platt is
        frozen for the duration of a weight backtest, preventing calibration/weight
        co-optimization oscillation. Sharpe of the returned list is candidate_sharpe for
        adoption testing.
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
            wall_weight=cfg["wall_weight"],
            liquidation_weight=cfg["liquidation_weight"],
            prev_margin_weight=cfg["prev_margin_weight"],
            logit_scale=cfg["logit_scale"],
            min_atr=cfg["min_atr"],
            probability_compression=cfg["probability_compression"],
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

        # Build a thin recommendations dict for _config_for_helper
        single_rec: dict[str, Any] = {}
        if param == "weights":
            single_rec["recommended_weights"] = value
        elif param in (
            "momentum_weight", "atr_sigma_ratio", "student_t_df", "kelly_fraction",
            "regime_weight", "flow_weight", "spot_flow_weight", "wall_weight",
            "liquidation_weight", "prev_margin_weight", "logit_scale", "min_atr",
            "probability_compression",
            "min_edge", "min_kelly", "min_model_probability",
        ):
            single_rec[f"recommended_{param}"] = value
        else:
            # Params not plumbed through _config_for_helper (timing/exit/etc.) —
            # still attempt to backtest by re-using current config unchanged; the
            # change will be applied at hot-swap time if adopted.
            pass

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
            wall_weight=cfg["wall_weight"],
            liquidation_weight=cfg["liquidation_weight"],
            prev_margin_weight=cfg["prev_margin_weight"],
            logit_scale=cfg["logit_scale"],
            min_atr=cfg["min_atr"],
            probability_compression=cfg["probability_compression"],
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

        improved = sum(
            1 for r in populated
            if candidate_by_regime[r] > baseline_by_regime[r]
        )
        regressed_hard = [
            r for r in populated
            if baseline_by_regime[r] - candidate_by_regime[r] > 0.10
        ]

        # Single gate: dominant regime must improve AND no regime may degrade >0.10 Sharpe.
        # (The former "≥2/3 regimes improve" alternative was weaker evidence and correlated
        # with fold consistency — dropped to simplify false-negative stacking.)
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

        current_version = self.weight_optimizer.get_best_version()
        info["old_version"] = current_version

        # Get the changes list (new format) or fall back to checking for recommended_weights
        changes_list: list[dict[str, Any]] = recommendations.get("changes", [])

        if not changes_list:
            info["reason"] = "no changes proposed by evolver"
            return info

        def _clamp(val, lo, hi): return max(lo, min(hi, val))

        # --- Compute baseline once across all folds ---
        n = len(all_outcomes)
        fold_boundaries = [0.60, 0.70, 0.80, 0.90, 1.0]
        all_current_returns: list[float] = []
        baseline_request: dict[str, Any] = {}  # empty -> helper uses current engine state

        for i in range(len(fold_boundaries) - 1):
            start_idx = int(n * fold_boundaries[i])
            end_idx = int(n * fold_boundaries[i + 1])
            fold_test = all_outcomes[start_idx:end_idx]
            if len(fold_test) < 3:
                continue
            current_fold_returns = self._backtest_recommendations(baseline_request, fold_test)
            all_current_returns.extend(current_fold_returns)

        current_sharpe = _sharpe(all_current_returns) if all_current_returns else 0.0
        current_win_rate = (sum(1 for r in all_current_returns if r > 0) / len(all_current_returns)
                            if all_current_returns else 0.0)
        if all_current_returns:
            self.weight_optimizer.record_score(current_version, current_sharpe,
                                               len(all_current_returns), current_win_rate)
        info["old_sharpe"] = round(current_sharpe, 4)
        info["n_baseline_trades"] = len(all_current_returns)

        # Compute baseline JK SE for Claude's adoption-target context.
        # Stored so build_claude_context can inject it into next cycle's context.
        from polybot.agents.weight_optimizer import _lag1_autocorr as _ac
        n_base = len(all_current_returns)
        if n_base >= 2:
            base_se = math.sqrt((1.0 + 0.5 * current_sharpe ** 2) / n_base)
            if n_base >= 3:
                rho = _ac(all_current_returns)
                base_se *= math.sqrt(1.0 + 2.0 * max(0.0, rho))
            self._baseline_jk_se = round(base_se, 4)
            self._baseline_n_trades = n_base

        # --- Per-change walk-forward backtests ---
        adopted_changes: list[dict[str, Any]] = []
        any_adopted = False

        # Per-parameter cooldown: skip any param adopted in the last 2 days.
        # Prevents the same knob from being driven in one direction across
        # consecutive runs without live data validating the previous adoption.
        cooldown_params: set[str] = set()
        if self.pipeline_tracker:
            try:
                cooldown_params = self.pipeline_tracker.params_in_cooldown(cooldown_days=2.0)
            except Exception as e:
                logger.debug(f"Failed to compute per-param cooldown: {e}")

        for change in changes_list[:5]:
            param = change.get("param", "")
            value = change.get("value")
            reason_str = change.get("reason", "")
            change_info: dict[str, Any] = {"param": param, "value": value}

            # Per-param cooldown check
            if param in cooldown_params:
                msg = f"param cooldown (adopted within last 2 days)"
                change_info.update({"decision": "rejected", "reason": msg})
                logger.info(f"SKIPPED {param}: {msg}")
                info["per_change"].append(change_info)
                continue

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
                logger.info(f"REJECTED {param}: {msg}")
                info["per_change"].append(change_info)
                continue

            candidate_sharpe = _sharpe(all_candidate_returns)
            candidate_win_rate = sum(1 for r in all_candidate_returns if r > 0) / len(all_candidate_returns)
            change_info.update({
                "candidate_sharpe": round(candidate_sharpe, 4),
                "candidate_win_rate": round(candidate_win_rate, 4),
                "fold_sharpes": [round(s, 4) for s in fold_sharpes],
                "n_candidate_trades": len(all_candidate_returns),
            })

            adopt, adopt_reason = self.weight_optimizer.should_adopt(
                current_sharpe, candidate_sharpe,
                n_trades=len(all_candidate_returns),
                fold_sharpes=fold_sharpes,
                candidate_returns=all_candidate_returns,
            )

            # Regime-stratified check: a change that passes aggregate stats
            # but hurts a specific regime is likely overfitting to the dominant sample.
            if adopt and all_outcomes:
                regime_ok, regime_reason = self._check_regime_adoption(change, all_outcomes, current_sharpe)
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
                logger.info(f"ADOPTED {param}: {old_val_str}{value} ({adopt_reason}, n={n_trades} candidates, baseline={current_sharpe:.3f}, candidate={candidate_sharpe:.3f})")
            else:
                change_info.update({"decision": "rejected", "reason": adopt_reason})
                n_trades = len(all_candidate_returns)
                logger.info(f"REJECTED {param}: {value} — {adopt_reason} (n={n_trades} candidates, baseline={current_sharpe:.3f}, candidate={candidate_sharpe:.3f})")

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
        # If ≥2 changes were adopted, run ONE combined backtest.
        # If combined Δ < sum(individual Δ) × 0.7, the changes interact and one is riding
        # on the other's signal. Back out the lowest-conviction change (smallest z-score).
        #
        # Coupled parameter groups — any two params in the SAME group interact through
        # shared signal composition (L1 sharpness, L3 flow, L2/L4 momentum, L4 sizing).
        # Config-driven: `pipeline_groups` in settings.yaml overrides these defaults.
        DEFAULT_COUPLED_GROUPS: dict[str, list[str]] = {
            "volatility_core": ["atr_sigma_ratio", "student_t_df", "logit_scale"],
            "flow_stack": ["flow_weight", "spot_flow_weight", "liquidation_weight"],
            "momentum_regime": ["momentum_weight", "regime_weight"],
            "sizing": ["kelly_fraction", "probability_compression"],
        }
        coupled_groups: dict[str, list[str]] = DEFAULT_COUPLED_GROUPS
        if self._config:
            cfg_groups = self._config.get("pipeline_groups")
            if isinstance(cfg_groups, dict):
                coupled_groups = cfg_groups
        # Expand groups to all within-group pairs for the "known interaction" logger.
        KNOWN_INTERACTING_PAIRS: list[tuple[str, str]] = []
        for members in coupled_groups.values():
            for i, p1 in enumerate(members):
                for p2 in members[i + 1:]:
                    KNOWN_INTERACTING_PAIRS.append((p1, p2))
        if len(adopted_changes) >= 2:
            try:
                combined_rec: dict[str, Any] = {}
                sum_individual_delta = 0.0
                for c in adopted_changes:
                    param = c["param"]
                    value = c["value"]
                    if param == "weights":
                        combined_rec["recommended_weights"] = value
                    elif param in (
                        "momentum_weight", "atr_sigma_ratio", "student_t_df", "kelly_fraction",
                        "regime_weight", "flow_weight", "spot_flow_weight", "wall_weight",
                        "liquidation_weight", "prev_margin_weight", "logit_scale", "min_atr",
                        "probability_compression",
                    ):
                        combined_rec[f"recommended_{param}"] = value
                    ci = next((x for x in info["per_change"]
                               if x.get("param") == param and x.get("decision") == "adopted"), {})
                    sum_individual_delta += (ci.get("candidate_sharpe", current_sharpe) - current_sharpe)

                cfg_combined = self._config_for_helper(combined_rec)
                calibrator = self.signal_engine.calibrator if self.signal_engine else None
                combined_returns = self._kelly_bankroll_returns(
                    outcomes=all_outcomes,
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
                    wall_weight=cfg_combined["wall_weight"],
                    liquidation_weight=cfg_combined["liquidation_weight"],
                    prev_margin_weight=cfg_combined["prev_margin_weight"],
                    logit_scale=cfg_combined["logit_scale"],
                    min_atr=cfg_combined["min_atr"],
                    probability_compression=cfg_combined["probability_compression"],
                )
                combined_sharpe = _sharpe(combined_returns) if combined_returns else 0.0
                combined_delta = combined_sharpe - current_sharpe
                info["combined_sharpe"] = round(combined_sharpe, 4)
                info["combined_delta"] = round(combined_delta, 4)
                info["sum_individual_delta"] = round(sum_individual_delta, 4)

                if sum_individual_delta > 0 and combined_delta < sum_individual_delta * 0.7:
                    # Interaction detected — back out the weakest change
                    z_scores = {}
                    for c in info["per_change"]:
                        if c.get("decision") == "adopted":
                            reason_str = c.get("reason", "")
                            try:
                                z_val = float(reason_str.split("z=")[1].split()[0]) if "z=" in reason_str else 0.0
                            except (IndexError, ValueError):
                                z_val = 0.0
                            z_scores[c["param"]] = z_val
                    weakest_param = min(z_scores, key=z_scores.get) if z_scores else None
                    if weakest_param:
                        adopted_changes = [c for c in adopted_changes if c["param"] != weakest_param]
                        for c in info["per_change"]:
                            if c.get("param") == weakest_param and c.get("decision") == "adopted":
                                c["decision"] = "backed_out"
                                c["reason"] = (
                                    f"interaction detected: combined Δ={combined_delta:+.3f} < "
                                    f"sum_individual Δ={sum_individual_delta:+.3f} × 0.7 — "
                                    f"weakest change (z={z_scores[weakest_param]:.2f}) removed"
                                )
                        info["interaction_detected"] = True
                        info["backed_out_param"] = weakest_param
                        logger.info(
                            f"Interaction detected: combined Δ={combined_delta:+.3f} vs "
                            f"sum_individual Δ={sum_individual_delta:+.3f}. "
                            f"Backing out {weakest_param} (z={z_scores.get(weakest_param, 0):.2f})"
                        )

                # Flag known interacting pairs for Claude's awareness
                adopted_params = {c["param"] for c in adopted_changes}
                for p1, p2 in KNOWN_INTERACTING_PAIRS:
                    if p1 in adopted_params and p2 in adopted_params:
                        logger.info(f"Known interacting pair adopted: {p1} + {p2}")
            except Exception as e:
                logger.debug(f"Combined backtest failed (non-critical): {e}")

        if not adopted_changes:
            info["decision"] = "no_change"
            info["reason"] = "all changes backed out (interactions)"
            return info

        # --- Apply and persist all adopted changes ---
        info["decision"] = "adopted"
        info["adopted_params"] = [c["param"] for c in adopted_changes]

        # Determine new weights version (needed if any weights change was adopted)
        weights_change = next((c for c in adopted_changes if c["param"] == "weights"), None)
        new_weights: dict[str, Any] = {}
        new_version = current_version

        if weights_change:
            new_version = self.weight_optimizer.get_next_version()
            new_weights = dict(weights_change["value"])
            new_weights["version"] = new_version
            self.weight_optimizer.save_weights(new_version, new_weights)
            if self.indicator_engine:
                self.indicator_engine.set_active_version(new_version)
            info["new_version"] = new_version

        if self.signal_engine:
            for change in adopted_changes:
                param = change["param"]
                value = change["value"]
                if param == "weights":
                    self.signal_engine.weights = {k: v for k, v in new_weights.items()
                                                   if k in ["rsi", "macd", "stochastic", "obv", "vwap"]}
                elif param == "momentum_weight":
                    self.signal_engine.momentum_weight = _clamp(float(value), -0.10, 0.10)
                elif param == "regime_weight":
                    self.signal_engine.regime_weight = _clamp(float(value), 0.02, 0.10)
                elif param == "flow_weight":
                    self.signal_engine.flow_weight = _clamp(float(value), 0.02, 0.12)
                elif param == "student_t_df":
                    self.signal_engine.student_t_df = _clamp(int(value), 3, 8)
                elif param == "kelly_fraction":
                    self.signal_engine.kelly_fraction = _clamp(float(value), 0.05, 0.25)
                elif param == "exit_edge_threshold":
                    self._exit_edge_threshold = _clamp(float(value), -0.25, 0.0)
                elif param == "atr_sigma_ratio":
                    self.signal_engine.atr_sigma_ratio = _clamp(float(value), 1.2, 2.5)
                elif param == "spot_flow_weight":
                    self.signal_engine.spot_flow_weight = _clamp(float(value), 0.0, 0.10)
                elif param == "wall_weight":
                    self.signal_engine.wall_weight = _clamp(float(value), 0.0, 0.15)
                elif param == "prev_margin_weight":
                    self.signal_engine.prev_margin_weight = _clamp(float(value), 0.01, 0.05)
                elif param == "logit_scale":
                    self.signal_engine.logit_scale = _clamp(float(value), 2.0, 6.0)
                elif param == "probability_compression":
                    self.signal_engine.probability_compression = _clamp(float(value), 0.5, 1.0)
                elif param == "liquidation_weight":
                    self.signal_engine.liquidation_weight = _clamp(float(value), 0.01, 0.06)
                elif param == "min_atr":
                    self.signal_engine.min_atr = _clamp(float(value), 5.0, 15.0)
                elif param == "max_edge":
                    self.signal_engine.max_edge = _clamp(float(value), 0.10, 0.30)
                elif param == "adverse_selection_threshold":
                    if self._config:
                        self._config.setdefault("signal", {})["adverse_selection_threshold"] = _clamp(float(value), 0.45, 0.75)
                elif param == "normal_fraction":
                    if self._config:
                        self._config.setdefault("entry_timing", {})["normal_fraction"] = _clamp(float(value), 0.40, 0.80)
                elif param == "late_max_penalty":
                    if self._config:
                        self._config.setdefault("entry_timing", {})["late_max_penalty"] = _clamp(float(value), 0.20, 0.80)
                elif param == "trading_start_hour_et":
                    self._trading_start = (int(value), 0)
                elif param == "trading_end_hour_et":
                    self._trading_end = (int(value), 59)

        # Persist to settings.yaml
        if self._config:
            sig = self._config.setdefault("signal", {})
            mkt = self._config.setdefault("market", {})
            sched = self._config.setdefault("schedule", {})
            math_sec = self._config.setdefault("math", {})

            if weights_change:
                sig["active_weights_version"] = new_version
                sig["weights"] = {k: v for k, v in new_weights.items()
                                   if k in ["rsi", "macd", "stochastic", "obv", "vwap"]}

            for change in adopted_changes:
                param = change["param"]
                value = change["value"]
                if param == "weights":
                    pass  # handled above
                elif param == "atr_sigma_ratio":
                    sig["atr_sigma_ratio"] = _clamp(float(value), 1.2, 2.5)
                elif param == "spot_flow_weight":
                    sig["spot_flow_weight"] = _clamp(float(value), 0.0, 0.10)
                elif param == "wall_weight":
                    sig["wall_weight"] = _clamp(float(value), 0.0, 0.15)
                elif param == "prev_margin_weight":
                    sig["prev_margin_weight"] = _clamp(float(value), 0.01, 0.05)
                elif param == "logit_scale":
                    sig["logit_scale"] = _clamp(float(value), 2.0, 6.0)
                elif param == "probability_compression":
                    sig["probability_compression"] = _clamp(float(value), 0.5, 1.0)
                elif param == "liquidation_weight":
                    sig["liquidation_weight"] = _clamp(float(value), 0.01, 0.06)
                elif param == "adverse_selection_threshold":
                    sig["adverse_selection_threshold"] = _clamp(float(value), 0.45, 0.75)
                elif param == "normal_fraction":
                    self._config.setdefault("entry_timing", {})["normal_fraction"] = _clamp(float(value), 0.40, 0.80)
                elif param == "late_max_penalty":
                    self._config.setdefault("entry_timing", {})["late_max_penalty"] = _clamp(float(value), 0.20, 0.80)
                elif param == "min_atr":
                    sig["min_atr"] = _clamp(float(value), 5.0, 15.0)
                elif param == "max_edge":
                    sig["max_edge"] = _clamp(float(value), 0.10, 0.30)
                elif param == "momentum_weight":
                    sig["momentum_weight"] = _clamp(float(value), -0.10, 0.10)
                elif param == "regime_weight":
                    sig["regime_weight"] = _clamp(float(value), 0.02, 0.10)
                elif param == "flow_weight":
                    sig["flow_weight"] = _clamp(float(value), 0.02, 0.12)
                elif param == "student_t_df":
                    sig["student_t_df"] = _clamp(int(value), 3, 8)
                elif param == "kelly_fraction":
                    math_sec["kelly_fraction"] = _clamp(float(value), 0.05, 0.25)
                elif param == "exit_edge_threshold":
                    sig["exit_edge_threshold"] = _clamp(float(value), -0.25, 0.0)
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
                version=new_version if new_version != current_version else f"{current_version}+params",
                baseline_sharpe=current_sharpe,
                predicted_sharpe=best_candidate_sharpe,
                changes=tracker_changes,
                reason=f"{len(adopted_changes)} change(s) adopted",
                run_predicted_delta=run_predicted_delta,
            )

        return info

    async def run_daily_pipeline(self) -> None:
        logger.info("Starting daily learning pipeline")

        pipeline_info: dict[str, Any] = {}

        # Snapshot current config before changes
        old_config = {}
        if self.signal_engine:
            old_config = {
                "min_edge": getattr(self.signal_engine, 'min_edge', 0.20),
                "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', 0.15),
                "momentum_weight": getattr(self.signal_engine, 'momentum_weight', 0.08),
                "min_model_probability": getattr(self.signal_engine, 'min_model_probability', 0.65),
                "exit_edge_threshold": self._exit_edge_threshold,
                "min_time_remaining": self._min_time_remaining,
                "trading_start": self._trading_start,
                "trading_end": self._trading_end,
                "min_kelly": getattr(self.signal_engine, 'min_kelly', 0.015),
                "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', 1.7),
            }

        # Walk-forward validation: train on first 60%, validate across 4 expanding
        # folds of the remaining 40% (each fold is genuinely out-of-sample).
        rolled = self.outcome_reviewer.rollup_old_outcomes()
        if rolled:
            logger.info(f"Daily rollup: consolidated {rolled} outcome files")
        if self.ghost_tracker:
            ghost_rolled = self.ghost_tracker.rollup_old_ghosts()
            if ghost_rolled:
                logger.info(f"Daily rollup: consolidated {ghost_rolled} ghost files")
        if self.counterfactual_tracker:
            cf_rolled = self.counterfactual_tracker.rollup_old_counterfactuals()
            if cf_rolled:
                logger.info(f"Daily rollup: consolidated {cf_rolled} counterfactual files")
        # Merge real outcomes + resolved ghosts so the backtest sample contains both
        # trades that fired AND trades rejected at entry gates. Required for
        # min_edge / min_model_probability / min_kelly to be pipeline-tunable:
        # without ghosts, raising/lowering a gate would asymmetrically filter trades.
        all_outcomes = self._load_combined_outcomes()
        n_ghosts = sum(1 for o in all_outcomes if o.get("is_ghost"))
        split_idx = max(1, int(len(all_outcomes) * 0.6))
        train_outcomes = all_outcomes[:split_idx]
        validation_outcomes = all_outcomes[split_idx:]  # used for Platt holdout validation
        logger.info(f"Walk-forward split: {len(train_outcomes)} train / {len(validation_outcomes)} validation "
                    f"(4 folds, {len(all_outcomes)} total incl. {n_ghosts} resolved ghosts)")
        pipeline_info["total_outcomes"] = len(all_outcomes)
        pipeline_info["train_count"] = len(train_outcomes)
        pipeline_info["validation_count"] = len(all_outcomes) - len(train_outcomes)

        # Review past pipeline adoptions (fill in actual 7d/30d Sharpe)
        if self.pipeline_tracker:
            self.pipeline_tracker.review_past_adoptions(all_outcomes)

        analysis = await self._run_bias_detector(train_outcomes)

        # Gate skip stats: how often did each entry gate fire?
        # Tells Claude which gates are over-filtering and whether adverse selection /
        # pre-submit drift / late-window guards are actually affecting trade count.
        from pathlib import Path as _Path
        import json as _json
        _gate_stats_path = _Path("polybot/memory/gate_stats.json")
        if _gate_stats_path.exists():
            try:
                gate_stats = _json.loads(_gate_stats_path.read_text())
                analysis["gate_skip_stats"] = gate_stats
                total = gate_stats.get("total_skips", 0)
                logger.info(f"Gate stats loaded: {total} total skips since last reset")
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
                logger.info(f"Counterfactual analysis: {cf_info['total']} scalps tracked, "
                           f"accuracy={cf_info['accuracy']:.0%}")
        pipeline_info["counterfactual"] = cf_info

        # Ghost trade analysis: which downstream gates are blocking profitable trades?
        ghost_tracker = getattr(self, 'ghost_tracker', None)
        if ghost_tracker:
            ghosts = ghost_tracker.load_all()
            resolved_ghosts = [g for g in ghosts if g.get("resolved", False)]
            if resolved_ghosts:
                analysis["ghost_analysis"] = self.bias_detector.analyze_ghosts(resolved_ghosts)
                logger.info(f"Ghost analysis: {len(resolved_ghosts)} resolved ghost trades")

        # Platt calibration fitting. Adoption is gated on Kelly-sized-Sharpe of validation
        # trades (matches production PnL dynamics), not log-loss. Log-loss kept for telemetry.
        platt_info: dict[str, Any] = {"decision": "skipped"}
        from polybot.agents.weight_optimizer import _sharpe, _sharpe_z_test
        from polybot.core.calibrator import PlattCalibrator, compute_log_loss
        # Raised from 20 — Sharpe on 20 samples has huge variance, which meant small-
        # sample noise was driving some Platt adoptions. 50 pushes the noise floor down.
        MIN_PLATT_VALIDATION_TRADES = 50
        PLATT_Z_FLOOR = 1.0  # additional gate: require statistically non-trivial improvement
        if len(train_outcomes) >= 200 and self.signal_engine:
            cal_probs = []
            cal_outcomes = []
            for o in train_outcomes:
                ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
                mp = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
                if mp > 0:
                    cal_probs.append(mp)
                    cal_outcomes.append(1 if o.get("correct", False) else 0)

            if len(cal_probs) >= 200:
                cal = PlattCalibrator()
                if self.signal_engine.calibrator:
                    cal.a = self.signal_engine.calibrator.a
                    cal.b = self.signal_engine.calibrator.b
                # Recency weights for Platt fitting — recent outcomes carry more signal
                platt_now_ts = datetime.now(timezone.utc).timestamp()
                cal_weights = []
                for o in train_outcomes:
                    ctx2 = o.get("indicator_snapshot", {}).get("trade_context", {})
                    if ctx2.get("model_probability_raw", ctx2.get("model_probability", 0)) <= 0:
                        continue
                    ts2 = o.get("exit_timestamp", o.get("timestamp", ""))
                    try:
                        t2 = datetime.fromisoformat(ts2.replace("Z", "+00:00")).timestamp() if ts2 else platt_now_ts
                        w2 = 0.995 ** max(0.0, (platt_now_ts - t2) / 86400.0)
                    except Exception:
                        w2 = 1.0
                    cal_weights.append(w2)
                if cal.fit(cal_probs, cal_outcomes, sample_weights=cal_weights):
                    val_probs, val_outs = [], []
                    for o in validation_outcomes:
                        ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
                        mp = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
                        if mp > 0:
                            val_probs.append(mp)
                            val_outs.append(1 if o.get("correct", False) else 0)

                    # Log-loss: telemetry only, no adoption power.
                    old_loss = compute_log_loss(val_probs, val_outs) if val_probs else float("nan")
                    new_loss = (compute_log_loss([cal.calibrate(p) for p in val_probs], val_outs)
                                if val_probs else float("nan"))

                    # Adoption gate: Kelly-sized-Sharpe on validation under current weights.
                    # Only the calibrator changes between old and new runs -> any Sharpe delta
                    # is attributable to calibration, not weight/asr/df drift.
                    cfg = self._config_for_helper()
                    kelly_fraction = getattr(self.signal_engine, 'kelly_fraction', 0.15)
                    min_kelly = getattr(self.signal_engine, 'min_kelly', 0.015)
                    min_prob = getattr(self.signal_engine, 'min_model_probability', 0.58)
                    helper_kwargs = dict(
                        outcomes=validation_outcomes,
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
                        wall_weight=cfg["wall_weight"],
                        liquidation_weight=cfg["liquidation_weight"],
                        prev_margin_weight=cfg["prev_margin_weight"],
                        logit_scale=cfg["logit_scale"],
                        min_atr=cfg["min_atr"],
                        probability_compression=cfg["probability_compression"],
                    )
                    old_returns = self._kelly_bankroll_returns(calibrator=self.signal_engine.calibrator, **helper_kwargs)
                    new_returns = self._kelly_bankroll_returns(calibrator=cal, **helper_kwargs)
                    # Meta-validation: raw-model (no Platt) Sharpe. If raw ≈ current,
                    # the calibrator isn't earning its keep — surface as a warning so
                    # the operator knows whether to simplify away Platt entirely.
                    raw_returns = self._kelly_bankroll_returns(calibrator=None, **helper_kwargs)
                    old_kelly_sharpe = _sharpe(old_returns)
                    new_kelly_sharpe = _sharpe(new_returns)
                    raw_kelly_sharpe = _sharpe(raw_returns)

                    platt_info = {
                        "old_loss": round(old_loss, 4) if val_probs else None,
                        "new_loss": round(new_loss, 4) if val_probs else None,
                        "old_kelly_sharpe": round(old_kelly_sharpe, 4),
                        "new_kelly_sharpe": round(new_kelly_sharpe, 4),
                        "raw_kelly_sharpe": round(raw_kelly_sharpe, 4),
                        "n_val_trades_old": len(old_returns),
                        "n_val_trades_new": len(new_returns),
                        "n_val_trades_raw": len(raw_returns),
                        "a": round(cal.a, 4),
                        "b": round(cal.b, 4),
                    }

                    # Meta-alert: raw within 5% of current means calibration isn't earning its keep
                    if old_kelly_sharpe > 0 and raw_kelly_sharpe >= 0.95 * old_kelly_sharpe:
                        platt_info["meta_warning"] = (
                            f"raw_sharpe {raw_kelly_sharpe:.4f} >= 0.95 x current_platt "
                            f"{old_kelly_sharpe:.4f} — calibrator may not be earning its keep"
                        )
                        logger.warning(
                            f"Platt meta-check: raw model Sharpe {raw_kelly_sharpe:.4f} is within 5% "
                            f"of current Platt {old_kelly_sharpe:.4f}. Consider dropping calibration."
                        )

                    insufficient = (len(old_returns) < MIN_PLATT_VALIDATION_TRADES
                                    or len(new_returns) < MIN_PLATT_VALIDATION_TRADES)
                    # Z-test of Sharpe improvement — gated in addition to `new > old`
                    # so small-sample noise doesn't drive adoption.
                    n_for_z = min(len(old_returns), len(new_returns))
                    z_score = _sharpe_z_test(old_kelly_sharpe, new_kelly_sharpe, n_for_z) if n_for_z else 0.0
                    platt_info["z_score"] = round(z_score, 3)
                    if insufficient:
                        platt_info["decision"] = "rejected"
                        platt_info["reason"] = (f"validation trades below {MIN_PLATT_VALIDATION_TRADES} "
                                                f"(old={len(old_returns)}, new={len(new_returns)})")
                        logger.info(
                            f"Platt calibration rejected: insufficient validation trades "
                            f"(old={len(old_returns)}, new={len(new_returns)})"
                        )
                    elif new_kelly_sharpe > old_kelly_sharpe and z_score >= PLATT_Z_FLOOR:
                        platt_info["decision"] = "adopted"
                        cal.save()
                        self.signal_engine.calibrator = cal
                        logger.info(
                            f"Platt calibration adopted: kelly_sharpe {old_kelly_sharpe:.4f} -> "
                            f"{new_kelly_sharpe:.4f} (z={z_score:.2f}, log-loss {old_loss:.4f} -> {new_loss:.4f})"
                        )
                    else:
                        platt_info["decision"] = "rejected"
                        reason = ("below z-floor" if new_kelly_sharpe > old_kelly_sharpe
                                  else "no Sharpe improvement")
                        platt_info["reason"] = f"{reason} (z={z_score:.2f}, need >= {PLATT_Z_FLOOR})"
                        logger.info(
                            f"Platt calibration rejected: kelly_sharpe {old_kelly_sharpe:.4f} -> "
                            f"{new_kelly_sharpe:.4f} (z={z_score:.2f}, log-loss {old_loss:.4f} -> {new_loss:.4f})"
                        )
        pipeline_info["platt"] = platt_info
        # Expose Platt meta-check (raw-vs-calibrated) so Claude sees the diagnostic
        if platt_info.get("meta_warning"):
            analysis["platt_meta_warning"] = platt_info["meta_warning"]

        # Distribution shift detection (recent 50 vs historical)
        from polybot.agents.pipeline_analytics import detect_distribution_shift, aggregate_sprt_evidence
        if len(all_outcomes) > 100:
            recent_50 = all_outcomes[-50:]
            historical = all_outcomes[:-50]
            shifts = detect_distribution_shift(recent_50, historical)
            if shifts:
                analysis["distribution_shifts"] = shifts
                pipeline_info["distribution_shifts"] = list(shifts.keys())
                logger.info(f"Distribution shifts detected: {list(shifts.keys())}")

        # SPRT aggregate evidence — modulates adoption urgency
        sprt_agg = aggregate_sprt_evidence(all_outcomes, recent_n=50)
        analysis["sprt_aggregate"] = sprt_agg
        pipeline_info["sprt"] = sprt_agg

        # (per-regime Platt stub removed — it only counted samples without fitting or
        # adopting anything. If we re-introduce regime-conditional calibration it should
        # use the same Kelly-sized-Sharpe adoption gate as the main Platt block above.)

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

            recommendations = await self._run_ta_evolver(analysis, train_outcomes)
            source = "claude" if recommendations.get("confidence") else "local"
            pipeline_info["source"] = source

            # Fixed absolute adoption floor. The noise-scaled term (0.25×JK_SE) dominates
            # at realistic N anyway; the prior SPRT modulation (0.015/0.020/0.035) was a
            # no-op that added explanation overhead. SPRT remains a diagnostic only.
            self.weight_optimizer.min_improvement = 0.020

            # Per-parameter cooldown is enforced inside _run_weight_optimizer:
            # any param adopted in the last 2 days is skipped individually; other
            # params adopt normally. No global pipeline-wide cooldown.
            weight_info = await self._run_weight_optimizer(recommendations, all_outcomes, pipeline_source=source)
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
                "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', 0.15),
                "entry_threshold": getattr(self.signal_engine, 'min_edge', 0.04),
                "min_model_prob": getattr(self.signal_engine, 'min_model_probability', 0.58),
                "momentum_weight": getattr(self.signal_engine, 'momentum_weight', -0.02),
                "regime_weight": getattr(self.signal_engine, 'regime_weight', 0.03),
                "flow_weight": getattr(self.signal_engine, 'flow_weight', 0.04),
                "spot_flow_weight": getattr(self.signal_engine, 'spot_flow_weight', 0.04),
                "student_t_df": getattr(self.signal_engine, 'student_t_df', 5),
                "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', 1.4),
                "exit_edge_threshold": self._exit_edge_threshold,
                "min_kelly": getattr(self.signal_engine, 'min_kelly', 0.015),
            }

        # Compute config diff
        config_changes = {}
        if self.signal_engine and old_config:
            new_vals = {
                "min_edge": getattr(self.signal_engine, 'min_edge', 0.20),
                "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', 0.15),
                "momentum_weight": getattr(self.signal_engine, 'momentum_weight', 0.08),
                "min_model_probability": getattr(self.signal_engine, 'min_model_probability', 0.65),
                "exit_edge_threshold": self._exit_edge_threshold,
                "min_time_remaining": self._min_time_remaining,
                "trading_start": self._trading_start,
                "trading_end": self._trading_end,
                "min_kelly": getattr(self.signal_engine, 'min_kelly', 0.015),
                "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', 1.7),
            }
            for k, old_v in old_config.items():
                new_v = new_vals.get(k)
                if old_v != new_v:
                    config_changes[k] = {"old": old_v, "new": new_v}

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
