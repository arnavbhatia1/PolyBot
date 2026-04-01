import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from polybot.agents.ta_evolver import TAEvolver

@pytest.fixture
def evolver(tmp_path):
    return TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"))

def _make_outcomes(n=10, correct=True):
    return [{"correct": correct, "log_return": 0.05 if correct else -0.10,
             "indicator_snapshot": {"rsi": {"score": 0.8}, "macd": {"score": 0.7},
              "stochastic": {"score": 0.5}, "obv": {"score": 0.3}, "vwap": {"score": 0.2}}}
            for _ in range(n)]

def test_analyze_computes_stats(evolver):
    outcomes = _make_outcomes(2, True) + _make_outcomes(1, False)
    analysis = evolver.analyze(outcomes)
    assert analysis["total_trades"] == 3 and "win_rate" in analysis

def test_recommend_weights(evolver):
    outcomes = _make_outcomes(10)
    recs = evolver.recommend_weight_adjustments(outcomes,
        {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20})
    assert isinstance(recs, dict) and "rsi" in recs

def test_save_log(evolver, tmp_path):
    evolver.save_log({"win_rate": 0.65, "total_trades": 15}, {"rsi": 0.22})
    assert (tmp_path / "strategy_log.md").exists()

@pytest.mark.asyncio
async def test_evolve_with_claude_success(tmp_path):
    """When Claude returns valid recommendations, evolve() uses them."""
    mock_claude = AsyncMock()
    mock_claude.analyze_strategy = AsyncMock(return_value={
        "recommended_weights": {"rsi": 0.22, "macd": 0.23, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20},
        "recommended_momentum_weight": 0.07,
        "recommended_min_edge": 0.12,
        "recommended_kelly_fraction": 0.14,
        "key_findings": ["RSI outperforms"],
        "risk_warnings": [],
        "reasoning": "Based on analysis...",
        "confidence": "medium",
    })
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"),
                        claude_client=mock_claude)
    outcomes = _make_outcomes(20)
    result = await evolver.evolve(outcomes, {"overall": {"total_trades": 20}},
                                  {"weights": {"rsi": 0.20}, "momentum_weight": 0.08})
    assert result["recommended_weights"]["rsi"] == 0.22
    assert result["recommended_momentum_weight"] == 0.07
    mock_claude.analyze_strategy.assert_called_once()
    assert (tmp_path / "strategy_log.md").exists()

@pytest.mark.asyncio
async def test_evolve_falls_back_on_claude_failure(tmp_path):
    """When Claude raises an exception, evolve() falls back to local math."""
    mock_claude = AsyncMock()
    mock_claude.analyze_strategy = AsyncMock(side_effect=Exception("API error"))
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"),
                        claude_client=mock_claude)
    outcomes = _make_outcomes(10)
    result = await evolver.evolve(outcomes, {},
                                  {"weights": {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                               "obv": 0.15, "vwap": 0.20}})
    assert "recommended_weights" in result
    assert "rsi" in result["recommended_weights"]

@pytest.mark.asyncio
async def test_evolve_without_claude_client(tmp_path):
    """When no claude_client is provided, evolve() uses local math directly."""
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"),
                        claude_client=None)
    outcomes = _make_outcomes(10)
    result = await evolver.evolve(outcomes, {},
                                  {"weights": {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                               "obv": 0.15, "vwap": 0.20}})
    assert "recommended_weights" in result

@pytest.mark.asyncio
async def test_evolve_empty_outcomes(tmp_path):
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"))
    result = await evolver.evolve([], {}, {})
    assert result == {}
