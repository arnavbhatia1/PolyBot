"""End-to-end integration leak regressions.

One test per fix landed in INTEGRATION_FIXES.md. Each test would have failed
against the pre-fix code and passes against the post-fix code.
"""
from __future__ import annotations

from pathlib import Path


# ---- Stage 2 — sub_threshold_prob ghost stamps aux_signals ----

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
        "polybot/agents/scheduler.py",
        "polybot/config/settings.yaml",
    ):
        src = Path(path).read_text(encoding="utf-8")
        assert "bybit" not in src.lower(), f"{path} still references bybit"


def test_bybit_feed_module_deleted():
    assert not Path("polybot/feeds/bybit_feed.py").exists()
