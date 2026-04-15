from __future__ import annotations

import asyncio
import math
import logging
from datetime import datetime
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
            "prev_margin_weight": getattr(self.signal_engine, 'prev_margin_weight', 0.02),
            "active_weights_version": getattr(self.indicator_engine, 'active_version', 'weights_v001')
                                      if self.indicator_engine else "weights_v001",
        }

        recommendations = await self.ta_evolver.evolve(outcomes, analysis, current_config)
        return recommendations

    def _backtest_recommendations(self, recommendations: dict[str, Any],
                                    outcomes: list[dict[str, Any]]) -> list[float]:
        """Simulate trades with recommended weights. Returns list of gain_pcts."""
        recommended_weights = recommendations.get("recommended_weights", {})
        rec_mw = recommendations.get("recommended_momentum_weight",
                    getattr(self.signal_engine, 'momentum_weight', 0.08))
        rec_me = recommendations.get("recommended_min_edge",
                    getattr(self.signal_engine, 'min_edge', 0.20))
        old_mw = getattr(self.signal_engine, 'momentum_weight', 0.08)

        candidate_returns = []
        for o in outcomes:
            snap = o.get("indicator_snapshot", {})
            if not snap:
                continue
            ctx = snap.get("trade_context", {})

            if ctx.get("edge", 0) > 0:
                new_momentum = sum(
                    snap.get(ind, {}).get("norm_score", snap.get(ind, {}).get("score", 0)) * recommended_weights.get(ind, 0)
                    for ind in ["rsi", "macd", "stochastic", "obv", "vwap"]
                )
                new_momentum = max(-1.0, min(1.0, new_momentum))
                original_edge = ctx.get("edge", 0)
                old_momentum = ctx.get("momentum_score", 0)
                momentum_delta = (new_momentum * rec_mw) - (old_momentum * old_mw)
                estimated_new_edge = original_edge + momentum_delta
                if estimated_new_edge >= rec_me:
                    candidate_returns.append(o.get("gain_pct", 0))
            else:
                new_score = sum(
                    snap.get(ind, {}).get("norm_score", snap.get(ind, {}).get("score", 0)) * recommended_weights.get(ind, 0)
                    for ind in ["rsi", "macd", "stochastic", "obv", "vwap"]
                )
                if abs(new_score) >= 0.10:
                    candidate_returns.append(o.get("gain_pct", 0))
        return candidate_returns

    async def _run_weight_optimizer(self, recommendations: dict[str, Any],
                                    all_outcomes: list[dict[str, Any]] | None = None,
                                    pipeline_source: str = "local") -> dict[str, Any]:
        """Run weight optimizer with walk-forward validation.

        Walk-forward folds (each fold's test set is genuinely out-of-sample):
          Fold 1: Test [60%:70%]
          Fold 2: Test [70%:80%]
          Fold 3: Test [80%:90%]
          Fold 4: Test [90%:100%]

        Adoption requires statistical significance (z >= 1.65) on aggregated
        results AND positive improvement in every fold.

        Returns info dict with decision details.
        """
        from polybot.agents.weight_optimizer import _sharpe

        info: dict[str, Any] = {"decision": "skipped", "reason": ""}
        if all_outcomes is None:
            all_outcomes = self.outcome_reviewer.load_all_outcomes()
        if not all_outcomes or len(all_outcomes) < 10:
            info["reason"] = f"only {len(all_outcomes) if all_outcomes else 0} outcomes (need 10)"
            return info

        # Score current weights on FULL dataset
        current_version = self.weight_optimizer.get_best_version()
        current_outcomes = [o for o in all_outcomes if o.get("weight_version") == current_version]

        current_sharpe = 0.0
        if current_outcomes:
            current_returns = [o.get("gain_pct", 0) for o in current_outcomes]
            current_sharpe = _sharpe(current_returns)
            win_rate = sum(1 for o in current_outcomes if o.get("correct", False)) / len(current_outcomes)
            self.weight_optimizer.record_score(current_version, current_sharpe, len(current_outcomes), win_rate)
        info["old_version"] = current_version
        info["old_sharpe"] = round(current_sharpe, 4)

        recommended_weights = recommendations.get("recommended_weights", {})
        if not recommended_weights:
            info["reason"] = "no recommended weights from evolver"
            return info

        # --- Walk-forward validation ---
        n = len(all_outcomes)
        fold_boundaries = [0.60, 0.70, 0.80, 0.90, 1.0]
        fold_sharpes = []
        all_candidate_returns = []

        for i in range(len(fold_boundaries) - 1):
            start_idx = int(n * fold_boundaries[i])
            end_idx = int(n * fold_boundaries[i + 1])
            fold_test = all_outcomes[start_idx:end_idx]
            if len(fold_test) < 3:
                continue
            fold_returns = self._backtest_recommendations(recommendations, fold_test)
            if len(fold_returns) < 3:
                continue
            fold_sharpes.append(_sharpe(fold_returns))
            all_candidate_returns.extend(fold_returns)

        if len(all_candidate_returns) < 10:
            info["reason"] = f"only {len(all_candidate_returns)} hypothetical trades across folds (need 10)"
            logger.info("Not enough hypothetical trades across walk-forward folds")
            return info

        candidate_sharpe = _sharpe(all_candidate_returns)
        candidate_win_rate = sum(1 for r in all_candidate_returns if r > 0) / len(all_candidate_returns)
        info["new_sharpe"] = round(candidate_sharpe, 4)
        info["candidate_win_rate"] = round(candidate_win_rate, 4)
        info["fold_sharpes"] = [round(s, 4) for s in fold_sharpes]
        info["n_validation_trades"] = len(all_candidate_returns)

        # --- Statistical adoption test ---
        adopt, reason = self.weight_optimizer.should_adopt(
            current_sharpe, candidate_sharpe,
            n_trades=len(all_candidate_returns),
            fold_sharpes=fold_sharpes,
        )

        if adopt:
            info["decision"] = "adopted"
            info["reason"] = reason
            new_version = self.weight_optimizer.get_next_version()
            new_weights = recommended_weights.copy()
            new_weights["version"] = new_version
            self.weight_optimizer.save_weights(new_version, new_weights)
            self.weight_optimizer.record_score(new_version, candidate_sharpe,
                                               len(all_candidate_returns), candidate_win_rate)

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
                    self.signal_engine.momentum_weight = _clamp(recommendations["recommended_momentum_weight"], -0.10, 0.10)
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
                if "recommended_prev_margin_weight" in recommendations:
                    sig["prev_margin_weight"] = _clamp(recommendations["recommended_prev_margin_weight"], 0.0, 0.05)
                if "recommended_momentum_weight" in recommendations:
                    sig["momentum_weight"] = _clamp(recommendations["recommended_momentum_weight"], -0.10, 0.10)
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
                    config_to_save = dict(self._config)
                    save_config(config_to_save)
                    logger.info("Pipeline parameters persisted to settings.yaml")
                except Exception as e:
                    logger.error(f"Failed to persist config: {e}")

            info["new_version"] = new_version
            logger.info(f"AUTO-ADOPTED {new_version}: Sharpe {current_sharpe:.3f} -> {candidate_sharpe:.3f}, "
                        f"win rate {candidate_win_rate:.0%} ({reason})")

            # Track adoption for future self-evaluation
            if self.pipeline_tracker:
                changes = {}
                param_map = [
                    ("recommended_momentum_weight", "momentum_weight"),
                    ("recommended_min_edge", "min_edge"),
                    ("recommended_kelly_fraction", "kelly_fraction"),
                    ("recommended_min_model_probability", "min_model_probability"),
                    ("recommended_exit_edge_threshold", "exit_edge_threshold"),
                    ("recommended_atr_sigma_ratio", "atr_sigma_ratio"),
                ]
                for rec_key, param_name in param_map:
                    if rec_key in recommendations:
                        old_val = getattr(self.signal_engine, param_name, None) if self.signal_engine else None
                        changes[param_name] = (old_val, recommendations[rec_key])
                self.pipeline_tracker.record_adoption(
                    source=pipeline_source,
                    version=new_version,
                    baseline_sharpe=current_sharpe,
                    predicted_sharpe=candidate_sharpe,
                    changes=changes,
                    reason=reason,
                )

        else:
            info["decision"] = "no_change"
            info["reason"] = reason
            logger.info(f"Weights not adopted: {reason}")

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
        all_outcomes = self.outcome_reviewer.load_all_outcomes()  # already sorted by timestamp
        split_idx = max(1, int(len(all_outcomes) * 0.6))
        train_outcomes = all_outcomes[:split_idx]
        validation_outcomes = all_outcomes[split_idx:]  # used for Platt holdout validation
        logger.info(f"Walk-forward split: {len(train_outcomes)} train / {len(validation_outcomes)} validation "
                    f"(4 folds, {len(all_outcomes)} total)")
        pipeline_info["total_outcomes"] = len(all_outcomes)
        pipeline_info["train_count"] = len(train_outcomes)
        pipeline_info["validation_count"] = len(all_outcomes) - len(train_outcomes)

        # Review past pipeline adoptions (fill in actual 7d/30d Sharpe)
        if self.pipeline_tracker:
            self.pipeline_tracker.review_past_adoptions(all_outcomes)

        analysis = await self._run_bias_detector(train_outcomes)

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

        # Platt calibration fitting
        platt_info: dict[str, Any] = {"decision": "skipped"}
        from polybot.core.calibrator import PlattCalibrator, compute_log_loss
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
                        platt_info = {"old_loss": round(old_loss, 4), "new_loss": round(new_loss, 4),
                                      "a": round(cal.a, 4), "b": round(cal.b, 4)}
                        if new_loss < old_loss:
                            platt_info["decision"] = "adopted"
                            cal.save()
                            self.signal_engine.calibrator = cal
                            logger.info(f"Platt calibration adopted: log-loss {old_loss:.4f} -> {new_loss:.4f}")
                        else:
                            platt_info["decision"] = "rejected"
                            logger.info(f"Platt calibration rejected: {old_loss:.4f} -> {new_loss:.4f}")
        pipeline_info["platt"] = platt_info

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

        # Per-regime Platt calibration (when enough per-regime data)
        if len(train_outcomes) >= 200 and self.signal_engine:
            regime_buckets: dict[str, tuple[list, list]] = {}
            for o in train_outcomes:
                ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
                regime = ctx.get("regime_state", "neutral")
                if regime.startswith("trending"):
                    regime = "trending"
                mp = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
                if mp > 0:
                    if regime not in regime_buckets:
                        regime_buckets[regime] = ([], [])
                    regime_buckets[regime][0].append(mp)
                    regime_buckets[regime][1].append(1 if o.get("correct", False) else 0)
            regime_cal_info = {}
            for regime, (probs, outs) in regime_buckets.items():
                if len(probs) >= 200:
                    regime_cal_info[regime] = {"samples": len(probs), "status": "sufficient"}
                else:
                    regime_cal_info[regime] = {"samples": len(probs), "status": "insufficient"}
            if regime_cal_info:
                analysis["regime_calibration_data"] = regime_cal_info
                logger.info(f"Per-regime calibration data: {', '.join(f'{r}={v['samples']}' for r, v in regime_cal_info.items())}")

        # Gate: need at least 200 trades before running TAEvolver and WeightOptimizer.
        MIN_TRADES_FOR_LEARNING = 200
        weight_info: dict[str, Any] = {"decision": "skipped"}
        if len(all_outcomes) < MIN_TRADES_FOR_LEARNING:
            logger.info(f"Skipping learning pipeline: only {len(all_outcomes)} trades, need {MIN_TRADES_FOR_LEARNING}")
            recommendations = {}
            weight_info["reason"] = f"only {len(all_outcomes)} trades (need {MIN_TRADES_FOR_LEARNING})"
        else:
            # Build Claude context including pipeline track record
            if self.pipeline_tracker:
                track_record = self.pipeline_tracker.format_for_claude()
                if track_record:
                    analysis["pipeline_track_record"] = track_record

            recommendations = await self._run_ta_evolver(analysis, train_outcomes)
            source = "claude" if recommendations.get("confidence") else "local"
            pipeline_info["source"] = source

            # SPRT urgency: if edge evidence is negative, lower adoption bar
            if sprt_agg.get("state") == "negative":
                self.weight_optimizer.min_improvement = 0.02  # more aggressive
                logger.info("SPRT negative — lowering adoption threshold to 0.02")
            elif sprt_agg.get("state") == "positive":
                self.weight_optimizer.min_improvement = 0.05  # more conservative
                logger.info("SPRT positive — raising adoption threshold to 0.05")
            else:
                self.weight_optimizer.min_improvement = 0.03  # default

            # Cooldown: don't adopt within 3 days of last change (confounded data)
            COOLDOWN_DAYS = 3
            cooldown_active = False
            if self.pipeline_tracker:
                days = self.pipeline_tracker.days_since_last_adoption()
                if days is not None and days < COOLDOWN_DAYS:
                    cooldown_active = True
                    logger.info(f"Pipeline cooldown: {days:.1f} days since last adoption (need {COOLDOWN_DAYS}), analysis only")
                    weight_info = {"decision": "cooldown", "reason": f"{days:.1f}d since last adoption (need {COOLDOWN_DAYS}d)"}

            if not cooldown_active:
                # Walk-forward optimizer gets ALL outcomes; it splits into 4 folds internally
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
