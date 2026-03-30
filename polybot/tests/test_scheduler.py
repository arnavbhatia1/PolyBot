import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from polybot.agents.scheduler import AgentScheduler

@pytest.fixture
def scheduler():
    return AgentScheduler(outcome_reviewer=MagicMock(), bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2,
        math_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24})

def test_scheduler_has_all_agents(scheduler):
    assert scheduler.outcome_reviewer is not None
    assert scheduler.bias_detector is not None
    assert scheduler.ta_evolver is not None
    assert scheduler.weight_optimizer is not None

@pytest.mark.asyncio
async def test_run_daily_pipeline_calls_agents_in_order():
    call_order = []
    async def mock_bias():
        call_order.append("bias")
        return {"politics": -0.1}
    async def mock_ta_evolver(biases):
        call_order.append("ta_evolver")
        return {}
    async def mock_weight_optimizer(recs):
        call_order.append("weight_optimizer")
    scheduler = AgentScheduler(outcome_reviewer=MagicMock(), bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2,
        math_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24})
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = mock_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()
    assert call_order == ["bias", "ta_evolver", "weight_optimizer"]
