"""AgentScheduler: orchestrates the nightly learning pipeline (12:05 AM ET).

Runs BiasDetector, Platt calibration (with recency-weighted MLE), distribution shift
detection, SPRT aggregation, TA Evolver (Claude), and WeightOptimizer in sequence.
Adopts parameter changes only when they pass strict statistical gates (z >= 1.28,
3/4 walk-forward folds positive, 3-day cooldown).
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
        self._last_rejection_reason: str = ""  # why last weight proposal was rejected
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

        if self._last_rejection_reason:
            analysis["last_rejection_reason"] = self._last_rejection_reason
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
        """Resolve config for `_kelly_bankroll_returns` — recommendation first, live engine fallback."""
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
            "min_edge": rec.get("recommended_min_edge",
                getattr(self.signal_engine, 'min_edge', 0.04)),
            "kelly_fraction": rec.get("recommended_kelly_fraction",
                getattr(self.signal_engine, 'kelly_fraction', 0.15)),
            "min_kelly": rec.get("recommended_min_kelly",
                getattr(self.signal_engine, 'min_kelly', 0.015)),
            "min_model_probability": rec.get("recommended_min_model_probability",
                getattr(self.signal_engine, 'min_model_probability', 0.58)),
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

        current_version = self.weight_optimizer.get_best_version()
        info["old_version"] = current_version

        recommended_weights = recommendations.get("recommended_weights", {})
        if not recommended_weights:
            info["reason"] = "no recommended weights from evolver"
            return info

        # --- Walk-forward validation ---
        # Baseline and candidate are BOTH backtested on the same folds with the same
        # Kelly-sized-Sharpe metric, so adoption can't be driven by a population mismatch
        # (previous code compared candidate fold-Sharpe against a raw-gain_pct Sharpe of
        # trades that happened to run under current_version — not apples-to-apples).
        n = len(all_outcomes)
        fold_boundaries = [0.60, 0.70, 0.80, 0.90, 1.0]
        fold_sharpes: list[float] = []
        all_candidate_returns: list[float] = []
        all_current_returns: list[float] = []

        baseline_request: dict[str, Any] = {}  # empty -> helper uses current engine state

        for i in range(len(fold_boundaries) - 1):
            start_idx = int(n * fold_boundaries[i])
            end_idx = int(n * fold_boundaries[i + 1])
            fold_test = all_outcomes[start_idx:end_idx]
            if len(fold_test) < 3:
                continue
            fold_returns = self._backtest_recommendations(recommendations, fold_test)
            current_fold_returns = self._backtest_recommendations(baseline_request, fold_test)
            all_current_returns.extend(current_fold_returns)
            if len(fold_returns) < 3:
                continue
            fold_sharpes.append(_sharpe(fold_returns))
            all_candidate_returns.extend(fold_returns)

        current_sharpe = _sharpe(all_current_returns) if all_current_returns else 0.0
        current_win_rate = (sum(1 for r in all_current_returns if r > 0) / len(all_current_returns)
                            if all_current_returns else 0.0)
        if all_current_returns:
            self.weight_optimizer.record_score(current_version, current_sharpe,
                                               len(all_current_returns), current_win_rate)
        info["old_sharpe"] = round(current_sharpe, 4)
        info["n_baseline_trades"] = len(all_current_returns)

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
            candidate_returns=all_candidate_returns,
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
                    self.signal_engine.prev_margin_weight = _clamp(recommendations["recommended_prev_margin_weight"], 0.01, 0.05)
                if "recommended_logit_scale" in recommendations:
                    self.signal_engine.logit_scale = _clamp(float(recommendations["recommended_logit_scale"]), 2.0, 6.0)
                if "recommended_probability_compression" in recommendations:
                    self.signal_engine.probability_compression = _clamp(float(recommendations["recommended_probability_compression"]), 0.5, 1.0)
                if "recommended_liquidation_weight" in recommendations:
                    self.signal_engine.liquidation_weight = _clamp(recommendations["recommended_liquidation_weight"], 0.01, 0.06)
                if "recommended_adverse_selection_threshold" in recommendations:
                    if self._config:
                        self._config.setdefault("signal", {})["adverse_selection_threshold"] = _clamp(recommendations["recommended_adverse_selection_threshold"], 0.45, 0.75)
                if "recommended_normal_fraction" in recommendations:
                    if self._config:
                        self._config.setdefault("entry_timing", {})["normal_fraction"] = _clamp(float(recommendations["recommended_normal_fraction"]), 0.40, 0.80)
                if "recommended_late_max_penalty" in recommendations:
                    if self._config:
                        self._config.setdefault("entry_timing", {})["late_max_penalty"] = _clamp(float(recommendations["recommended_late_max_penalty"]), 0.20, 0.80)
                if "recommended_min_atr" in recommendations:
                    self.signal_engine.min_atr = _clamp(float(recommendations["recommended_min_atr"]), 5.0, 15.0)
                if "recommended_max_edge" in recommendations:
                    self.signal_engine.max_edge = _clamp(float(recommendations["recommended_max_edge"]), 0.10, 0.30)
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
                    sig["prev_margin_weight"] = _clamp(recommendations["recommended_prev_margin_weight"], 0.01, 0.05)
                if "recommended_logit_scale" in recommendations:
                    sig["logit_scale"] = _clamp(float(recommendations["recommended_logit_scale"]), 2.0, 6.0)
                if "recommended_probability_compression" in recommendations:
                    sig["probability_compression"] = _clamp(float(recommendations["recommended_probability_compression"]), 0.5, 1.0)
                if "recommended_liquidation_weight" in recommendations:
                    sig["liquidation_weight"] = _clamp(recommendations["recommended_liquidation_weight"], 0.01, 0.06)
                if "recommended_adverse_selection_threshold" in recommendations:
                    sig["adverse_selection_threshold"] = _clamp(recommendations["recommended_adverse_selection_threshold"], 0.45, 0.75)
                if "recommended_normal_fraction" in recommendations:
                    self._config.setdefault("entry_timing", {})["normal_fraction"] = _clamp(float(recommendations["recommended_normal_fraction"]), 0.40, 0.80)
                if "recommended_late_max_penalty" in recommendations:
                    self._config.setdefault("entry_timing", {})["late_max_penalty"] = _clamp(float(recommendations["recommended_late_max_penalty"]), 0.20, 0.80)
                if "recommended_min_atr" in recommendations:
                    sig["min_atr"] = _clamp(float(recommendations["recommended_min_atr"]), 5.0, 15.0)
                if "recommended_max_edge" in recommendations:
                    sig["max_edge"] = _clamp(float(recommendations["recommended_max_edge"]), 0.10, 0.30)
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
            self._last_rejection_reason = reason
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
        all_outcomes = self.outcome_reviewer.load_all_outcomes()
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
                    old_kelly_sharpe = _sharpe(old_returns)
                    new_kelly_sharpe = _sharpe(new_returns)

                    platt_info = {
                        "old_loss": round(old_loss, 4) if val_probs else None,
                        "new_loss": round(new_loss, 4) if val_probs else None,
                        "old_kelly_sharpe": round(old_kelly_sharpe, 4),
                        "new_kelly_sharpe": round(new_kelly_sharpe, 4),
                        "n_val_trades_old": len(old_returns),
                        "n_val_trades_new": len(new_returns),
                        "a": round(cal.a, 4),
                        "b": round(cal.b, 4),
                    }

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

            # Cooldown: don't adopt on back-to-back days (need at least 1 day of data to validate)
            COOLDOWN_DAYS = 2
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
        logger.debug("Agent scheduler started")

    async def stop(self) -> None:
        self._running = False
        logger.debug("Agent scheduler stopped")
