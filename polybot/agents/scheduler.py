import asyncio
import math
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class AgentScheduler:
    def __init__(self, outcome_reviewer, bias_detector, ta_evolver, weight_optimizer,
                 indicator_engine=None, signal_engine=None, alert_manager=None,
                 outcome_interval_seconds=3600, daily_pipeline_hour=2, math_config=None,
                 claude_client=None):
        self.outcome_reviewer = outcome_reviewer
        self.bias_detector = bias_detector
        self.ta_evolver = ta_evolver
        self.weight_optimizer = weight_optimizer
        self.indicator_engine = indicator_engine
        self.signal_engine = signal_engine
        self.alert_manager = alert_manager
        self.outcome_interval_seconds = outcome_interval_seconds
        self.daily_pipeline_hour = daily_pipeline_hour
        self.math_config = math_config or {}
        self.claude_client = claude_client
        self._running = False

        # Inject claude_client into ta_evolver if not already set
        if claude_client and not getattr(self.ta_evolver, 'claude_client', None):
            self.ta_evolver.claude_client = claude_client

    async def _run_bias_detector(self) -> dict:
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            logger.info("No outcomes to analyze for biases")
            return {}
        analysis = self.bias_detector.detect(outcomes)
        self.bias_detector.save(analysis)
        return analysis

    async def _run_ta_evolver(self, analysis: dict) -> dict:
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            return {}

        # Build current config from live engines
        current_weights = self.indicator_engine.get_weights() if self.indicator_engine else {}
        current_config = {
            "weights": {k: v for k, v in current_weights.items()
                        if k in ["rsi", "macd", "stochastic", "obv", "vwap"]},
            "momentum_weight": getattr(self.signal_engine, 'momentum_weight', 0.08),
            "min_edge": getattr(self.signal_engine, 'min_edge', 0.10),
            "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', 0.15),
            "active_weights_version": getattr(self.indicator_engine, 'active_version', 'weights_v001')
                                      if self.indicator_engine else "weights_v001",
        }

        recommendations = await self.ta_evolver.evolve(outcomes, analysis, current_config)
        return recommendations

    async def _run_weight_optimizer(self, recommendations: dict):
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes or len(outcomes) < 5:
            logger.info(f"Only {len(outcomes)} outcomes — need at least 5 for weight optimization")
            return

        # Score current weights
        current_version = self.weight_optimizer.get_best_version()
        current_outcomes = [o for o in outcomes if o.get("weight_version") == current_version]

        current_sharpe = 0.0
        if current_outcomes:
            returns = [o.get("log_return", 0) for o in current_outcomes]
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
                # Recompute momentum with new weights
                new_momentum = sum(
                    snap.get(ind, {}).get("score", 0) * recommended_weights.get(ind, 0)
                    for ind in ["rsi", "macd", "stochastic", "obv", "vwap"]
                )
                new_momentum = max(-1.0, min(1.0, new_momentum))

                # Compute what the probability adjustment would have been
                rec_mw = recommendations.get("recommended_momentum_weight",
                            getattr(self.signal_engine, 'momentum_weight', 0.08))
                rec_me = recommendations.get("recommended_min_edge",
                            getattr(self.signal_engine, 'min_edge', 0.10))

                # Original model_probability already includes old momentum
                # Approximate: would the trade still have enough edge with new params?
                original_edge = ctx.get("edge", 0)
                old_momentum = ctx.get("momentum_score", 0)
                old_mw = getattr(self.signal_engine, 'momentum_weight', 0.08)

                # Estimate new edge by adjusting for momentum difference
                momentum_delta = (new_momentum * rec_mw) - (old_momentum * old_mw)
                estimated_new_edge = original_edge + momentum_delta

                if estimated_new_edge >= rec_me:
                    candidate_returns.append(o.get("log_return", 0))
            else:
                # Fallback: old-style backtest for outcomes without trade_context
                new_score = sum(
                    snap.get(ind, {}).get("score", 0) * recommended_weights.get(ind, 0)
                    for ind in ["rsi", "macd", "stochastic", "obv", "vwap"]
                )
                if abs(new_score) >= 0.10:
                    candidate_returns.append(o.get("log_return", 0))

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
            if self.signal_engine:
                self.signal_engine.weights = {k: v for k, v in new_weights.items()
                                               if k in ["rsi", "macd", "stochastic", "obv", "vwap"]}
                if "recommended_momentum_weight" in recommendations:
                    self.signal_engine.momentum_weight = recommendations["recommended_momentum_weight"]
                if "recommended_min_edge" in recommendations:
                    self.signal_engine.min_edge = recommendations["recommended_min_edge"]
                    self.signal_engine.entry_threshold = recommendations["recommended_min_edge"]
                if "recommended_kelly_fraction" in recommendations:
                    self.signal_engine.kelly_fraction = recommendations["recommended_kelly_fraction"]

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

    async def run_daily_pipeline(self):
        logger.info("Starting daily learning pipeline")
        analysis = await self._run_bias_detector()
        recommendations = await self._run_ta_evolver(analysis)
        await self._run_weight_optimizer(recommendations)
        logger.info("Daily learning pipeline complete")

    async def run_outcome_loop(self):
        """Periodic outcome review — outcomes are recorded inline by the trading loop.
        This loop exists for future periodic analysis tasks."""
        while self._running:
            await asyncio.sleep(self.outcome_interval_seconds)

    async def run_daily_loop(self):
        while self._running:
            now = datetime.now(timezone.utc)
            if now.hour == self.daily_pipeline_hour and now.minute < 5:
                try:
                    await self.run_daily_pipeline()
                except Exception as e:
                    logger.error(f"Daily pipeline error: {e}")
                    if self.alert_manager:
                        await self.alert_manager.send_error(f"Daily pipeline failed: {e}")
                await asyncio.sleep(3600)
            await asyncio.sleep(60)

    async def start(self):
        self._running = True
        logger.info("Agent scheduler started")

    async def stop(self):
        self._running = False
        logger.info("Agent scheduler stopped")
