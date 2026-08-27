"""The margin-table freeze scripts (scripts/research/ws1_*).

signal_engine documents MAX as per-tick INTERVAL maxima. The engine
interpolates margin(k) at every tick, so a MAX fitted at grid points can sit
below the true error between knots — re-running the freeze that way at the
>=14-day re-fit would ship under-bounding MAX knots.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

RESEARCH = Path(__file__).resolve().parents[2] / "scripts" / "research"


def _load(name):
    if str(RESEARCH) not in sys.path:      # siblings import each other by name
        sys.path.insert(0, str(RESEARCH))
    spec = importlib.util.spec_from_file_location(name, RESEARCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_knot_bounds_both_adjacent_intervals():
    m = _load("ws1_interval_max")
    n = len(m.KNOTS)
    real = [0.0] * n
    all_ = [0.0] * n
    all_[3] = 4.2                       # a tick between KNOTS[2] and KNOTS[3]
    knots = dict(m.interval_max_knots(real, all_))
    # both knots bracketing that interval must bound it, so interpolation does too
    assert knots[m.KNOTS[2]] >= 4.2 and knots[m.KNOTS[3]] >= 4.2


def test_knots_are_monotone_and_rounded_up():
    m = _load("ws1_interval_max")
    n = len(m.KNOTS)
    all_ = [0.0] * n
    all_[1], all_[5] = 9.1, 2.0         # a big low-k interval, a small later one
    out = m.interval_max_knots([0.0] * n, all_)
    vals = [v for _k, v in out]
    assert vals == sorted(vals), "MAX must never decrease in k"
    assert all(v == int(v) for v in vals), "MAX knots round up to whole dollars"
    assert dict(out)[m.KNOTS[0]] >= 10.0


def test_freeze_script_sources_max_from_the_interval_maxima():
    src = (RESEARCH / "ws1_freeze_tables.py").read_text(encoding="utf-8")
    assert "interval_max_knots" in src, \
        "the freeze script must take MAX from ws1_interval_max"
    assert "max_r[k], max_a" not in src, \
        "the freeze script still fits MAX at grid points"


def test_engine_tables_untouched():
    """The re-fit is a scheduled measurement; this fix must not move a knot."""
    from polybot.core.signal_engine import TWAP_MARGIN_MAX, TWAP_MARGIN_P995
    assert TWAP_MARGIN_P995[2] == (6.0, 4.0) and TWAP_MARGIN_P995[8] == (25.0, 28.5)
    assert TWAP_MARGIN_MAX[8] == (25.0, 100.0) and TWAP_MARGIN_MAX[-1] == (58.0, 371.0)
