import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from polybot.agents.scheduler import AgentScheduler

@pytest.fixture
def scheduler():
    return AgentScheduler(outcome_reviewer=MagicMock(), bias_detector=MagicMock(),
        strategy_evolver=MagicMock(), prompt_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2,
        math_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24})

def test_scheduler_has_all_agents(scheduler):
    assert scheduler.outcome_reviewer is not None
    assert scheduler.bias_detector is not None
    assert scheduler.strategy_evolver is not None
    assert scheduler.prompt_optimizer is not None

@pytest.mark.asyncio
async def test_run_daily_pipeline_calls_agents_in_order():
    call_order = []
    async def mock_bias():
        call_order.append("bias")
        return {"politics": -0.1}
    async def mock_strategy(biases):
        call_order.append("strategy")
        return []
    async def mock_prompt(recs):
        call_order.append("prompt")
    scheduler = AgentScheduler(outcome_reviewer=MagicMock(), bias_detector=MagicMock(),
        strategy_evolver=MagicMock(), prompt_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2,
        math_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24})
    scheduler._run_bias_detector = mock_bias
    scheduler._run_strategy_evolver = mock_strategy
    scheduler._run_prompt_optimizer = mock_prompt
    await scheduler.run_daily_pipeline()
    assert call_order == ["bias", "strategy", "prompt"]
