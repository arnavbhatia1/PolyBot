"""Side-by-side paper+live: state isolation + the --no-recorder/--no-discord flags.

Running a second instance (live, beside paper) must not contaminate the first.
The isolation contract: every memory path derives from MEMORY_DIR, so setting
POLYBOT_MEMORY_DIR fully separates a second instance's counterfactuals/outcomes/
state — the exact records the exit-edge gate reads. A reintroduced
base_dir/"memory" hardcode would silently leak live records into the paper pool.
"""
import sys
from pathlib import Path

import polybot.paths as P
from polybot.main import parse_args
from polybot.agents.counterfactual_tracker import CounterfactualTracker
from polybot.agents.ghost_tracker import GhostTracker

MAIN_SRC = (P.POLYBOT_DIR / "main.py").read_text(encoding="utf-8")


def _args(argv):
    old = sys.argv
    sys.argv = ["polybot", *argv]
    try:
        return parse_args()
    finally:
        sys.argv = old


def test_flags_default_off():
    a = _args(["--mode", "paper"])
    assert a.no_recorder is False and a.no_discord is False


def test_side_by_side_flags_parse():
    a = _args(["--mode", "live", "--no-recorder", "--no-discord"])
    assert a.no_recorder is True and a.no_discord is True


def test_per_event_dirs_isolate_under_memory_dir():
    # POLYBOT_MEMORY_DIR -> MEMORY_DIR -> every per-event + state dir. Pointing a
    # second instance at its own POLYBOT_MEMORY_DIR therefore isolates all of them.
    for d in (P.OUTCOMES_DIR, P.COUNTERFACTUALS_DIR, P.GHOSTS_DIR, P.STATE_DIR):
        assert d.parent == P.MEMORY_DIR


def test_trackers_route_under_given_memory_dir(tmp_path):
    cf = CounterfactualTracker(memory_dir=str(tmp_path))
    gh = GhostTracker(memory_dir=str(tmp_path))
    assert cf.memory_dir == tmp_path / "counterfactuals"
    assert gh._dir == tmp_path / "ghost_outcomes"


def test_main_has_no_hardcoded_memory_dir():
    # Regression guard for the side-by-side isolation: trackers must derive from
    # MEMORY_DIR, never a hardcoded base_dir/"memory" (which ignores the env var).
    assert 'base_dir / "memory"' not in MAIN_SRC


def test_main_routes_trackers_through_memory_dir():
    assert 'CounterfactualTracker(memory_dir=str(MEMORY_DIR))' in MAIN_SRC
    assert 'GhostTracker(memory_dir=str(MEMORY_DIR))' in MAIN_SRC
    assert 'OutcomeReviewer(outcomes_dir=str(MEMORY_DIR / "outcomes"))' in MAIN_SRC
