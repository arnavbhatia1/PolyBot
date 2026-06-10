"""BaseRecommender probe/rotation rules + ClaudeRecommender change merging."""
import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from polybot.agents.claude_recommender import ClaudeRecommender
from polybot.agents.recommender_base import BaseRecommender


def _rec(analysis=None, cfg=None):
    return BaseRecommender(analysis or {}, cfg or {})


# ---------------------------------------------------------------------------
# Structural-probe suppression — any final verdict counts as evidence
# ---------------------------------------------------------------------------

def test_probe_suppressed_by_adopted_value():
    rec = _rec({"cumulative_failures": {
        "exit_edge_threshold": ["-0.08 (Δ=+0.0120, adopted)"]}})
    rec._rule_structural_probes()
    proposed = {(p["param"], p["value"]) for p in rec.proposals}
    assert ("exit_edge_threshold", -0.08) not in proposed
    assert ("exit_edge_threshold", -0.05) in proposed


def test_probe_suppressed_by_backed_out_value():
    rec = _rec({"cumulative_failures": {
        "exit_edge_threshold": ["-0.05 (Δ=+0.0030, backed_out)"]}})
    rec._rule_structural_probes()
    proposed = {(p["param"], p["value"]) for p in rec.proposals}
    assert ("exit_edge_threshold", -0.05) not in proposed


def test_untagged_entry_counts_as_failed_and_tested():
    rec = _rec({"cumulative_failures": {
        "derived_log_atr_ratio_weight": ["0.005 (Δ=-0.0020)"]}})
    assert rec._value_failed("derived_log_atr_ratio_weight", 0.005)
    assert rec._value_tested("derived_log_atr_ratio_weight", 0.005)


def test_adopted_value_not_counted_as_failed():
    rec = _rec({"cumulative_failures": {
        "exit_edge_threshold": ["-0.08 (Δ=+0.0120, adopted)"]}})
    assert not rec._value_failed("exit_edge_threshold", -0.08)
    assert rec._value_tested("exit_edge_threshold", -0.08)


# ---------------------------------------------------------------------------
# Exploratory direction rotation — deterministic across processes
# ---------------------------------------------------------------------------

def test_exploratory_rotation_is_deterministic():
    rec = _rec({}, {"atr_sigma_ratio": 1.5})
    rec._rule_exploratory()
    by_param = {p["param"]: p for p in rec.proposals}
    cycle = datetime.now(timezone.utc).timetuple().tm_yday
    digest = int(hashlib.md5(b"atr_sigma_ratio").hexdigest(), 16)
    expected_up = (cycle + digest) % 2 == 0
    assert (by_param["atr_sigma_ratio"]["value"] > 1.5) == expected_up


# ---------------------------------------------------------------------------
# ClaudeRecommender — per-change defensive numeric conversion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_predicted_delta_does_not_discard_cycle():
    client = MagicMock()
    client.analyze_strategy = AsyncMock(return_value={
        "changes": [
            {"param": "atr_sigma_ratio", "value": 1.45, "reason": "ok",
             "predicted_delta_sharpe_7d": "not-a-number"},
        ],
        "reasoning": "r",
        "confidence": "low",
    })
    rec = ClaudeRecommender({"overall": {"total_trades": 100}},
                            {"atr_sigma_ratio": 1.3}, client)
    out = await rec.recommend()
    by_param = {c["param"]: c for c in out["changes"]}
    assert by_param["atr_sigma_ratio"]["value"] == 1.45
    assert by_param["atr_sigma_ratio"]["predicted_delta_sharpe_7d"] == 0.015
