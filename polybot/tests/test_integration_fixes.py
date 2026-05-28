"""End-to-end integration leak regressions.

One test per fix landed in INTEGRATION_FIXES.md. Each test would have failed
against the pre-fix code and passes against the post-fix code.
"""
from __future__ import annotations

from pathlib import Path


# ---- Stage 2 — sub_threshold_prob ghost stamps aux_signals ----

def test_sub_threshold_prob_ghost_includes_aux_signals():
    src = Path("polybot/main.py").read_text(encoding="utf-8")
    # Find the sub_threshold_prob ghost block; it must splat aux_signals.
    idx = src.find('gate_name="sub_threshold_prob"')
    assert idx > 0, "sub_threshold_prob ghost block missing"
    block = src[idx: idx + 1500]
    assert "**aux_signals" in block, (
        "sub_threshold_prob ghost block must splat aux_signals so the 13 "
        "Pillar-1 aux fields parity-match other ghost paths"
    )


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


# ---- Param registry — L3b/L3e descriptions reflect current sources ----

def test_param_registry_l3b_l3e_descriptions_current():
    src = Path("polybot/config/param_registry.py").read_text(encoding="utf-8")
    assert "L3b Coinbase CVD" in src
    assert "L3e Binance forceOrder" in src
    assert "L3b Binance" not in src
    assert "Bybit" not in src
