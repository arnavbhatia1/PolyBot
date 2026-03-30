import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.brain.claude_client import ClaudeClient, MarketAnalysis

def test_market_analysis_from_valid_json():
    data = {"probability": 0.72, "confidence": "high", "reasoning": "Strong indicators",
            "key_factors": ["factor1", "factor2"], "base_rate_considered": True}
    analysis = MarketAnalysis.from_dict(data)
    assert analysis.probability == 0.72
    assert analysis.confidence == "high"

def test_market_analysis_rejects_invalid_probability():
    data = {"probability": 1.5, "confidence": "high", "reasoning": "Bad",
            "key_factors": [], "base_rate_considered": True}
    with pytest.raises(ValueError, match="probability"):
        MarketAnalysis.from_dict(data)

def test_market_analysis_rejects_invalid_confidence():
    data = {"probability": 0.5, "confidence": "super_high", "reasoning": "Bad",
            "key_factors": [], "base_rate_considered": True}
    with pytest.raises(ValueError, match="confidence"):
        MarketAnalysis.from_dict(data)

def test_passes_confidence_gate_high():
    analysis = MarketAnalysis(probability=0.72, confidence="high", reasoning="test",
                              key_factors=[], base_rate_considered=True)
    assert analysis.passes_gate(min_confidence="high", min_probability=0.65) is True

def test_fails_confidence_gate_medium():
    analysis = MarketAnalysis(probability=0.72, confidence="medium", reasoning="test",
                              key_factors=[], base_rate_considered=True)
    assert analysis.passes_gate(min_confidence="high", min_probability=0.65) is False

def test_fails_probability_gate():
    analysis = MarketAnalysis(probability=0.55, confidence="high", reasoning="test",
                              key_factors=[], base_rate_considered=True)
    assert analysis.passes_gate(min_confidence="high", min_probability=0.65) is False

@pytest.mark.asyncio
async def test_analyze_market_returns_analysis():
    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = json.dumps({
        "probability": 0.72, "confidence": "high", "reasoning": "Test reasoning",
        "key_factors": ["factor1"], "base_rate_considered": True})
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    with patch("polybot.brain.claude_client.anthropic.AsyncAnthropic", return_value=mock_client):
        client = ClaudeClient(api_key="test-key", model="claude-sonnet-4-6")
        result = await client.analyze_market(
            question="Will X happen?", price=0.55, volume=5000, liquidity=2000,
            spread=0.02, days_to_expiry=15, prompt="Analyze this market.")
        assert isinstance(result, MarketAnalysis)
        assert result.probability == 0.72
