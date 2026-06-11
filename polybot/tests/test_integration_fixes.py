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


# ---- Stage 5 — flip_insufficient_edge writes ghosts ----

def test_flip_insufficient_edge_records_ghost():
    src = Path("polybot/main.py").read_text(encoding="utf-8")
    idx = src.find('_record_skip("flip_insufficient_edge")')
    assert idx > 0
    block = src[idx: idx + 200]
    assert '_ghost("flip_insufficient_edge"' in block


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
        "polybot/config/settings.yaml",
        "polybot/config/param_registry.py",
    ):
        src = Path(path).read_text(encoding="utf-8")
        assert "bybit" not in src.lower(), f"{path} still references bybit"


def test_bybit_feed_module_deleted():
    assert not Path("polybot/feeds/bybit_feed.py").exists()
