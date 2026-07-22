"""SPRT machinery + regime-Kelly shadow stamps + the single settled OPEN banner
(the 07-24 commit: alert-only analytics + log-only banner redesign)."""
import importlib.util
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from polybot.core.sprt import run_sprt, format_status
from polybot.main import (
    _regime_shadow_fields, _log_open_banner, _on_entry_settled,
    _pending_settled_banners, _lru_set,
)

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_harness():
    hp = ROOT / "scripts" / "analyze_late_window.py"
    spec = importlib.util.spec_from_file_location("analyze_late_window_t", hp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── SPRT core ─────────────────────────────────────────────────────────────────

def test_sprt_retro_sanity_validation_passes_day_3():
    """Pre-registered retro check (SPRT_DESIGN): the 07-13..18 validation
    day-means accept H1 on day 3 at the frozen constants."""
    r = run_sprt([2.9, 8.6, 21.1, 12.6, 1.9, 9.3], mu1=6.0, sigma=7.0)
    assert r.state == "accept_h1"
    assert r.n_days == 3


def test_sprt_rejects_dead_edge():
    r = run_sprt([-6.0, -6.0, -6.0], mu1=6.0, sigma=7.0)
    assert r.state == "accept_h0"
    assert r.n_days == 3


def test_sprt_no_decision_before_min_days():
    # Λ is already past the accept boundary at day 2, but one-day flukes must
    # not decide — min 3 days.
    r = run_sprt([30.0, 30.0], mu1=6.0, sigma=7.0)
    assert r.state == "continue"


def test_sprt_truncates_at_16_days():
    # x = μ₁/2 makes each day's increment exactly 0 — no boundary is ever hit.
    r = run_sprt([3.0] * 20, mu1=6.0, sigma=7.0)
    assert r.state == "truncated"
    assert r.n_days == 16


def test_sprt_void_on_sigma_blowup_or_unset():
    assert run_sprt([30.0, -30.0, 25.0, -25.0], mu1=6.0, sigma=7.0).state == "void"
    assert run_sprt([1.0, 2.0], mu1=6.0, sigma=0.0).state == "void"
    assert "SPRT[x]" in format_status("x", run_sprt([], mu1=6.0, sigma=7.0))


# ── Regime shadow stamps ──────────────────────────────────────────────────────

_AUX_HOT = {"n_ticks_1s": 4, "n_ticks_30s": 30, "fast_realized_vol_60s": 5e-5}


def test_regime_buckets_and_burst_mult():
    f = _regime_shadow_fields(10.0, 10.0, 10.0, _AUX_HOT, size=3.0,
                              bankroll=135.0, max_bankroll_pct=0.80)
    b = f["regime_buckets"]
    assert b["burst"] == "HOT" and f["regime_kelly_mult"] == 1.15
    assert b["atr_regime"] == "MID" and b["atr_short"] == "MID" and b["frv"] == "MID"
    assert b["session"] in ("ON", "DAY", "EVE")
    assert f["size_flat"] == 3.0 and f["size_regime"] == pytest.approx(3.45)


def test_regime_cold_feed_stamps_none_not_zero():
    f = _regime_shadow_fields(0.0, 0.0, 0.0,
                              {"n_ticks_1s": None, "n_ticks_30s": None,
                               "fast_realized_vol_60s": None},
                              size=3.0, bankroll=135.0, max_bankroll_pct=0.80)
    b = f["regime_buckets"]
    assert b["burst"] is None and b["atr_regime"] is None and b["frv"] is None
    assert f["regime_kelly_mult"] == 1.0


def test_regime_size_floor_and_cap():
    cold = {"n_ticks_1s": 1, "n_ticks_30s": 30, "fast_realized_vol_60s": None}
    f = _regime_shadow_fields(10.0, 10.0, 10.0, cold, size=1.0,
                              bankroll=135.0, max_bankroll_pct=0.80)
    assert f["regime_kelly_mult"] == 0.80
    assert f["size_regime"] == 0.0            # 0.80 < $1 → the regime arm skips
    f2 = _regime_shadow_fields(10.0, 10.0, 10.0, _AUX_HOT, size=200.0,
                               bankroll=135.0, max_bankroll_pct=0.80)
    assert f2["size_regime"] == pytest.approx(108.0)   # bankroll cap binds


# ── Nightly reads (burst SPRT + regime D) over a realistic ledger ─────────────

def _mk_ledger(tmp_path, days, regime_stamps=False):
    """days: list of (utc_day, [(pnl, hot?)...]). Builds the minimal schema the
    reads join on."""
    db = tmp_path / "ledger.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, shares_held REAL, "
                "indicator_snapshot TEXT)")
    con.execute("CREATE TABLE trade_history (id INTEGER PRIMARY KEY, position_id INTEGER, "
                "pnl REAL, size REAL, exit_timestamp TEXT)")
    pid = 0
    for day, fills in days:
        for pnl, hot in fills:
            pid += 1
            ctx = {"n_ticks_1s": 4 if hot else 1, "n_ticks_30s": 30}
            if regime_stamps:
                ctx.update({"size_flat": 2.0, "size_regime": 3.0})
            con.execute("INSERT INTO positions VALUES (?,?,?)",
                        (pid, 1.0, json.dumps({"trade_context": ctx})))
            con.execute("INSERT INTO trade_history VALUES (?,?,?,?,?)",
                        (pid, pid, pnl, 2.0, f"{day}T12:00:00+00:00"))
    con.commit()
    con.close()
    return db


def test_burst_sprt_freezes_sigma_then_scores(tmp_path):
    mod = _load_harness()
    # 6 qualifying days (≥2 fills per arm) freeze σ; the 7th+ score. Day-varying
    # nets so the frozen σ (stdev of the 6 estimation-day diffs) is nonzero.
    days = [(f"2026-07-{d:02d}", [(0.10 + 0.01 * d, True), (0.12, True),
                                  (0.02, False), (0.01, False)])
            for d in range(19, 26)]
    db = _mk_ledger(tmp_path, days)
    state = tmp_path / "sprt_burst.json"
    r = mod.burst_sprt_read(db, None, state_path=state)
    assert state.exists()
    frozen = json.loads(state.read_text())
    assert len(frozen["sigma_days"]) == 6 and frozen["frozen_sigma"] > 0
    assert r["n_qualifying"] == 7 and r["n_scored"] == 1
    assert r["state"] in ("continue", "accept_h1", "accept_h0")
    # Re-running never re-freezes (write-once): sigma_days unchanged.
    r2 = mod.burst_sprt_read(db, None, state_path=state)
    assert json.loads(state.read_text())["sigma_days"] == frozen["sigma_days"]
    assert r2["n_scored"] == 1


def test_burst_sprt_accrues_below_six_days(tmp_path):
    mod = _load_harness()
    days = [(f"2026-07-{d:02d}", [(0.10, True), (0.12, True), (0.02, False), (0.01, False)])
            for d in range(19, 22)]
    db = _mk_ledger(tmp_path, days)
    state = tmp_path / "sprt_burst.json"
    r = mod.burst_sprt_read(db, None, state_path=state)
    assert r["state"] == "accruing_sigma" and r["n_qualifying"] == 3
    assert not state.exists()


def test_regime_shadow_counterfactual_d(tmp_path):
    mod = _load_harness()
    # One day, 3 stamped fills: D = Σ(pnl/2.0·3.0 − pnl) = Σ pnl·0.5
    days = [("2026-07-24", [(0.40, True), (-0.20, False), (0.60, True)])]
    db = _mk_ledger(tmp_path, days, regime_stamps=True)
    r = mod.regime_shadow_read(db, None)
    assert r["n_days"] == 1
    assert r["total_d"] == pytest.approx(0.5 * (0.40 - 0.20 + 0.60))


# ── Single settled OPEN banner ────────────────────────────────────────────────

_CTX = dict(side="Up", size=1.61, cid="btc-updown-5m-1776691500", phase="late_sniper",
            signal_ask=0.80, posted=0.81, btc_price=118_000.0, strike=117_950.0,
            prob=0.94, edge=0.14, flow=0.10, cvd=0.20, fee_rate=0.07, bankroll=135.0)


def test_paper_banner_prints_fee_buffer_label(caplog):
    with caplog.at_level(logging.INFO, logger="polybot"):
        _log_open_banner(dict(_CTX), 0.77, settled="paper")
    assert "OPEN Up" in caplog.text
    assert "fee buffer" in caplog.text and "not charged" in caplog.text
    assert "provisional" not in caplog.text


def test_settled_banner_prints_once_from_audit_callback(caplog):
    _pending_settled_banners.clear()
    _lru_set(_pending_settled_banners, 42, dict(_CTX), 32)
    with caplog.at_level(logging.INFO, logger="polybot"):
        _on_entry_settled(42, 0.77, "chain")
    assert "OPEN Up" in caplog.text and "@0.77" in caplog.text
    assert not _pending_settled_banners
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="polybot"):
        _on_entry_settled(42, 0.77, "chain")   # duplicate settle → no second banner
    assert "OPEN Up" not in caplog.text


def test_settled_banner_flags_provisional_when_chain_lookup_fails(caplog):
    _pending_settled_banners.clear()
    _lru_set(_pending_settled_banners, 7, dict(_CTX), 32)
    with caplog.at_level(logging.INFO, logger="polybot"):
        _on_entry_settled(7, 0.81, "provisional")
    assert "provisional" in caplog.text
