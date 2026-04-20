import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.agents.claude_client import (
    ClaudeClient, _validate_strategy_response, _format_strategy_context,
)

@pytest.mark.asyncio
async def test_analyze_strategy_returns_recommendations():
    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = json.dumps({
        "recommended_weights": {"rsi": 0.22, "macd": 0.23, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20},
        "recommended_momentum_weight": 0.07,
        "recommended_min_edge": 0.10,
        "recommended_kelly_fraction": 0.15,
        "key_findings": ["RSI bullish accuracy high"],
        "risk_warnings": [],
        "reasoning": "Analysis shows...",
        "confidence": "medium",
    })
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    with patch("polybot.agents.claude_client.anthropic.AsyncAnthropic", return_value=mock_client):
        client = ClaudeClient(api_key="test-key")
        result = await client.analyze_strategy({
            "current_config": {"weights": {"rsi": 0.20}},
            "analysis": {"overall": {"total_trades": 50}},
            "trades": [],
        })
    assert result["recommended_weights"]["rsi"] == 0.22
    assert result["recommended_momentum_weight"] == 0.07

def test_validate_renormalizes_weights():
    data = {
        "recommended_weights": {"rsi": 0.30, "macd": 0.30, "stochastic": 0.30, "obv": 0.30, "vwap": 0.30},
        "recommended_momentum_weight": 0.08,
        "recommended_min_edge": 0.10,
        "recommended_kelly_fraction": 0.15,
    }
    result = _validate_strategy_response(data)
    total = sum(result["recommended_weights"].values())
    assert abs(total - 1.0) < 0.01

def test_validate_enforces_momentum_below_min_edge():
    data = {
        "recommended_weights": {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20},
        "recommended_momentum_weight": 0.15,
        "recommended_min_edge": 0.10,
        "recommended_kelly_fraction": 0.15,
    }
    result = _validate_strategy_response(data)
    assert result["recommended_momentum_weight"] < result["recommended_min_edge"]

def test_validate_clamps_kelly_fraction():
    data = {
        "recommended_weights": {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20},
        "recommended_momentum_weight": 0.08,
        "recommended_min_edge": 0.10,
        "recommended_kelly_fraction": 0.50,
    }
    result = _validate_strategy_response(data)
    assert result["recommended_kelly_fraction"] == 0.25

def test_validate_enforces_min_weight():
    data = {
        "recommended_weights": {"rsi": 0.01, "macd": 0.50, "stochastic": 0.20, "obv": 0.15, "vwap": 0.14},
        "recommended_momentum_weight": 0.08,
        "recommended_min_edge": 0.10,
        "recommended_kelly_fraction": 0.15,
    }
    result = _validate_strategy_response(data, current_weights=None, total_trades=50)
    assert result["recommended_weights"]["rsi"] >= 0.05

def test_validate_caps_weight_change_per_cycle():
    current = {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20}
    data = {
        "recommended_weights": {"rsi": 0.50, "macd": 0.10, "stochastic": 0.15, "obv": 0.10, "vwap": 0.15},
        "recommended_momentum_weight": 0.08,
        "recommended_min_edge": 0.10,
        "recommended_kelly_fraction": 0.15,
    }
    result = _validate_strategy_response(data, current_weights=current, total_trades=50)
    # RSI wanted to jump from 0.20 to 0.50 — cap + renormalization prevents radical change
    # The raw 0.30 jump gets capped, then renormalization distributes residuals
    assert result["recommended_weights"]["rsi"] < 0.40  # Far less than the 0.50 requested

def test_validate_no_changes_with_few_trades():
    current = {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20}
    data = {
        "recommended_weights": {"rsi": 0.50, "macd": 0.10, "stochastic": 0.15, "obv": 0.10, "vwap": 0.15},
        "recommended_momentum_weight": 0.08,
        "recommended_min_edge": 0.10,
        "recommended_kelly_fraction": 0.15,
    }
    result = _validate_strategy_response(data, current_weights=current, total_trades=5)
    # Should keep current weights since < 50 trades
    assert result["recommended_weights"]["rsi"] == 0.20
    assert "insufficient data" in result.get("risk_warnings", [""])[0].lower()

def test_format_strategy_context_includes_sections():
    context = {
        "current_config": {"weights": {"rsi": 0.20}, "momentum_weight": 0.08, "min_edge": 0.10},
        "analysis": {"overall": {"total_trades": 50, "win_rate": 0.55, "avg_edge": 0.14,
                                  "avg_gain_pct": 0.005, "sharpe": 0.8}},
        "trades": [{"correct": True, "side": "Up", "entry_price": 0.50, "exit_price": 1.0,
                     "log_return": 0.5, "signal_score": 0.7, "indicator_snapshot": {}}],
        "previous_recommendations": "## previous\nsome text",
    }
    text = _format_strategy_context(context)
    assert "Current Configuration" in text
    assert "Overall Performance" in text
    assert "Recent Trades" in text
    assert "Previous Recommendations" in text
