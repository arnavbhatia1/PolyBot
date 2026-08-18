"""The TWAP lock signal (evaluate_twap_lock) and the max-tier gate.

The signal mirrors scripts/analyze_twap_lock.py exactly: displacement of the
projected final TWAP past the frozen error margin, bought only while the
winner's ask still clears the edge floor — and, by default, only on the tier
that has never breached.
"""
import ast
import time
from pathlib import Path

import pytest

from polybot.core.signal_engine import (
    SignalEngine, TWAP_MARGIN_MAX, TWAP_MARGIN_P995,
    TWAP_PROB_DETERMINISTIC, TWAP_PROB_P995, twap_margin,
)


def test_phase_assigned_before_any_ghost_call():
    """Regression: _evaluate_signal_and_enter's nested _ghost() reads `phase` (and
    other enclosing free vars) from base_ctx. A sniper_only suppression ghost fires
    early, so `phase` MUST be assigned before the first _ghost() call — a NameError
    here crashed every live tick when the assignment was accidentally removed.
    Static guard: no mocking, catches the free-var-before-use regardless of runtime
    path. Extend the checked set if base_ctx gains more enclosing vars."""
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_evaluate_signal_and_enter")

    # First _ghost(...) call inside the function.
    ghost_lines = [n.lineno for n in ast.walk(fn)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_ghost"]
    assert ghost_lines, "no _ghost() call found — test is stale"
    first_ghost = min(ghost_lines)

    # Enclosing free vars that _ghost's base_ctx reads; each must be bound before the ghost.
    # (raw_prob_side / _closes_tail / _ghost_flip_count are local to _ghost, not checked here.)
    for var in ("aux_signals", "phase", "_signal_leg", "_proj"):
        assigns = [t.lineno
                   for node in ast.walk(fn) if isinstance(node, ast.Assign)
                   for t in ast.walk(node)
                   if isinstance(t, ast.Name) and t.id == var and isinstance(t.ctx, ast.Store)]
        assigns = [ln for ln in assigns if ln < first_ghost]
        assert assigns, f"'{var}' is read by _ghost's base_ctx but never assigned before the first _ghost() call (line {first_ghost}) — free-var-before-use"


def _eng():
    return SignalEngine()


def _lock(eng, proj, strike, k, ask_up, ask_down, min_edge=0.04,
          zone=30.0, k_min=0.8, require_max_tier=False):
    """require_max_tier defaults FALSE here so these cases can still exercise the
    two-tier rule; production defaults it TRUE (see the max-tier gate tests)."""
    return eng.evaluate_twap_lock(proj, strike, k, ask_up, ask_down,
                                  zone, k_min, min_edge,
                                  require_max_tier=require_max_tier)


# ───────────────────────── the max-tier gate (production default) ─────────────
def test_max_tier_gate_refuses_the_p995_tier():
    """The p99.5 band has realized breaches (the 08-11 30s-era one lost a whole
    stake while the max bound held through it). A displacement between the two
    margins is exactly that trade — max tier must refuse it."""
    eng = _eng()
    m995 = twap_margin(TWAP_MARGIN_P995, 19.0)
    mmax = twap_margin(TWAP_MARGIN_MAX, 19.0)
    disp = (m995 + mmax) / 2.0                     # between the tiers
    assert m995 < disp < mmax
    fired = _lock(eng, 64000.0 - disp, 64000.0, 19.0, 0.99, 0.80,
                  require_max_tier=False)
    assert fired.action == "LATE_SNIPE_NO"         # p99.5 would have taken it
    gated = _lock(eng, 64000.0 - disp, 64000.0, 19.0, 0.99, 0.80,
                  require_max_tier=True)
    assert gated.action == "SKIP"
    assert "not locked" in gated.reason


def test_max_tier_gate_still_fires_beyond_the_max_margin():
    """The gate must not silence the tier that has never breached."""
    eng = _eng()
    mmax = twap_margin(TWAP_MARGIN_MAX, 19.0)
    s = _lock(eng, 64000.0 + mmax + 1.0, 64000.0, 19.0, 0.90, 0.11,
              require_max_tier=True)
    assert s.action == "LATE_SNIPE_YES"
    assert s.prob == pytest.approx(TWAP_PROB_DETERMINISTIC)


def test_production_default_is_max_tier_only():
    """A caller that forgets the flag must get the SAFE behaviour."""
    eng = _eng()
    disp = (twap_margin(TWAP_MARGIN_P995, 19.0)
            + twap_margin(TWAP_MARGIN_MAX, 19.0)) / 2.0
    s = eng.evaluate_twap_lock(64000.0 - disp, 64000.0, 19.0, 0.99, 0.80,
                               30.0, 0.8, 0.04)
    assert s.action == "SKIP"


# ───────────────────────── twap_margin interpolation ─────────────────────────
def test_margin_knots_exact_and_linear_between():
    assert twap_margin(TWAP_MARGIN_P995, 4.0) == pytest.approx(1.0)
    assert twap_margin(TWAP_MARGIN_MAX, 4.0) == pytest.approx(2.0)
    # midpoint of (4, 1.0)-(6, 1.5)
    assert twap_margin(TWAP_MARGIN_P995, 5.0) == pytest.approx(1.25)


def test_margin_clamps_to_end_knots():
    assert twap_margin(TWAP_MARGIN_P995, 0.5) == pytest.approx(1.0)    # below k=2
    assert twap_margin(TWAP_MARGIN_P995, 60.0) == pytest.approx(38.0)  # above k=58


def test_margins_monotone_nondecreasing():
    # More time remaining = more can happen; a dip in the table would let a
    # LATER evaluation lock on a smaller displacement than an earlier one.
    for table in (TWAP_MARGIN_P995, TWAP_MARGIN_MAX):
        vals = [v for _, v in table]
        assert vals == sorted(vals)


# ───────────────────────── evaluate_twap_lock ─────────────────────────────────
def test_fires_up_on_max_tier_lock_with_cheap_ask():
    # k=4: max margin $4.0 — disp +$6 is beyond the worst error ever observed.
    sig = _lock(_eng(), proj=60006.0, strike=60000.0, k=4.0,
                ask_up=0.90, ask_down=0.11)
    assert sig.action == "LATE_SNIPE_YES"
    assert sig.side == "Up"
    assert sig.prob == pytest.approx(TWAP_PROB_DETERMINISTIC)
    assert sig.edge == pytest.approx(TWAP_PROB_DETERMINISTIC - 0.90)


def test_fires_down_on_negative_displacement():
    sig = _lock(_eng(), proj=59994.0, strike=60000.0, k=4.0,
                ask_up=0.11, ask_down=0.90)
    assert sig.action == "LATE_SNIPE_NO"
    assert sig.side == "Down"


def test_zero_displacement_takes_up_side():
    # Tie rule: final >= strike resolves Up, so disp == 0 projects Up (and is
    # never locked — a zero displacement can't clear any margin).
    sig = _lock(_eng(), proj=60000.0, strike=60000.0, k=4.0,
                ask_up=0.50, ask_down=0.51)
    assert sig.action == "SKIP"
    assert sig.side == "Up"


def test_skips_when_not_locked():
    # k=4: p99.5 margin $1.0 — disp $0.5 is inside the error band.
    sig = _lock(_eng(), proj=60000.5, strike=60000.0, k=4.0,
                ask_up=0.60, ask_down=0.41)
    assert sig.action == "SKIP"
    assert "not locked" in sig.reason


def test_p995_tier_prob_between_margins():
    # disp $1.5 at k=4: beyond p99.5 ($1.0) but inside max-ever ($2.0).
    sig = _lock(_eng(), proj=60001.5, strike=60000.0, k=4.0,
                ask_up=0.90, ask_down=0.11)
    assert sig.action == "LATE_SNIPE_YES"
    assert sig.prob == pytest.approx(TWAP_PROB_P995)


def test_ask_cap_derives_from_edge_floor():
    # max tier (prob 0.999): ask 0.96 -> edge 0.039 < 0.04 floor -> SKIP;
    # ask 0.95 -> edge 0.049 -> fire. One knob, no separate cap to drift.
    eng = _eng()
    rich = _lock(eng, 60006.0, 60000.0, 4.0, ask_up=0.96, ask_down=0.05)
    assert rich.action == "SKIP"
    assert "prices it" in rich.reason
    ok = _lock(eng, 60006.0, 60000.0, 4.0, ask_up=0.95, ask_down=0.06)
    assert ok.action == "LATE_SNIPE_YES"


def test_p995_tier_caps_tighter_than_max_tier():
    # Same ask 0.958: max tier fires (0.999-0.958 >= 0.04), p99.5 tier must not
    # (0.995-0.958 < 0.04) — the riskier tier demands the deeper discount.
    eng = _eng()
    assert _lock(eng, 60006.0, 60000.0, 4.0, 0.958, 0.05).action == "LATE_SNIPE_YES"
    assert _lock(eng, 60001.5, 60000.0, 4.0, 0.958, 0.05).action == "SKIP"


def test_skips_outside_zone_and_below_k_min():
    eng = _eng()
    assert _lock(eng, 60050.0, 60000.0, 31.0, 0.80, 0.21).action == "SKIP"
    assert _lock(eng, 60050.0, 60000.0, 0.5, 0.80, 0.21).action == "SKIP"


def test_skips_on_none_projection_or_bad_strike():
    eng = _eng()
    assert _lock(eng, None, 60000.0, 4.0, 0.80, 0.21).action == "SKIP"
    assert _lock(eng, 60006.0, 0.0, 4.0, 0.80, 0.21).action == "SKIP"


def test_skips_on_unexecutable_ask():
    eng = _eng()
    for bad in (None, 0.0, 1.0):
        sig = _lock(eng, 60006.0, 60000.0, 4.0, bad, 0.05)
        assert sig.action == "SKIP"


def test_kelly_sized_on_market_anchored_prob_not_tier_prob():
    """Sizing must use ask + sniper_min_edge (the defended edge at market odds),
    never the tier prob — the tier floors are empirical tail bounds, and Kelly
    on a tail bound upsizes exactly the fires a regime shift breaks first."""
    eng = _eng()
    sig = _lock(eng, 60006.0, 60000.0, 4.0, ask_up=0.90, ask_down=0.11)
    assert sig.action == "LATE_SNIPE_YES"
    anchored = eng._kelly(0.90 + 0.04, 0.90)
    tier = eng._kelly(sig.prob, 0.90)
    assert sig.kelly_size == pytest.approx(anchored)
    assert anchored < tier          # the anchor is the conservative branch


def test_trading_enabled_wired_from_settings():
    # trading_enabled is THE master brake, read straight from settings.yaml —
    # the single config source. This also smoke-tests that the loader
    # validates and surfaces the live config end-to-end.
    from polybot.config.loader import load_config
    cfg = load_config()
    assert isinstance(cfg["late_window"]["trading_enabled"], bool)
    assert cfg["late_window"]["twap_zone_s"] <= 60.0


def test_sniper_enabled_alias_still_halts(tmp_path, monkeypatch):
    """An old settings file using the retired key must still run (aliased with
    a deprecation warning), never KeyError at boot — the brake must work under
    either name."""
    import polybot.config.loader as loader_mod
    src = (Path(loader_mod.__file__).resolve().parents[0] / "settings.yaml").read_text(
        encoding="utf-8")
    old = src.replace("trading_enabled: true", "sniper_enabled: false")
    p = tmp_path / "settings.yaml"
    p.write_text(old, encoding="utf-8")
    monkeypatch.setattr(loader_mod, "_config", None, raising=False)
    cfg = loader_mod.load_config(config_path=str(p))
    assert cfg["late_window"]["trading_enabled"] is False
