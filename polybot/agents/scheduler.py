"""Nightly job scheduler: record rollups + pluggable nightly jobs.

This scheduler tunes nothing — entry forecasting has no edge over the CLOB price,
so there are no parameter/model optimizers or calibrators to run. Nightly it rolls
per-trade records into daily bundles, then runs whatever jobs are registered
(exit-value model refit, wallet-markout classification — Phases 3/4 of tasks/todo.md).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

NightlyJob = Callable[[], Awaitable[dict[str, Any] | None]]


class NightlyScheduler:
    def __init__(self, outcome_reviewer: Any,
                 counterfactual_tracker: Any = None,
                 ghost_tracker: Any = None,
                 alert_manager: Any = None,
                 outcome_interval_seconds: int = 3600,
                 daily_pipeline_hour: int = 2,
                 daily_pipeline_minute: int = 0,
                 config: dict[str, Any] | None = None) -> None:
        self.outcome_reviewer = outcome_reviewer
        self.counterfactual_tracker = counterfactual_tracker
        self.ghost_tracker = ghost_tracker
        self.alert_manager = alert_manager
        self.outcome_interval_seconds = outcome_interval_seconds
        self.daily_pipeline_hour = daily_pipeline_hour
        self.daily_pipeline_minute = daily_pipeline_minute
        self._config = config
        self._running = False
        self._auto_shutdown = False
        self._shutdown_requested = False
        # Runtime knobs the trading loop reads (set by main at boot).
        self._exit_edge_threshold: float | None = None
        self._min_time_remaining: int | None = None
        self._trading_start: tuple[int, int] | None = None
        self._trading_end: tuple[int, int] | None = None
        # Registered by main at boot (model refit, wallet classification, ...).
        self.nightly_jobs: list[tuple[str, NightlyJob]] = []

    def register_job(self, name: str, job: NightlyJob) -> None:
        self.nightly_jobs.append((name, job))

    async def run_daily_pipeline(self) -> None:
        now = datetime.now(timezone.utc)
        logger.info(f"─── Nightly jobs starting — {now.strftime('%b %d, %Y %I:%M %p UTC')} ───")

        def _safe_rollup(name: str, fn) -> int:
            try:
                return fn()
            except Exception as e:
                logger.error(f"Rollup '{name}' failed: {e}")
                return 0

        rolled = _safe_rollup("outcomes", self.outcome_reviewer.rollup_old_outcomes)
        ghost_rolled = (_safe_rollup("ghosts", self.ghost_tracker.rollup_old_ghosts)
                        if self.ghost_tracker else 0)
        cf_rolled = (_safe_rollup("counterfactuals",
                                  self.counterfactual_tracker.rollup_old_counterfactuals)
                     if self.counterfactual_tracker else 0)
        logger.info(f"  Rolled up: {rolled} outcomes, {cf_rolled} counterfactuals, {ghost_rolled} ghosts")

        for name, job in self.nightly_jobs:
            try:
                result = await job()
                logger.info(f"  Nightly job '{name}': {result if result else 'ok'}")
            except Exception as e:
                logger.error(f"Nightly job '{name}' failed: {e}")
                if self.alert_manager:
                    try:
                        await self.alert_manager.send_error(f"Nightly job '{name}' failed: {e}")
                    except Exception:
                        pass

        logger.info("─── Nightly jobs complete ───")

    async def run_outcome_loop(self) -> None:
        """Heartbeat slot for periodic analysis tasks — records are written inline
        by the trading loop."""
        while self._running:
            await asyncio.sleep(self.outcome_interval_seconds)

    async def run_daily_loop(self) -> None:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        while self._running:
            now = datetime.now(ET)
            if (now.hour == self.daily_pipeline_hour
                    and self.daily_pipeline_minute <= now.minute < self.daily_pipeline_minute + 5):
                try:
                    await self.run_daily_pipeline()
                except Exception as e:
                    logger.error(f"Nightly pipeline error: {e}")
                    if self.alert_manager:
                        await self.alert_manager.send_error(f"Nightly pipeline failed: {e}")
                if self._auto_shutdown:
                    logger.info("Pipeline complete")
                    self._shutdown_requested = True
                    return
                await asyncio.sleep(3600)
            await asyncio.sleep(60)

    async def start(self) -> None:
        self._running = True
        logger.debug("Nightly scheduler started")

    async def stop(self) -> None:
        self._running = False
        logger.debug("Nightly scheduler stopped")
