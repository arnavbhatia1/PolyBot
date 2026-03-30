import asyncio
import math
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class AgentScheduler:
    def __init__(self, outcome_reviewer, bias_detector, ta_evolver, weight_optimizer,
                 indicator_engine=None, signal_engine=None, alert_manager=None,
                 outcome_interval_seconds=3600, daily_pipeline_hour=2, math_config=None):
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
        self._running = False

    async def _run_bias_detector(self) -> dict:
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            logger.info("No outcomes to analyze for biases")
            return {}
        biases = self.bias_detector.detect(outcomes)
        self.bias_detector.save(biases)
        return biases

    async def _run_ta_evolver(self, biases: dict) -> dict:
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            return {}
        current_weights = self.indicator_engine.get_weights() if self.indicator_engine else {}
        analysis = self.ta_evolver.analyze(outcomes)
        recommended_weights = self.ta_evolver.recommend_weight_adjustments(outcomes, current_weights)
        self.ta_evolver.save_log(analysis, recommended_weights)
        return recommended_weights

    async def _run_weight_optimizer(self, recommended_weights: dict):
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

        # If no recommended weights, nothing to test
        if not recommended_weights:
            return

        # Simulate: what would Sharpe have been with recommended weights?
        # Use all outcomes that have indicator snapshots
        scored_outcomes = [o for o in outcomes if o.get("indicator_snapshot")]
        if len(scored_outcomes) < 5:
            return

        # Recompute hypothetical scores with new weights
        candidate_returns = []
        for o in scored_outcomes:
            snap = o.get("indicator_snapshot", {})
            new_score = sum(
                snap.get(ind, {}).get("score", 0) * recommended_weights.get(ind, 0)
                for ind in ["rsi", "macd", "stochastic", "obv", "vwap"]
            )
            # Would this trade have been taken with new weights?
            threshold = recommended_weights.get("entry_threshold", 0.40)
            if abs(new_score) >= threshold:
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
            # Auto-adopt new weights
            new_version = self.weight_optimizer.get_next_version()
            new_weights = recommended_weights.copy()
            new_weights["version"] = new_version
            self.weight_optimizer.save_weights(new_version, new_weights)
            self.weight_optimizer.record_score(new_version, candidate_sharpe, len(candidate_returns), candidate_win_rate)

            # Hot-swap in the running engine
            if self.indicator_engine:
                self.indicator_engine.set_active_version(new_version)
            if self.signal_engine and "entry_threshold" in new_weights:
                self.signal_engine.entry_threshold = new_weights["entry_threshold"]
                self.signal_engine.weights = {k: v for k, v in new_weights.items()
                                               if k in ["rsi", "macd", "stochastic", "obv", "vwap"]}

            logger.info(f"AUTO-ADOPTED {new_version}: Sharpe {current_sharpe:.3f} -> {candidate_sharpe:.3f}, "
                        f"win rate {candidate_win_rate:.0%}")

            if self.alert_manager:
                await self.alert_manager.send_pipeline_summary(
                    f"**Weights auto-updated: {current_version} -> {new_version}**\n"
                    f"Sharpe: `{current_sharpe:.3f}` -> `{candidate_sharpe:.3f}`\n"
                    f"Win rate: `{candidate_win_rate:.0%}`\n"
                    f"Trades evaluated: `{len(candidate_returns)}`\n"
                    f"New weights: {', '.join(f'{k}={v:.2f}' for k, v in new_weights.items() if k in ['rsi','macd','stochastic','obv','vwap'])}"
                )

        elif candidate_sharpe < -0.5:
            # Something looks seriously wrong — flag it
            logger.warning(f"NEGATIVE SHARPE detected: {candidate_sharpe:.3f} — flagging in Discord")
            if self.alert_manager:
                await self.alert_manager.send_error(
                    f"WARNING: Strategy performing poorly.\n"
                    f"Current Sharpe: {current_sharpe:.3f}\n"
                    f"Candidate Sharpe: {candidate_sharpe:.3f}\n"
                    f"Win rate: {candidate_win_rate:.0%}\n"
                    f"Consider pausing (!pause) and reviewing trades (!history 20)"
                )
        else:
            logger.info(f"Weights not adopted: improvement {candidate_sharpe - current_sharpe:.3f} "
                        f"below threshold {self.weight_optimizer.min_improvement}")
            if self.alert_manager:
                await self.alert_manager.send_pipeline_summary(
                    f"**Learning pipeline complete — no weight change**\n"
                    f"Current Sharpe: `{current_sharpe:.3f}`\n"
                    f"Candidate Sharpe: `{candidate_sharpe:.3f}`\n"
                    f"Improvement `{candidate_sharpe - current_sharpe:.3f}` below threshold `{self.weight_optimizer.min_improvement}`"
                )

    async def run_daily_pipeline(self):
        logger.info("Starting daily learning pipeline")
        biases = await self._run_bias_detector()
        recommended_weights = await self._run_ta_evolver(biases)
        await self._run_weight_optimizer(recommended_weights)
        logger.info("Daily learning pipeline complete")

    async def run_outcome_loop(self):
        while self._running:
            try:
                logger.debug("Running outcome reviewer")
            except Exception as e:
                logger.error(f"Outcome reviewer error: {e}")
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
