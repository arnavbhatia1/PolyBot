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
                 config: dict[str, Any] | None = None, counterfactual_tracker: Any = None) -> None:
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
        self._exit_edge_threshold: float | None = None  # Set by main.py, updated by pipeline
        self._min_time_remaining: int | None = None   # Set by main.py, updated by pipeline
        self._trading_start: tuple[int, int] | None = None        # (hour, minute) UTC — updated by pipeline
        self._trading_end: tuple[int, int] | None = None          # (hour, minute) UTC — updated by pipeline
        self._running: bool = False
        self._auto_shutdown: bool = False
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
            "perp_lead_weight": getattr(self.signal_engine, 'perp_lead_weight', 0.03),
            "prev_margin_weight": getattr(self.signal_engine, 'prev_margin_weight', 0.02),
            "active_weights_version": getattr(self.indicator_engine, 'active_version', 'weights_v001')
                                      if self.indicator_engine else "weights_v001",
        }

        recommendations = await self.ta_evolver.evolve(outcomes, analysis, current_config)
        return recommendations

    async def _run_weight_optimizer(self, recommendations: dict[str, Any], outcomes: list[dict[str, Any]] | None = None) -> None:
        if outcomes is None:
            outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes or len(outcomes) < 10:
            logger.info(f"Only {len(outcomes)} outcomes — need at least 10 for weight optimization")
            return

        # Score current weights
        current_version = self.weight_optimizer.get_best_version()
        current_outcomes = [o for o in outcomes if o.get("weight_version") == current_version]

        current_sharpe = 0.0
        if current_outcomes:
            returns = [o.get("gain_pct", 0) for o in current_outcomes]
            avg = sum(returns) / len(returns)
            variance = sum((r - avg) ** 2 for r in returns) / len(returns) if len(returns) > 1 else 1
            std = math.sqrt(variance) if variance > 0 else 1
            current_sharpe = avg / std if std > 0 else 0
            win_rate = sum(1 for o in current_outcomes if o.get("correct", False)) / len(current_outcomes)
            self.weight_optimizer.record_score(current_version, current_sharpe, len(current_outcomes), win_rate)

        # Extract recommended indicator weights
        recommended_weights = recommendations.get("recommended_weights", {})
        if not recommended_weights:
            return

        # Simulate: what would outcomes look like with new weights?
        scored_outcomes = [o for o in outcomes if o.get("indicator_snapshot")]
        if len(scored_outcomes) < 5:
            return

        candidate_returns = []
        for o in scored_outcomes:
            snap = o.get("indicator_snapshot", {})
            ctx = snap.get("trade_context", {})

            # Preferred: use trade_context to evaluate with probability model
            if ctx.get("edge", 0) > 0:
                # Recompute momentum with new weights (use norm_score — matches live engine)
                new_momentum = sum(
                    snap.get(ind, {}).get("norm_score", snap.get(ind, {}).get("score", 0)) * recommended_weights.get(ind, 0)
                    for ind in ["rsi", "macd", "stochastic", "obv", "vwap"]
                )
                new_momentum = max(-1.0, min(1.0, new_momentum))

                # Compute what the probability adjustment would have been
                rec_mw = recommendations.get("recommended_momentum_weight",
                            getattr(self.signal_engine, 'momentum_weight', 0.08))
                rec_me = recommendations.get("recommended_min_edge",
                            getattr(self.signal_engine, 'min_edge', 0.20))

                # Original model_probability already includes old momentum
                # Approximate: would the trade still have enough edge with new params?
                original_edge = ctx.get("edge", 0)
                old_momentum = ctx.get("momentum_score", 0)
                old_mw = getattr(self.signal_engine, 'momentum_weight', 0.08)

                # Estimate new edge by adjusting for momentum difference
                momentum_delta = (new_momentum * rec_mw) - (old_momentum * old_mw)
                estimated_new_edge = original_edge + momentum_delta

                if estimated_new_edge >= rec_me:
                    candidate_returns.append(o.get("gain_pct", 0))
            else:
                # Fallback: old-style backtest for outcomes without trade_context
                new_score = sum(
                    snap.get(ind, {}).get("norm_score", snap.get(ind, {}).get("score", 0)) * recommended_weights.get(ind, 0)
                    for ind in ["rsi", "macd", "stochastic", "obv", "vwap"]
                )
                if abs(new_score) >= 0.10:
                    candidate_returns.append(o.get("gain_pct", 0))

        if len(candidate_returns) < 3:
            logger.info("Not enough hypothetical trades to evaluate new weights")
            return

        avg = sum(candidate_returns) / len(candidate_returns)
        variance = sum((r - avg) ** 2 for r in candidate_returns) / len(candidate_returns)
        std = math.sqrt(variance) if variance > 0 else 1
        candidate_sharpe = avg / std if std > 0 else 0
        candidate_win_rate = sum(1 for r in candidate_returns if r > 0) / len(candidate_returns)

        # Decide: auto-adopt or flag as concerning
        if self.weight_optimizer.should_adopt(current_sharpe, candidate_sharpe):
            new_version = self.weight_optimizer.get_next_version()
            new_weights = recommended_weights.copy()
            new_weights["version"] = new_version
            self.weight_optimizer.save_weights(new_version, new_weights)
            self.weight_optimizer.record_score(new_version, candidate_sharpe,
                                               len(candidate_returns), candidate_win_rate)

            # Hot-swap indicator weights
            if self.indicator_engine:
                self.indicator_engine.set_active_version(new_version)

            # Hot-swap signal engine parameters (weights + Claude's parameter recommendations)
            # Clamp all values to safe ranges to prevent runaway pipeline recommendations
            def _clamp(val, lo, hi): return max(lo, min(hi, val))

            if self.signal_engine:
                self.signal_engine.weights = {k: v for k, v in new_weights.items()
                                               if k in ["rsi", "macd", "stochastic", "obv", "vwap"]}
                if "recommended_momentum_weight" in recommendations:
                    self.signal_engine.momentum_weight = _clamp(recommendations["recommended_momentum_weight"], 0.02, 0.10)
                if "recommended_regime_weight" in recommendations:
                    self.signal_engine.regime_weight = _clamp(recommendations["recommended_regime_weight"], 0.02, 0.10)
                if "recommended_flow_weight" in recommendations:
                    self.signal_engine.flow_weight = _clamp(recommendations["recommended_flow_weight"], 0.02, 0.12)
                if "recommended_student_t_df" in recommendations:
                    self.signal_engine.student_t_df = _clamp(int(recommendations["recommended_student_t_df"]), 3, 8)
                if "recommended_min_edge" in recommendations:
                    val = _clamp(recommendations["recommended_min_edge"], 0.01, 0.10)
                    self.signal_engine.min_edge = val
                    self.signal_engine.entry_threshold = val
                if "recommended_kelly_fraction" in recommendations:
                    self.signal_engine.kelly_fraction = _clamp(recommendations["recommended_kelly_fraction"], 0.05, 0.25)
                if "recommended_min_model_probability" in recommendations:
                    self.signal_engine.min_model_probability = _clamp(recommendations["recommended_min_model_probability"], 0.55, 0.85)
                if "recommended_exit_edge_threshold" in recommendations:
                    self._exit_edge_threshold = _clamp(recommendations["recommended_exit_edge_threshold"], -0.25, 0.0)
                if "recommended_min_time_remaining" in recommendations:
                    self._min_time_remaining = _clamp(recommendations["recommended_min_time_remaining"], 0, 120)
                    if self.market_scanner:
                        self.market_scanner.min_time_remaining = self._min_time_remaining
                if "recommended_min_kelly" in recommendations:
                    self.signal_engine.min_kelly = _clamp(recommendations["recommended_min_kelly"], 0.005, 0.05)
                if "recommended_atr_sigma_ratio" in recommendations:
                    self.signal_engine.atr_sigma_ratio = _clamp(float(recommendations["recommended_atr_sigma_ratio"]), 1.2, 2.5)
                if "recommended_spot_flow_weight" in recommendations:
                    self.signal_engine.spot_flow_weight = _clamp(recommendations["recommended_spot_flow_weight"], 0.0, 0.10)
                if "recommended_wall_weight" in recommendations:
                    self.signal_engine.wall_weight = _clamp(recommendations["recommended_wall_weight"], 0.0, 0.15)
                if "recommended_perp_lead_weight" in recommendations:
                    self.signal_engine.perp_lead_weight = _clamp(recommendations["recommended_perp_lead_weight"], 0.0, 0.10)
                if "recommended_prev_margin_weight" in recommendations:
                    self.signal_engine.prev_margin_weight = _clamp(recommendations["recommended_prev_margin_weight"], 0.0, 0.05)
                if "recommended_trading_start_hour_et" in recommendations:
                    start_h = recommendations["recommended_trading_start_hour_et"]
                    self._trading_start = (start_h, 0)
                if "recommended_trading_end_hour_et" in recommendations:
                    end_h = recommendations["recommended_trading_end_hour_et"]
                    end_m = recommendations.get("recommended_trading_end_minute", 59)
                    self._trading_end = (end_h, end_m)

            # Persist tuned parameters to settings.yaml so they survive restarts
            # Apply same _clamp bounds as hot-swap to prevent unclamped values on restart
            if self._config:
                sig = self._config.setdefault("signal", {})
                mkt = self._config.setdefault("market", {})
                sched = self._config.setdefault("schedule", {})
                math_sec = self._config.setdefault("math", {})

                sig["active_weights_version"] = new_version
                sig["weights"] = {k: v for k, v in new_weights.items()
                                  if k in ["rsi", "macd", "stochastic", "obv", "vwap"]}
                if "recommended_min_kelly" in recommendations:
                    sig["min_kelly"] = _clamp(recommendations["recommended_min_kelly"], 0.005, 0.05)
                if "recommended_atr_sigma_ratio" in recommendations:
                    sig["atr_sigma_ratio"] = _clamp(float(recommendations["recommended_atr_sigma_ratio"]), 1.2, 2.5)
                if "recommended_spot_flow_weight" in recommendations:
                    sig["spot_flow_weight"] = _clamp(recommendations["recommended_spot_flow_weight"], 0.0, 0.10)
                if "recommended_wall_weight" in recommendations:
                    sig["wall_weight"] = _clamp(recommendations["recommended_wall_weight"], 0.0, 0.15)
                if "recommended_perp_lead_weight" in recommendations:
                    sig["perp_lead_weight"] = _clamp(recommendations["recommended_perp_lead_weight"], 0.0, 0.10)
                if "recommended_prev_margin_weight" in recommendations:
                    sig["prev_margin_weight"] = _clamp(recommendations["recommended_prev_margin_weight"], 0.0, 0.05)
                if "recommended_momentum_weight" in recommendations:
                    sig["momentum_weight"] = _clamp(recommendations["recommended_momentum_weight"], 0.02, 0.10)
                if "recommended_regime_weight" in recommendations:
                    sig["regime_weight"] = _clamp(recommendations["recommended_regime_weight"], 0.02, 0.10)
                if "recommended_flow_weight" in recommendations:
                    sig["flow_weight"] = _clamp(recommendations["recommended_flow_weight"], 0.02, 0.12)
                if "recommended_student_t_df" in recommendations:
                    sig["student_t_df"] = _clamp(int(recommendations["recommended_student_t_df"]), 3, 8)
                if "recommended_min_edge" in recommendations:
                    sig["entry_threshold"] = _clamp(recommendations["recommended_min_edge"], 0.01, 0.10)
                if "recommended_kelly_fraction" in recommendations:
                    math_sec["kelly_fraction"] = _clamp(recommendations["recommended_kelly_fraction"], 0.05, 0.25)
                if "recommended_min_model_probability" in recommendations:
                    sig["min_model_probability"] = _clamp(recommendations["recommended_min_model_probability"], 0.55, 0.85)
                if "recommended_exit_edge_threshold" in recommendations:
                    sig["exit_edge_threshold"] = _clamp(recommendations["recommended_exit_edge_threshold"], -0.25, 0.0)
                if "recommended_min_time_remaining" in recommendations:
                    mkt["min_time_remaining_seconds"] = _clamp(recommendations["recommended_min_time_remaining"], 0, 120)
                if "recommended_trading_start_hour_et" in recommendations:
                    sched["trading_start_hour_et"] = recommendations["recommended_trading_start_hour_et"]
                if "recommended_trading_end_hour_et" in recommendations:
                    sched["trading_end_hour_et"] = recommendations["recommended_trading_end_hour_et"]
                if "recommended_trading_end_minute" in recommendations:
                    sched["trading_end_minute"] = recommendations["recommended_trading_end_minute"]

                try:
                    config_to_save = {k: v for k, v in self._config.items() if k != "mode"}
                    save_config(config_to_save)
                    logger.info("Pipeline parameters persisted to settings.yaml")
                except Exception as e:
                    logger.error(f"Failed to persist config: {e}")

            logger.info(f"AUTO-ADOPTED {new_version}: Sharpe {current_sharpe:.3f} -> {candidate_sharpe:.3f}, "
                        f"win rate {candidate_win_rate:.0%}")

            if self.alert_manager:
                findings = recommendations.get("key_findings", [])
                findings_str = "\n".join(f"  - {f}" for f in findings[:5]) if findings else ""
                reasoning_preview = recommendations.get("reasoning", "")[:200]

                msg = (
                    f"**Weights auto-updated: {current_version} -> {new_version}**\n"
                    f"Sharpe: `{current_sharpe:.3f}` -> `{candidate_sharpe:.3f}`\n"
                    f"Win rate: `{candidate_win_rate:.0%}`\n"
                    f"Trades evaluated: `{len(candidate_returns)}`\n"
                    f"New weights: {', '.join(f'{k}={v:.2f}' for k, v in new_weights.items() if k in ['rsi','macd','stochastic','obv','vwap'])}"
                )
                if "recommended_momentum_weight" in recommendations:
                    msg += f"\nmomentum_weight: `{recommendations['recommended_momentum_weight']}`"
                if "recommended_min_edge" in recommendations:
                    msg += f"\nmin_edge: `{recommendations['recommended_min_edge']}`"
                if "recommended_min_model_probability" in recommendations:
                    msg += f"\nmin_model_prob: `{recommendations['recommended_min_model_probability']}`"
                if "recommended_exit_edge_threshold" in recommendations:
                    msg += f"\nexit_threshold: `{recommendations['recommended_exit_edge_threshold']}`"
                if "recommended_min_time_remaining" in recommendations:
                    msg += f"\nmin_time_remaining: `{recommendations['recommended_min_time_remaining']}s`"
                if "recommended_min_kelly" in recommendations:
                    msg += f"\nmin_kelly: `{recommendations['recommended_min_kelly']}`"
                if "recommended_atr_sigma_ratio" in recommendations:
                    msg += f"\natr_sigma_ratio: `{recommendations['recommended_atr_sigma_ratio']}`"
                if "recommended_trading_start_hour_et" in recommendations or "recommended_trading_end_hour_et" in recommendations:
                    start_h = recommendations.get("recommended_trading_start_hour_et", self._trading_start[0] if self._trading_start else 8)
                    end_h = recommendations.get("recommended_trading_end_hour_et", self._trading_end[0] if self._trading_end else 16)
                    end_m = recommendations.get("recommended_trading_end_minute", self._trading_end[1] if self._trading_end else 30)
                    msg += f"\ntrading_hours: `{start_h}:00-{end_h}:{end_m:02d} ET`"
                if findings_str:
                    msg += f"\n\n**Key Findings:**\n{findings_str}"
                if reasoning_preview:
                    msg += f"\n\n**Analysis:** {reasoning_preview}..."

                await self.alert_manager.send_pipeline_summary(msg)

        elif candidate_sharpe < -0.5:
            logger.warning(f"NEGATIVE SHARPE detected: {candidate_sharpe:.3f} — flagging in Discord")
            warnings = recommendations.get("risk_warnings", [])
            warnings_str = "\n".join(f"  - {w}" for w in warnings[:3]) if warnings else ""

            msg = (
                f"WARNING: Strategy performing poorly.\n"
                f"Current Sharpe: {current_sharpe:.3f}\n"
                f"Candidate Sharpe: {candidate_sharpe:.3f}\n"
                f"Win rate: {candidate_win_rate:.0%}\n"
                f"Consider pausing (!pause) and reviewing trades (!history 20)"
            )
            if warnings_str:
                msg += f"\n\nRisk Warnings:\n{warnings_str}"

            if self.alert_manager:
                await self.alert_manager.send_error(msg)
        else:
            logger.info(f"Weights not adopted: improvement {candidate_sharpe - current_sharpe:.3f} "
                        f"below threshold {self.weight_optimizer.min_improvement}")
            if self.alert_manager:
                msg = (
                    f"**Learning pipeline complete — no weight change**\n"
                    f"Current Sharpe: `{current_sharpe:.3f}`\n"
                    f"Candidate Sharpe: `{candidate_sharpe:.3f}`\n"
                    f"Improvement `{candidate_sharpe - current_sharpe:.3f}` below threshold `{self.weight_optimizer.min_improvement}`"
                )
                findings = recommendations.get("key_findings", [])
                if findings:
                    msg += "\n\n**Claude's Findings:**\n" + "\n".join(f"  - {f}" for f in findings[:3])
                await self.alert_manager.send_pipeline_summary(msg)

    async def run_daily_pipeline(self) -> None:
        logger.info("Starting daily learning pipeline")

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

        # Hold-out split: train on first 60% of outcomes (chronological),
        # validate on last 40%.  Prevents in-sample overfitting — Claude's
        # recommendations are based on older trades, adoption decision is
        # based on newer trades the model hasn't seen.
        all_outcomes = self.outcome_reviewer.load_all_outcomes()  # already sorted by timestamp
        split_idx = max(1, int(len(all_outcomes) * 0.6))
        train_outcomes = all_outcomes[:split_idx]
        validation_outcomes = all_outcomes[split_idx:] if len(all_outcomes) > split_idx else all_outcomes
        logger.info(f"Hold-out split: {len(train_outcomes)} train / {len(validation_outcomes)} validation "
                    f"(of {len(all_outcomes)} total)")

        analysis = await self._run_bias_detector(train_outcomes)

        # Counterfactual analysis: how accurate are our scalp exits?
        if self.counterfactual_tracker:
            counterfactuals = self.counterfactual_tracker.load_all()
            if counterfactuals:
                cf_analysis = self.bias_detector.analyze_counterfactuals(counterfactuals)
                analysis["counterfactual_analysis"] = cf_analysis
                logger.info(f"Counterfactual analysis: {cf_analysis.get('total_scalps_tracked', 0)} scalps tracked, "
                           f"accuracy={cf_analysis.get('scalp_accuracy', 0):.0%}")

        # Platt calibration fitting
        from polybot.core.calibrator import PlattCalibrator, compute_log_loss
        if len(train_outcomes) >= 100 and self.signal_engine:
            cal_probs = []
            cal_outcomes = []
            for o in train_outcomes:
                ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
                mp = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
                if mp > 0:
                    cal_probs.append(mp)
                    cal_outcomes.append(1 if o.get("correct", False) else 0)

            if len(cal_probs) >= 100:
                cal = PlattCalibrator()
                if self.signal_engine.calibrator:
                    cal.a = self.signal_engine.calibrator.a
                    cal.b = self.signal_engine.calibrator.b
                if cal.fit(cal_probs, cal_outcomes):
                    val_probs, val_outs = [], []
                    for o in validation_outcomes:
                        ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
                        mp = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
                        if mp > 0:
                            val_probs.append(mp)
                            val_outs.append(1 if o.get("correct", False) else 0)
                    if val_probs:
                        old_loss = compute_log_loss(val_probs, val_outs)
                        new_probs = [cal.calibrate(p) for p in val_probs]
                        new_loss = compute_log_loss(new_probs, val_outs)
                        if new_loss < old_loss:
                            cal.save()
                            self.signal_engine.calibrator = cal
                            logger.info(f"Platt calibration adopted: log-loss {old_loss:.4f} -> {new_loss:.4f}")
                        else:
                            logger.info(f"Platt calibration rejected: {old_loss:.4f} -> {new_loss:.4f}")

        # Gate: need at least 50 trades before running TAEvolver and WeightOptimizer.
        # With fewer trades, win-rate variance is too high (±13pp at N=25) — noise, not signal.
        MIN_TRADES_FOR_LEARNING = 50
        if len(all_outcomes) < MIN_TRADES_FOR_LEARNING:
            logger.info(f"Skipping learning pipeline: only {len(all_outcomes)} trades, need {MIN_TRADES_FOR_LEARNING}")
            recommendations = {}
        else:
            recommendations = await self._run_ta_evolver(analysis, train_outcomes)
            await self._run_weight_optimizer(recommendations, validation_outcomes)

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
            outcomes = self.outcome_reviewer.load_all_outcomes()
            try:
                await self.alert_manager.send_daily_report(
                    outcomes, analysis, recommendations, config_changes)
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
                # If auto_shutdown is enabled, signal the bot to exit
                if self._auto_shutdown:
                    logger.info("PIPELINE COMPLETE — auto-shutdown enabled, exiting for restart cycle")
                    self._shutdown_requested = True
                    return
                await asyncio.sleep(3600)
            await asyncio.sleep(60)

    async def start(self) -> None:
        self._running = True
        logger.info("Agent scheduler started")

    async def stop(self) -> None:
        self._running = False
        logger.info("Agent scheduler stopped")
