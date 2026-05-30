"""End-to-end integration leak regressions.

One test per fix landed in INTEGRATION_FIXES.md. Each test would have failed
against the pre-fix code and passes against the post-fix code.
"""
from __future__ import annotations

from pathlib import Path


# ---- Stage 2 — sub_threshold_prob ghost stamps aux_signals ----

def test_sub_threshold_prob_ghost_includes_aux_signals():
    src = Path("polybot/main.py").read_text(encoding="utf-8")
    # Find the sub_threshold_prob ghost block; it must splat aux_signals AND stamp
    # the phase/flip fields (P1-F3) and the cold-feed-aware *_rec flow values (P1-F1).
    idx = src.find('gate_name="sub_threshold_prob"')
    assert idx > 0, "sub_threshold_prob ghost block missing"
    block = src[idx: idx + 2000]
    assert "**aux_signals" in block, (
        "sub_threshold_prob ghost block must splat aux_signals so the 13 "
        "Pillar-1 aux fields parity-match other ghost paths"
    )
    for field in ('"entry_phase"', '"flip_count"', '"is_flip"'):
        assert field in block, f"sub_threshold ghost missing {field} (P1-F3 schema parity)"
    assert "flow_score_rec" in block and "spot_flow_rec" in block, (
        "sub_threshold ghost must record cold-feed-aware *_rec flow values (P1-F1)"
    )


def test_replay_kelly_is_fee_aware():
    """B2: backtest Kelly must mirror live SignalEngine._kelly (net_b = b*(1-fee)),
    not the fee-free edge/(1-price) form, so replay sizes the trades live would."""
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    assert "DEFAULT_FEE_RATE" in src
    assert "edge / (1.0 - market_price_side)" not in src, "replay reverted to fee-free Kelly"
    assert "_net_b = _b * max(1e-6, 1.0 - DEFAULT_FEE_RATE)" in src


def test_local_recommender_proposes_exit_threshold_in_changes():
    """B1: exit_edge_threshold is pipeline-tunable — LocalRecommender must put it in
    `changes` (backtested), never in manual_observations."""
    from polybot.agents.local_recommender import LocalRecommender
    analysis = {
        "overall": {"total_trades": 300},
        "counterfactual_analysis": {"total_scalps_tracked": 120, "net_exit_direction": "hold_long"},
    }
    rec = LocalRecommender(analysis, {"exit_edge_threshold": -0.07}).recommend()
    manual = [m["param"] for m in rec["manual_observations"]]
    changed = [c["param"] for c in rec["changes"]]
    assert "exit_edge_threshold" not in manual, "exit_edge_threshold must not be a manual obs"
    assert "exit_edge_threshold" in changed


def test_orphan_path_strings_point_to_state_subdir():
    """P1-F5: every operator-facing orphan-file reference must point to
    memory/state/orphan_positions.json (where it's actually written), not the
    pre-fix memory/orphan_positions.json."""
    import re
    for rel in ("polybot/main.py", "polybot/execution/live_trader.py"):
        src = Path(rel).read_text(encoding="utf-8")
        # Match the path NOT preceded by 'state/'.
        bad = re.findall(r"(?<!state/)memory/orphan_positions\.json", src)
        assert not bad, f"{rel} references memory/orphan_positions.json without /state/"


# ---- Stage 4 — norm_score redirect removed; backtest reads raw `score` ----

def test_signal_engine_reads_raw_score():
    src = Path("polybot/core/signal_engine.py").read_text(encoding="utf-8")
    # _s(name) must read indicators[name]["score"], not the legacy norm_score.
    assert 'indicators.get(name, {}).get("score", 0)' in src
    assert 'norm_score' not in src


def test_scheduler_replay_reads_raw_score():
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    # _ind_score reads snap[name]["score"]; pre-fix stale norm_score=0 in
    # historical rollups silently zeroed L4 across the 60-day backtest window.
    assert 'snap.get(name, {}).get("score", 0)' in src
    assert 'norm_score' not in src


def test_indicator_engine_does_not_write_norm_score():
    src = Path("polybot/indicators/engine.py").read_text(encoding="utf-8")
    assert 'norm_score' not in src


# ---- Stage 5 — flip_insufficient_edge and sprt_side_mismatch write ghosts ----

def test_flip_insufficient_edge_records_ghost():
    src = Path("polybot/main.py").read_text(encoding="utf-8")
    idx = src.find('_record_skip("flip_insufficient_edge")')
    assert idx > 0
    block = src[idx: idx + 200]
    assert '_ghost("flip_insufficient_edge"' in block


def test_sprt_side_mismatch_records_ghost():
    src = Path("polybot/main.py").read_text(encoding="utf-8")
    idx = src.find('_record_skip("sprt_side_mismatch")')
    assert idx > 0
    block = src[idx: idx + 200]
    assert '_ghost("sprt_side_mismatch"' in block


# ---- Stage 11 — every outcome triggers a gate_stats flush ----

def test_record_outcome_flushes_gate_stats():
    src = Path("polybot/main.py").read_text(encoding="utf-8")
    idx = src.find("async def _record_outcome(")
    assert idx > 0
    block = src[idx: idx + 2000]
    # Background flush keyed off every outcome, not just resolution paths.
    assert "asyncio.create_task(asyncio.to_thread(flush_gate_stats))" in block


def test_resolution_paths_no_longer_double_flush():
    """The previous duplicate flush_gate_stats at the two resolution branches
    was removed when the flush moved into _record_outcome."""
    src = Path("polybot/main.py").read_text(encoding="utf-8")
    # _record_outcome already runs at every outcome path, so the resolution-only
    # flush calls are gone. Count: 1 inside _record_outcome + 1 startup sync.
    count = src.count("asyncio.create_task(asyncio.to_thread(flush_gate_stats))")
    assert count == 1, f"expected single background flush call, found {count}"


# ---- Bybit fully removed ----

def test_bybit_completely_removed():
    for path in (
        "polybot/main.py",
        "polybot/core/aux_layers.py",
        "polybot/agents/scheduler.py",
        "polybot/agents/claude_client.py",
        "polybot/config/settings.yaml",
        "polybot/config/param_registry.py",
    ):
        src = Path(path).read_text(encoding="utf-8")
        assert "bybit" not in src.lower(), f"{path} still references bybit"


def test_bybit_feed_module_deleted():
    assert not Path("polybot/feeds/bybit_feed.py").exists()


# ---- Param registry — L3b description reflects current source; L3e removed ----

def test_param_registry_l3b_description_current():
    src = Path("polybot/config/param_registry.py").read_text(encoding="utf-8")
    assert "L3b Coinbase CVD" in src
    assert "L3b Binance" not in src
    assert "L3e" not in src
    assert "Bybit" not in src
