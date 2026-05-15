import pytest
from unittest.mock import AsyncMock
from polybot.agents.ta_evolver import TAEvolver


def _make_outcomes(n: int) -> list[dict]:
    return [{"correct": i % 2 == 0, "gain_pct": 0.05 if i % 2 == 0 else -0.10,
             "indicator_snapshot": {
                 "rsi": {"score": 0.8}, "macd": {"score": 0.7},
                 "stochastic": {"score": 0.5}, "obv": {"score": 0.3}, "vwap": {"score": 0.2}}}
            for i in range(n)]


@pytest.mark.asyncio
async def test_evolve_uses_claude_when_available(tmp_path):
    mock = AsyncMock()
    mock.analyze_strategy = AsyncMock(return_value={
        "changes": [{"param": "logit_scale", "value": 4.5, "reason": "test",
                     "predicted_delta_sharpe_7d": 0.02}],
        "manual_observations": [],
        "key_findings": [],
        "risk_warnings": [],
        "reasoning": "test",
        "confidence": "medium",
    })
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "log.md"), claude_client=mock)
    # n=100 trades — clears the BaseRecommender's 50-trade insufficient-data gate
    # so Claude actually gets called and the exploratory probe runs.
    result = await evolver.evolve(_make_outcomes(100), {"overall": {"total_trades": 100}}, {})
    # Claude's logit_scale proposal has predicted_delta 0.02, higher than any
    # exploratory probe (0.005), so it must win the dedupe + sort and sit at index 0.
    params = [c["param"] for c in result["changes"]]
    assert "logit_scale" in params
    assert result["changes"][0]["param"] == "logit_scale"
    mock.analyze_strategy.assert_called_once()
    assert (tmp_path / "log.md").exists()


@pytest.mark.asyncio
async def test_evolve_falls_back_to_local_recommender(tmp_path):
    """LocalRecommender returns empty changes below the 50-trade evidence floor."""
    mock = AsyncMock()
    mock.analyze_strategy = AsyncMock(side_effect=Exception("API down"))
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "log.md"), claude_client=mock)
    result = await evolver.evolve(_make_outcomes(10), {"overall": {"total_trades": 10}}, {})
    assert result["changes"] == []
    assert any("insufficient" in w.lower() or "10 trades" in w
               for w in result.get("risk_warnings", []))


@pytest.mark.asyncio
async def test_evolve_empty_outcomes_returns_empty(tmp_path):
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "log.md"))
    assert await evolver.evolve([], {}, {}) == {}
