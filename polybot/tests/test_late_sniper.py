"""Late-window sniper: the TWAP lock signal (evaluate_twap_lock) and the live
cb_move accessor (telemetry/scar dims still read it).

These cover the bot-formable late-window edge in isolation (the main.py wiring
is gated OFF by default and exercised by the integration review). The signal
mirrors scripts/analyze_twap_lock.py exactly: displacement of the projected
final TWAP past the frozen error margin, bought only while the winner's ask
still clears the edge floor.
"""
import ast
import time
from pathlib import Path

import pytest

from polybot.core.signal_engine import (
    SignalEngine, TWAP_MARGIN_MAX, TWAP_MARGIN_P995,
    TWAP_PROB_DETERMINISTIC, TWAP_PROB_P995, twap_margin,
)
from polybot.feeds.coinbase_feed import CoinbaseFeed


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
    for var in ("aux_signals", "adverse_kelly_mult", "adverse_rate_at_30s",
                "spot_flow_rec", "flow_score_rec", "phase"):
        assigns = [t.lineno
                   for node in ast.walk(fn) if isinstance(node, ast.Assign)
                   for t in ast.walk(node)
                   if isinstance(t, ast.Name) and t.id == var and isinstance(t.ctx, ast.Store)]
        assigns = [ln for ln in assigns if ln < first_ghost]
        assert assigns, f"'{var}' is read by _ghost's base_ctx but never assigned before the first _ghost() call (line {first_ghost}) — free-var-before-use"


def _eng():
    return SignalEngine()


def _lock(eng, proj, strike, k, ask_up, ask_down, min_edge=0.04,
          zone=30.0, k_min=0.8):
    return eng.evaluate_twap_lock(proj, strike, k, ask_up, ask_down,
                                  zone, k_min, min_edge)


# ───────────────────────── twap_margin interpolation ─────────────────────────
def test_margin_knots_exact_and_linear_between():
    assert twap_margin(TWAP_MARGIN_P995, 4.0) == pytest.approx(1.6)
    assert twap_margin(TWAP_MARGIN_MAX, 4.0) == pytest.approx(4.0)
    # midpoint of (4, 1.6)-(6, 4.5)
    assert twap_margin(TWAP_MARGIN_P995, 5.0) == pytest.approx(3.05)


def test_margin_clamps_to_end_knots():
    assert twap_margin(TWAP_MARGIN_P995, 0.5) == pytest.approx(0.6)   # below k=2
    assert twap_margin(TWAP_MARGIN_P995, 60.0) == pytest.approx(32.0)  # above k=29


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
    # k=4: p99.5 margin $1.6 — disp $1 is inside the error band.
    sig = _lock(_eng(), proj=60001.0, strike=60000.0, k=4.0,
                ask_up=0.60, ask_down=0.41)
    assert sig.action == "SKIP"
    assert "not locked" in sig.reason


def test_p995_tier_prob_between_margins():
    # disp $2 at k=4: beyond p99.5 ($1.6) but inside max-ever ($4.0).
    sig = _lock(_eng(), proj=60002.0, strike=60000.0, k=4.0,
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
    assert _lock(eng, 60002.0, 60000.0, 4.0, 0.958, 0.05).action == "SKIP"


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


def test_sniper_enabled_wired_from_settings():
    # The sniper is the bot's only strategy; sniper_enabled is the kill-bar SAFETY
    # (emergency brake), read straight from settings.yaml — the single config source
    # (there is no param_registry default any more). This also smoke-tests that the
    # loader validates and surfaces the live config end-to-end.
    from polybot.config.loader import load_config
    cfg = load_config()
    assert isinstance(cfg["late_window"]["sniper_enabled"], bool)
    assert cfg["late_window"]["twap_zone_s"] <= 30.0


# ───────────────────────────── cb_move accessor ──────────────────────────────
def test_cb_move_change_over_window():
    f = CoinbaseFeed()
    now = time.time()
    f._window_start = now - 10.0            # buffer continuously spans > 2s
    f._prices.clear()
    f._prices.append((now - 3.0, 60000.0))
    f._prices.append((now - 2.0, 60010.0))  # latest sample at/before cutoff (now-2)
    f._prices.append((now - 1.0, 60030.0))
    f.state.price = 60050.0
    # interpolated at exactly now-2.0 (= the 60010 bucket, within a sub-ms timing epsilon)
    assert f.cb_move(window_s=2.0) == pytest.approx(40.0, abs=0.01)


def test_cb_move_none_when_buffer_truncated():
    f = CoinbaseFeed()
    now = time.time()
    f._window_start = now - 0.5             # reconnect: buffer doesn't span 2s
    f._prices.append((now - 0.4, 60000.0))
    f.state.price = 60010.0
    assert f.cb_move(window_s=2.0) is None


def test_cb_move_none_when_no_price():
    f = CoinbaseFeed()
    f._window_start = time.time() - 10.0
    f.state.price = 0.0
    assert f.cb_move(2.0) is None


def test_cb_move_sign_matches_direction():
    f = CoinbaseFeed()
    now = time.time()
    f._window_start = now - 10.0
    f._prices.append((now - 2.5, 60100.0))
    f._prices.append((now - 1.0, 60050.0))
    f.state.price = 60000.0
    assert f.cb_move(2.0) < 0   # falling price -> negative move


def test_cb_move_interpolates_between_buckets_no_overstatement():
    # Regression for the 1s-bucket overstatement bug: the cutoff (now-2.0) falls BETWEEN
    # buckets at now-2.5 and now-1.5. The old code took the now-2.5 bucket (a ~2.5s
    # lookback) and overstated; interpolation must return the price at exactly now-2.0.
    f = CoinbaseFeed()
    now = time.time()
    f._window_start = now - 10.0
    f._prices.append((now - 2.5, 60000.0))
    f._prices.append((now - 1.5, 60020.0))   # +20 over 1s -> +10 at the midpoint (now-2.0)
    f.state.price = 60050.0
    mv = f.cb_move(window_s=2.0)
    # interpolated then ~= 60010 -> move ~= 40 (NOT the overstated 50 from using 60000)
    assert mv == pytest.approx(40.0, abs=0.6)
    assert mv < 50.0   # the bug would have returned ~50
