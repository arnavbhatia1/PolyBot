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

@pytest.mark.asyncio
async def test_evolve_with_claude_success(tmp_path):
    """When Claude returns valid recommendations, evolve() uses them."""
    mock_claude = AsyncMock()
    mock_claude.analyze_strategy = AsyncMock(return_value={
        "changes": [
            {"param": "weights",
             "value": {"rsi": 0.22, "macd": 0.23, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20},
             "reason": "rebalance"},
            {"param": "momentum_weight", "value": 0.07, "reason": "test"},
        ],
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
    by_param = {c["param"]: c["value"] for c in result["changes"]}
    assert by_param["weights"]["rsi"] == 0.22
    assert by_param["momentum_weight"] == 0.07
    mock_claude.analyze_strategy.assert_called_once()
    assert (tmp_path / "strategy_log.md").exists()

@pytest.mark.asyncio
async def test_evolve_falls_back_on_claude_failure(tmp_path):
    """When Claude raises an exception, evolve() falls back to LocalRecommender.

    LocalRecommender returns the same JSON shape Claude would, with `changes`
    possibly empty when sample size is below the 50-trade evidence floor.
    """
    mock_claude = AsyncMock()
    mock_claude.analyze_strategy = AsyncMock(side_effect=Exception("API error"))
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"),
                        claude_client=mock_claude)
    outcomes = _make_outcomes(10)
    result = await evolver.evolve(outcomes, {"overall": {"total_trades": 10}},
                                  {"weights": {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                               "obv": 0.15, "vwap": 0.20}})
    assert "changes" in result
    # Below 50 trades: insufficient-data warning, no changes proposed
    assert result["changes"] == []
    assert any("insufficient" in w.lower() or "10 trades" in w for w in result.get("risk_warnings", []))

@pytest.mark.asyncio
async def test_evolve_without_claude_client(tmp_path):
    """When no claude_client is provided, evolve() uses local math directly."""
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"),
                        claude_client=None)
    outcomes = _make_outcomes(10)
    result = await evolver.evolve(outcomes, {},
                                  {"weights": {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                               "obv": 0.15, "vwap": 0.20}})
    assert "changes" in result

@pytest.mark.asyncio
async def test_evolve_empty_outcomes(tmp_path):
    evolver = TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"))
    result = await evolver.evolve([], {}, {})
    assert result == {}
