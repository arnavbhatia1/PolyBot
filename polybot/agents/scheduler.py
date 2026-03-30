import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class AgentScheduler:
    def __init__(self, outcome_reviewer, bias_detector, ta_evolver, weight_optimizer,
                 outcome_interval_seconds=3600, daily_pipeline_hour=2, math_config=None):
        self.outcome_reviewer = outcome_reviewer
        self.bias_detector = bias_detector
        self.ta_evolver = ta_evolver
        self.weight_optimizer = weight_optimizer
        self.outcome_interval_seconds = outcome_interval_seconds
        self.daily_pipeline_hour = daily_pipeline_hour
        self.math_config = math_config or {"ev_threshold": 0.05, "exit_target": 0.90,
                                            "stop_loss_pct": 0.15, "time_stop_hours": 24}
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
        current_weights = self.weight_optimizer.get_scores()
        analysis = self.ta_evolver.analyze(outcomes)
        recommended_weights = self.ta_evolver.recommend_weight_adjustments(outcomes, current_weights)
        self.ta_evolver.save_log(analysis, recommended_weights)
        return recommended_weights

    async def _run_weight_optimizer(self, recommended_weights: dict):
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            logger.info("No outcomes for weight optimization")
            return
        current_version = self.weight_optimizer.get_best_version()
        version_outcomes = [o for o in outcomes if o.get("prompt_version") == current_version]
        if version_outcomes:
            returns = [o.get("log_return", 0) for o in version_outcomes]
            avg_return = sum(returns) / len(returns)
            import math
            variance = sum((r - avg_return) ** 2 for r in returns) / len(returns) if len(returns) > 1 else 1
            std = math.sqrt(variance) if variance > 0 else 1
            sharpe = avg_return / std
            win_rate = sum(1 for o in version_outcomes if o.get("correct", False)) / len(version_outcomes)
            self.weight_optimizer.record_score(current_version, sharpe, len(version_outcomes), win_rate)
        logger.info("Weight optimizer scoring complete")

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
                await asyncio.sleep(3600)
            await asyncio.sleep(60)

    async def start(self):
        self._running = True
        logger.info("Agent scheduler started")

    async def stop(self):
        self._running = False
        logger.info("Agent scheduler stopped")
