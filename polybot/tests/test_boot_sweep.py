"""Boot order sweep (main._boot_order_sweep) — a live-money precondition.

A crashed process can leave resting orders on the exchange; a fill in the gap
is unbooked shares with no DB row. Every outcome has to reach the operator.
"""
import logging

import pytest

from polybot.main import _boot_order_sweep


class _Client:
    def __init__(self, result=None, boom=None):
        self.result, self.boom = result, boom
        self.calls = 0

    def cancel_all(self):
        self.calls += 1
        if self.boom:
            raise self.boom
        return self.result


@pytest.mark.asyncio
async def test_clean_boot_says_nothing_was_carried_over(caplog):
    with caplog.at_level(logging.INFO, logger="polybot"):
        await _boot_order_sweep(_Client({"canceled": [], "not_canceled": {}}))
    assert any("no resting orders carried over" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_real_sweep_reports_the_count(caplog):
    with caplog.at_level(logging.INFO, logger="polybot"):
        await _boot_order_sweep(_Client({"canceled": ["o1", "o2", "o3"]}))
    assert any("cancelled 3 resting order(s)" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_orders_that_refuse_to_cancel_are_an_error(caplog):
    with caplog.at_level(logging.INFO, logger="polybot"):
        await _boot_order_sweep(_Client({"canceled": [], "not_canceled": {"o9": "busy"}}))
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs and "would not cancel" in errs[0].getMessage()


@pytest.mark.asyncio
async def test_a_failed_sweep_is_loud(caplog):
    """It only WARNed while boot carried on with unknown live orders."""
    with caplog.at_level(logging.INFO, logger="polybot"):
        await _boot_order_sweep(_Client(boom=ConnectionError("gateway down")))
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs and "BOOT SWEEP FAILED" in errs[0].getMessage()
