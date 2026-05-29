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
        "changes": [
            {"param": "weights",
             "value": {"rsi": 0.22, "macd": 0.23, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20},
             "reason": "rebalance"},
            {"param": "momentum_weight", "value": 0.03, "reason": "test"},
            {"param": "kelly_fraction", "value": 0.15, "reason": "keep"},
        ],
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
            "current_config": {"weights": {"rsi": 0.20}, "min_edge": 0.04},
            "analysis": {"overall": {"total_trades": 100}},
            "trades": [],
        })
    by_param = {c["param"]: c["value"] for c in result["changes"]}
    assert by_param["weights"]["rsi"] == 0.22
    assert by_param["momentum_weight"] == 0.03

def test_validate_renormalizes_weights():
    data = {"changes": [
        {"param": "weights",
         "value": {"rsi": 0.30, "macd": 0.30, "stochastic": 0.30, "obv": 0.30, "vwap": 0.30},
         "reason": "test"},
    ]}
    result = _validate_strategy_response(data, total_trades=100)
    weights = next(c["value"] for c in result["changes"] if c["param"] == "weights")
    assert abs(sum(weights.values()) - 1.0) < 0.01

def test_validate_enforces_momentum_below_min_edge():
    data = {"changes": [{"param": "momentum_weight", "value": 0.15, "reason": "test"}]}
    result = _validate_strategy_response(data, total_trades=100, current_config={"min_edge": 0.10})
    by_param = {c["param"]: c["value"] for c in result["changes"]}
    assert by_param["momentum_weight"] < 0.10

def test_validate_clamps_kelly_fraction():
    data = {"changes": [{"param": "kelly_fraction", "value": 0.50, "reason": "test"}]}
    result = _validate_strategy_response(data, total_trades=100)
    by_param = {c["param"]: c["value"] for c in result["changes"]}
    assert by_param["kelly_fraction"] == 0.18

def test_validate_enforces_min_weight():
    data = {"changes": [
        {"param": "weights",
         "value": {"rsi": 0.01, "macd": 0.50, "stochastic": 0.20, "obv": 0.15, "vwap": 0.14},
         "reason": "test"},
    ]}
    result = _validate_strategy_response(data, current_weights=None, total_trades=100)
    weights = next(c["value"] for c in result["changes"] if c["param"] == "weights")
    assert weights["rsi"] >= 0.05

def test_validate_caps_weight_change_per_cycle():
    current = {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20}
    data = {"changes": [
        {"param": "weights",
         "value": {"rsi": 0.50, "macd": 0.10, "stochastic": 0.15, "obv": 0.10, "vwap": 0.15},
         "reason": "test"},
    ]}
    result = _validate_strategy_response(data, current_weights=current, total_trades=100)
    weights = next(c["value"] for c in result["changes"] if c["param"] == "weights")
    # RSI wanted to jump from 0.20 to 0.50 — per-cycle cap keeps it well under 0.40
    assert weights["rsi"] < 0.40

def test_validate_no_changes_with_few_trades():
    data = {"changes": [{"param": "kelly_fraction", "value": 0.10, "reason": "test"}]}
    result = _validate_strategy_response(data, current_weights=None, total_trades=5)
    assert result["changes"] == []
    assert "insufficient data" in result.get("risk_warnings", [""])[0].lower()

def test_validate_drops_manual_only_params():
    data = {"changes": [
        {"param": "loss_cut_fraction", "value": 0.70, "reason": "ignored"},
        {"param": "atr_sigma_ratio", "value": 1.6, "reason": "kept"},
    ]}
    result = _validate_strategy_response(data, total_trades=100)
    params = {c["param"] for c in result["changes"]}
    assert "loss_cut_fraction" not in params
    assert "atr_sigma_ratio" in params

def test_validate_parses_new_calibration_format():
    """The calibration-rewrite output format must parse through the claude_client validator:
    evidence-driven changes keep their machine-read fields (predicted_delta_sharpe_7d,
    confidence_interval), manual-only params reroute, and the new top-level calibration
    fields pass through untouched. This is the schema-compatibility guarantee for the
    prompt rewrite — if the rewrite renamed any change field, the asserts below break."""
    data = {
        "calibration_self_check": "Last cycle predicted +0.032, realized -0.003; optimistic, shrank.",
        "changes": [
            {"param": "atr_sigma_ratio", "value": 1.45,
             "reason": "N=120; 1.2x floor; 3/4 folds; prior prediction here ran optimistic",
             "predicted_delta_sharpe_7d": 0.004, "confidence_interval": [-0.010, 0.018]},
            {"param": "loss_cut_fraction", "value": 0.70, "reason": "manual — should reroute"},
        ],
        "exploratory_notes": [
            {"param": "prev_margin_weight", "reason": "no evidence yet; gather — not a prediction"},
        ],
        "manual_observations": [],
        "key_findings": ["thin data; most signals sub-floor"],
        "risk_warnings": [],
        "reasoning": "Evidence below the floor; one small calibrated proposal.",
        "confidence": "low",
    }
    result = _validate_strategy_response(data, total_trades=120,
                                         current_config={"min_edge": 0.04})
    by_param = {c["param"]: c for c in result["changes"]}
    # evidence-driven change survives with its machine-read fields intact
    assert "atr_sigma_ratio" in by_param
    assert by_param["atr_sigma_ratio"]["predicted_delta_sharpe_7d"] == 0.004
    assert by_param["atr_sigma_ratio"]["confidence_interval"] == [-0.010, 0.018]
    # manual-only param rerouted out of `changes` into `manual_observations`
    assert "loss_cut_fraction" not in by_param
    assert any(o["param"] == "loss_cut_fraction" for o in result["manual_observations"])
    # new top-level calibration fields are tolerated + preserved by the validator
    assert result["calibration_self_check"].startswith("Last cycle")
    assert result["exploratory_notes"][0]["param"] == "prev_margin_weight"
    # confidence stays machine-readable for the directional logger / ta_evolver log line
    assert result["confidence"] == "low"


def test_validate_accepts_null_recommendation():
    """An empty `changes` list — the correct output under insufficient evidence — parses
    cleanly and is not turned into anything else."""
    data = {
        "calibration_self_check": "No qualifying signal; nothing to shrink.",
        "changes": [],
        "exploratory_notes": [],
        "manual_observations": [],
        "key_findings": ["nothing clears the noise floor this cycle"],
        "risk_warnings": [],
        "reasoning": "Thin data; no change is the calibrated output.",
        "confidence": "low",
    }
    result = _validate_strategy_response(data, total_trades=300)
    assert result["changes"] == []
    assert result["confidence"] == "low"
    assert result["calibration_self_check"]


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
