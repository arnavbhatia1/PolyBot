"""NightlyScheduler job budget — a job that overruns is ABANDONED, not stopped.

Every nightly job body is asyncio.to_thread, so dropping the await leaves the
thread running. The log has to say so: a line reading "skipped" sent the
operator hunting for a job that was in fact still reading the tape.
"""
import asyncio
import logging
import threading

from polybot.agents import scheduler as sched_mod
from polybot.agents.scheduler import NightlyScheduler


class _FakeReviewer:
    def rollup_old_outcomes(self):
        return 0


def test_overrunning_job_is_reported_as_abandoned_not_skipped(caplog, monkeypatch):
    monkeypatch.setattr(sched_mod, "JOB_BUDGET_S", 0.2)
    s = NightlyScheduler(outcome_reviewer=_FakeReviewer())
    release = threading.Event()

    def _block():
        release.wait(10.0)

    async def _slow():
        await asyncio.to_thread(_block)

    s.register_job("slow", _slow)

    async def _go():
        with caplog.at_level(logging.ERROR):
            await s.run_daily_pipeline()

    asyncio.run(_go())
    release.set()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("abandoned after" in m and "may still be running" in m for m in msgs)
    assert not any("skipped" in m for m in msgs)
