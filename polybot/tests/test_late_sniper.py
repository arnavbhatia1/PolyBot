"""Late-window sniper: the live cb_move accessor and the evaluate_late_sniper signal.

These cover the bot-formable late-window edge in isolation (the main.py wiring is
gated OFF by default and exercised by the integration review). The signal mirrors
the offline `momentum` signal in scripts/analyze_late_window.py exactly: a Coinbase
move past strike with a still-cheap chosen-side ask.
"""
import ast
import time
from pathlib import Path

import pytest

from polybot.core.signal_engine import SignalEngine
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

# Healthy ATR so compute_probability runs against a real vol scale.
IND = {"atr": {"atr": 40.0, "passes": True, "candle_ts": 1}}


def _eng():
    return SignalEngine()


# ───────────────────────── evaluate_late_sniper ──────────────────────────────
def test_fires_on_up_move_past_strike_with_cheap_ask():
    sig = _eng().evaluate_late_sniper(
        IND, btc_price=60050.0, strike_price=60000.0, seconds_remaining=20.0,
        market_ask_up=0.70, market_ask_down=0.31, cb_move=12.0,
        cb_move_threshold=8.0, ask_cap=0.92, sniper_min_edge=0.02)
    assert sig.action == "LATE_SNIPE_YES"
    assert sig.side == "Up"
    assert sig.edge > 0.02
    assert sig.kelly_size >= 0.0


def test_fires_on_down_move_past_strike():
    sig = _eng().evaluate_late_sniper(
        IND, btc_price=59950.0, strike_price=60000.0, seconds_remaining=20.0,
        market_ask_up=0.31, market_ask_down=0.70, cb_move=-12.0,
        cb_move_threshold=8.0, ask_cap=0.92, sniper_min_edge=0.02)
    assert sig.action == "LATE_SNIPE_NO"
    assert sig.side == "Down"


def test_skips_move_below_threshold():
    sig = _eng().evaluate_late_sniper(
        IND, 60050.0, 60000.0, 20.0, 0.70, 0.31, cb_move=3.0,
        cb_move_threshold=8.0, ask_cap=0.92, sniper_min_edge=0.02)
    assert sig.action == "SKIP"


def test_skips_move_not_past_strike():
    # Big up move but BTC still below strike — the move hasn't crossed it.
    sig = _eng().evaluate_late_sniper(
        IND, 59950.0, 60000.0, 20.0, 0.45, 0.56, cb_move=12.0,
        cb_move_threshold=8.0, ask_cap=0.92, sniper_min_edge=0.02)
    assert sig.action == "SKIP"


def test_skips_ask_above_cap_book_already_repriced():
    sig = _eng().evaluate_late_sniper(
        IND, 60050.0, 60000.0, 20.0, market_ask_up=0.95, market_ask_down=0.06,
        cb_move=12.0, cb_move_threshold=8.0, ask_cap=0.92, sniper_min_edge=0.02)
    assert sig.action == "SKIP"


def test_skips_when_no_stale_cheap_edge_left():
    # Barely past strike (prob ~0.5) but ask already rich -> edge below the floor.
    sig = _eng().evaluate_late_sniper(
        IND, 60001.0, 60000.0, 20.0, market_ask_up=0.90, market_ask_down=0.11,
        cb_move=12.0, cb_move_threshold=8.0, ask_cap=0.92, sniper_min_edge=0.02)
    assert sig.action == "SKIP"


def test_skips_when_atr_not_ready():
    """The sniper bypasses the ATR gate, so a cold ATR buffer (atr<=0) must SKIP
    here — compute_probability's 0.5 fallback would otherwise let it fire on a
    garbage edge (0.5 - ask) at boot."""
    for cold in ({"atr": {"atr": 0, "passes": False, "candle_ts": 1}}, {}):
        sig = _eng().evaluate_late_sniper(
            cold, btc_price=60050.0, strike_price=60000.0, seconds_remaining=20.0,
            market_ask_up=0.30, market_ask_down=0.69, cb_move=12.0,
            cb_move_threshold=8.0, ask_cap=0.92, sniper_min_edge=0.02)
        assert sig.action == "SKIP"
        assert "ATR" in sig.reason


def test_skips_on_none_move():
    sig = _eng().evaluate_late_sniper(
        IND, 60050.0, 60000.0, 20.0, 0.70, 0.31, cb_move=None,
        cb_move_threshold=8.0, ask_cap=0.92, sniper_min_edge=0.02)
    assert sig.action == "SKIP"


def test_does_not_apply_min_model_probability():
    # min_model_probability is 0.56; a modest move giving prob in (0.5, 0.56) with a
    # cheap ask must STILL fire — the sniper is move-driven, not prob-gated.
    eng = _eng()
    # Barely past strike -> prob just over 0.5 (below the 0.56 entry gate); a cheap
    # ask keeps the edge above the stale-cheap floor so it still fires.
    sig = eng.evaluate_late_sniper(
        IND, btc_price=60001.5, strike_price=60000.0, seconds_remaining=20.0,
        market_ask_up=0.45, market_ask_down=0.56, cb_move=10.0,
        cb_move_threshold=8.0, ask_cap=0.92, sniper_min_edge=0.02)
    assert sig.action == "LATE_SNIPE_YES"
    assert sig.prob < eng.min_model_probability  # would have been blocked by the normal gate
    assert sig.edge > 0.02


def test_sniper_enabled_wired_from_settings():
    # The sniper is the bot's only strategy; sniper_enabled is the kill-bar SAFETY
    # (emergency brake), read straight from settings.yaml — the single config source
    # (there is no param_registry default any more). This also smoke-tests that the
    # loader validates and surfaces the live config end-to-end.
    from polybot.config.loader import load_config
    cfg = load_config()
    assert isinstance(cfg["late_window"]["sniper_enabled"], bool)


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
