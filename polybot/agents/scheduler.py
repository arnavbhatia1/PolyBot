import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class AgentScheduler:
    def __init__(self, outcome_reviewer, bias_detector, strategy_evolver, prompt_optimizer,
                 outcome_interval_seconds=3600, daily_pipeline_hour=2, math_config=None):
        self.outcome_reviewer = outcome_reviewer
        self.bias_detector = bias_detector
        self.strategy_evolver = strategy_evolver
        self.prompt_optimizer = prompt_optimizer
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

    async def _run_strategy_evolver(self, biases: dict) -> list:
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            return []
        analysis = self.strategy_evolver.analyze_local(outcomes, current_config=self.math_config)
        recs = self.strategy_evolver.generate_recommendations(analysis, current_config=self.math_config)
        self.strategy_evolver.save_log(recs, analysis)
        return recs

    async def _run_prompt_optimizer(self, recommendations: list):
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            logger.info("No outcomes for prompt optimization")
            return
        current_version = self.prompt_optimizer.get_best_version()
        version_outcomes = [o for o in outcomes if o.get("prompt_version") == current_version]
        if version_outcomes:
            accuracy = sum(1 for o in version_outcomes if o["correct"]) / len(version_outcomes)
            self.prompt_optimizer.record_score(current_version, accuracy, len(version_outcomes))
        logger.info("Prompt optimizer scoring complete")

    async def run_daily_pipeline(self):
        logger.info("Starting daily learning pipeline")
        biases = await self._run_bias_detector()
        recommendations = await self._run_strategy_evolver(biases)
        await self._run_prompt_optimizer(recommendations)
        logger.info("Daily learning pipeline complete")

    async def run_outcome_loop(self):
        while self._running:
            try:
                logger.info("Running outcome reviewer")
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
