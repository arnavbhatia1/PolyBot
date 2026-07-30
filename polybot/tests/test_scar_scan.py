"""Scar scan — the nightly learning loop (core/scar_scan.py + the main.py
fire-path stamps/enforce hook + the analyze_late_window read wrapper)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from polybot.core.scar_scan import (
    FIRE_TIME_DIMS, derive_dims, fire_time_matches, load_registry,
    record_veto, resolve_vetoes, save_registry, scan,
)
from polybot.main import _record_killed_ask, _scar_fields, _window_killed_asks

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_harness():
    hp = ROOT / "scripts" / "analyze_late_window.py"
    spec = importlib.util.spec_from_file_location("analyze_late_window_scar", hp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(**over):
    base = {
        "market_price_up": 0.88, "market_price_down": 0.14,
        "seconds_remaining": 20.0, "edge": 0.08, "model_probability": 0.95,
        "cb_tick_to_submit_ms": 450.0, "scar_refire_class": "first_fire",
        "scar_cb_move": 10.0,
        "regime_buckets": {"atr_regime": "HI", "session": "DAY", "burst": "HOT"},
    }
    base.update(over)
    return base


def _mk_ledger(tmp_path, fills):
    """fills: list of (utc_day, pnl, shares, side, entry, ctx)."""
    db = tmp_path / "scar_ledger.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, shares_held REAL, "
                "side TEXT, entry_price REAL, indicator_snapshot TEXT)")
    con.execute("CREATE TABLE trade_history (id INTEGER PRIMARY KEY, position_id INTEGER, "
                "pnl REAL, size REAL, exit_timestamp TEXT)")
    for i, (day, pnl, shares, side, entry, ctx) in enumerate(fills, 1):
        con.execute("INSERT INTO positions VALUES (?,?,?,?,?)",
                    (i, shares, side, entry, json.dumps({"trade_context": ctx})))
        con.execute("INSERT INTO trade_history VALUES (?,?,?,?,?)",
                    (i, i, pnl, shares * entry, f"{day}T12:00:00+00:00"))
    con.commit()
    con.close()
    return db


# ── Dimension derivation ──────────────────────────────────────────────────────

def test_derive_dims_fire_time_and_observational():
    d = derive_dims(_ctx(), "Up", "Mon")
    assert d["ask_bucket"] == "0.85+" and d["side"] == "Up" and d["dow"] == "Mon"
    assert d["tremain"] == "15-30s" and d["refire"] == "first_fire"
    assert d["atr_regime"] == "HI" and d["cb_move_bucket"] == "8-12"
    assert d["slip"] is None                      # no entry price at fire time
    # scan time: booked entry vs decision ask classifies slip
    assert derive_dims(_ctx(), "Up", "Mon", entry_price=0.885)["slip"] == "clean"
    assert derive_dims(_ctx(), "Up", "Mon", entry_price=0.90)["slip"] == "padded"
    assert derive_dims(_ctx(), "Up", "Mon", entry_price=0.86)["slip"] == "improved"


def test_derive_dims_reversion_mechanism_dims():
    ctx = _ctx(btc_price=64520.0, strike_price=64500.0, regime_autocorr=-0.12,
               coinbase_cvd_60s=-2.5, cross_venue_gap=-7.0,
               clob_depth_top5_up_usd=250.0, market_price_down=0.13,
               scar_killed_n=2, is_flip=False)
    d = derive_dims(ctx, "Up", "Mon")
    assert d["strike_dist"] == "12-25"
    assert d["autocorr"] == "reverting"
    assert d["cvd_side"] == "contradict"          # negative CVD against an Up fire
    assert d["xgap"] == "5-15"
    assert d["depth_side"] == "$100-500"
    assert d["vig"] == "1.00-1.01"                # 0.88 + 0.13
    assert d["killed_n"] == "2+"
    assert d["flip"] == "first"
    # a Down fire flips the CVD sign convention
    assert derive_dims(ctx, "Down", "Mon")["cvd_side"] == "confirm"


def test_derive_dims_staleask_and_oracle_confirm_dims():
    ctx = _ctx(btc_price=64520.0, strike_price=64500.0, clob_book_age_s=2.4,
               regime_direction=-1.0, adverse_rate_at_30s=0.62,
               scar_cb_move=10.0, scar_cb_move_10s=22.0,
               chainlink_price_at_fire=64512.0)
    d = derive_dims(ctx, "Up", "Mon")
    assert d["book_age"] == "1-5s"
    assert d["dir_agree"] == "against_drift"      # Up fire into a down drift
    assert d["adverse_regime"] == "0.60+"
    assert d["move_shape"] == "extending"         # 10s move 2.2x the 2s burst
    assert d["cl_confirm"] == "cl_crossed"        # oracle already past strike
    # isolated spike: the 2s burst IS the 10s move
    spike = derive_dims(_ctx(scar_cb_move=10.0, scar_cb_move_10s=11.0), "Up", "Mon")
    assert spike["move_shape"] == "spike"
    # a same-magnitude OPPOSITE-sign 10s move is a whipsaw, not extension
    whip = derive_dims(_ctx(scar_cb_move=10.0, scar_cb_move_10s=-20.0), "Up", "Mon")
    assert whip["move_shape"] == "spike"
    # oracle still below strike on an Up fire = unconfirmed cross
    d2 = derive_dims(_ctx(strike_price=64500.0, chainlink_price_at_fire=64490.0),
                     "Up", "Mon")
    assert d2["cl_confirm"] == "cl_not_crossed"
    # Down-side conventions
    d3 = derive_dims(ctx, "Down", "Mon")
    assert d3["dir_agree"] == "with_drift"
    assert d3["cl_confirm"] == "cl_not_crossed"   # 64512 > 64500 on a Down fire


def test_derive_dims_cold_feeds_are_none_never_binned():
    d = derive_dims({"market_price_up": None, "regime_buckets": {}}, "Up", "Tue")
    for k in ("ask_bucket", "tremain", "refire", "atr_regime", "burst",
              "edge_bucket", "cb_move_bucket", "latency", "strike_dist",
              "autocorr", "cvd_side", "xgap", "frv", "atr_short",
              "depth_side", "vig", "killed_n", "flip",
              "book_age", "dir_agree", "adverse_regime", "move_shape",
              "cl_confirm"):
        assert d[k] is None


# ── Discovery: the pre-registered flag rule ───────────────────────────────────

def _toxic_ledger(tmp_path):
    """LO-regime fills lose consistently across 4 days; the rest win. Both
    populations big enough for the rule."""
    fills = []
    for d in range(20, 24):
        day = f"2026-07-{d:02d}"
        lo = _ctx(regime_buckets={"atr_regime": "LO", "session": "DAY", "burst": "HOT"})
        for pnl in (-0.60, -0.50, -0.10 - 0.02 * d):   # day-varying means (t needs σ>0)
            fills.append((day, pnl, 1.0, "Up", 0.88, lo))
        for pnl in (0.11, 0.12, 0.13, 0.12):
            fills.append((day, pnl, 1.0, "Up", 0.88, _ctx()))
    return _mk_ledger(tmp_path, fills)


def test_scan_flags_and_registers_toxic_cell(tmp_path):
    db = _toxic_ledger(tmp_path)
    reg_path = tmp_path / "scar_gates.json"
    rep = scan(db, None, reg_path)
    assert "atr_regime=LO" in rep["registered"]
    reg = load_registry(reg_path)
    g = next(g for g in reg["gates"] if g["name"] == "atr_regime=LO")
    assert g["status"] == "shadow" and g["in_sample"]["n"] == 12
    assert g["in_sample"]["ew_cs"] <= -5.0
    # idempotent: night 2 does not re-register the same cell
    rep2 = scan(db, None, reg_path)
    assert rep2["registered"] == []
    assert sum(1 for g in load_registry(reg_path)["gates"]
               if g["name"] == "atr_regime=LO") == 1


def test_scan_ignores_inconsistent_noise_cell(tmp_path):
    # Same cell, but sign flips day-over-day: EW is negative, day-t is weak.
    fills = []
    for d, sign in zip(range(20, 24), (-1, 1, -1, 1)):
        day = f"2026-07-{d:02d}"
        lo = _ctx(regime_buckets={"atr_regime": "LO", "session": "DAY", "burst": "HOT"})
        for _ in range(3):
            fills.append((day, 0.40 * sign - 0.15, 1.0, "Up", 0.88, lo))
        for pnl in (0.11, 0.12, 0.13):
            fills.append((day, pnl, 1.0, "Up", 0.88, _ctx()))
    rep = scan(_mk_ledger(tmp_path, fills), None, tmp_path / "g.json")
    assert rep["registered"] == []


def test_scan_never_registers_observational_dims(tmp_path):
    # Catastrophic "padded slip" cell — post-hoc information, watch-only.
    fills = []
    for d in range(20, 24):
        day = f"2026-07-{d:02d}"
        for _ in range(3):
            fills.append((day, -0.60, 1.0, "Up", 0.90, _ctx()))   # slip=+0.02 padded
        for pnl in (0.11, 0.12, 0.13):
            fills.append((day, pnl, 1.0, "Up", 0.881, _ctx()))
    rep = scan(_mk_ledger(tmp_path, fills), None, tmp_path / "g.json")
    assert all(not r.startswith("slip=") for r in rep["registered"])
    assert "slip" not in FIRE_TIME_DIMS and "latency" not in FIRE_TIME_DIMS


# ── Per-gate OOS SPRT: freeze, score, graduate, retire ────────────────────────

def _seed(reg_path, discovered="2026-07-19"):
    save_registry(reg_path, {"version": 1, "gates": [{
        "name": "atr_regime=LO", "dim": "atr_regime", "bucket": "LO",
        "discovered": discovered, "status": "shadow", "source": "test",
        "in_sample": {"n": 18, "ew_cs": -15.0},
        "sprt": {"mu1": 6.0, "frozen_sigma": None, "sigma_days": []}}]})


def test_gate_sigma_freezes_then_graduates_on_toxic_oos(tmp_path):
    reg_path = tmp_path / "g.json"
    _seed(reg_path)
    lo = _ctx(regime_buckets={"atr_regime": "LO", "session": "DAY", "burst": "HOT"})
    # 9 OOS days strictly after discovery, day-gains alternating +10/+12¢
    # (vetoing wins, variance stable so the frozen σ matches the scoring
    # days): first 4 freeze σ, the rest score and cross the accept boundary.
    fills = [(f"2026-07-{d:02d}", -(0.10 + 0.02 * (d % 2)), 1.0, "Up", 0.88, lo)
             for d in range(20, 29)]
    db = _mk_ledger(tmp_path, fills)
    rep = scan(db, None, reg_path)
    g = next(g for g in load_registry(reg_path)["gates"] if g["name"] == "atr_regime=LO")
    assert g["sprt"]["frozen_sigma"] > 0 and len(g["sprt"]["sigma_days"]) == 4
    gr = next(r for r in rep["gates"] if r["name"] == "atr_regime=LO")
    assert gr["n_scored"] == 5
    assert g["status"] == "graduated" and gr["sprt_state"] == "accept_h1"
    # σ is write-once: a re-scan keeps the same estimation days.
    scan(db, None, reg_path)
    g2 = next(g for g in load_registry(reg_path)["gates"] if g["name"] == "atr_regime=LO")
    assert g2["sprt"]["sigma_days"] == g["sprt"]["sigma_days"]


def test_gate_retires_when_oos_is_healthy(tmp_path):
    reg_path = tmp_path / "g.json"
    _seed(reg_path)
    lo = _ctx(regime_buckets={"atr_regime": "LO", "session": "DAY", "burst": "HOT"})
    # OOS the cell WINS (day-gains ≈ −10¢): H0 accepted → auto-retired.
    fills = [(f"2026-07-{d:02d}", 0.10 + 0.005 * (d % 4), 1.0, "Up", 0.88, lo)
             for d in range(20, 29)]
    db = _mk_ledger(tmp_path, fills)
    scan(db, None, reg_path)
    g = next(g for g in load_registry(reg_path)["gates"] if g["name"] == "atr_regime=LO")
    assert g["status"] == "retired"
    # a retired gate never matches at fire time and is skipped on later scans
    assert fire_time_matches(
        _ctx(regime_buckets={"atr_regime": "LO"}), "Up", "Mon",
        load_registry(reg_path)) == []
    rep2 = scan(db, None, reg_path)
    assert all(r["name"] != "atr_regime=LO" for r in rep2["gates"])


def test_gate_scoring_is_strictly_oos(tmp_path):
    reg_path = tmp_path / "g.json"
    _seed(reg_path, discovered="2026-07-25")
    lo = _ctx(regime_buckets={"atr_regime": "LO", "session": "DAY", "burst": "HOT"})
    # all fills ON or BEFORE the discovery day: nothing may qualify
    fills = [(f"2026-07-{d:02d}", -0.50, 1.0, "Up", 0.88, lo) for d in range(20, 26)]
    rep = scan(_mk_ledger(tmp_path, fills), None, reg_path)
    gr = next(r for r in rep["gates"] if r["name"] == "atr_regime=LO")
    assert gr["n_oos"] == 0 and gr["sprt_state"] == "accruing_sigma"


# ── Fire-time matching + the main.py stamps ───────────────────────────────────

def test_fire_time_matches_only_matching_nonretired():
    reg = {"version": 1, "gates": [
        {"name": "atr_regime=LO", "dim": "atr_regime", "bucket": "LO", "status": "shadow"},
        {"name": "side=Up", "dim": "side", "bucket": "Up", "status": "retired"},
        {"name": "slip=padded", "dim": "slip", "bucket": "padded", "status": "shadow"},
    ]}
    lo = _ctx(regime_buckets={"atr_regime": "LO"})
    assert fire_time_matches(lo, "Up", "Mon", reg) == ["atr_regime=LO"]
    assert fire_time_matches(_ctx(), "Up", "Mon", reg) == []
    # the ENFORCE path requires graduation — a shadow gate may never veto
    assert fire_time_matches(lo, "Up", "Mon", reg, statuses=("graduated",)) == []
    reg["gates"][0]["status"] = "graduated"
    assert fire_time_matches(lo, "Up", "Mon", reg,
                             statuses=("graduated",)) == ["atr_regime=LO"]


def test_fire_time_matches_survives_malformed_registry():
    reg = {"version": 1, "gates": [
        "corrupt-string-entry",
        {"dim": "atr_regime", "bucket": "LO", "status": "shadow"},   # no name
        {"name": "atr_regime=LO", "dim": "atr_regime", "bucket": "LO",
         "status": "shadow"},
    ]}
    lo = _ctx(regime_buckets={"atr_regime": "LO"})
    assert fire_time_matches(lo, "Up", "Mon", reg) == ["atr_regime=LO"]


def test_scar_fields_refire_classification():
    _window_killed_asks.clear()
    cid = "btc-updown-5m-1785200000"
    f = _scar_fields(cid, "Up", 0.85, 12.0)
    assert f["scar_refire_class"] == "first_fire" and f["scar_killed_n"] == 0
    _record_killed_ask(cid, "Up", 0.85)
    assert _scar_fields(cid, "Up", 0.85, None)["scar_refire_class"] == "refire_leq_kill"
    assert _scar_fields(cid, "Up", 0.84, None)["scar_refire_class"] == "refire_leq_kill"
    assert _scar_fields(cid, "Up", 0.87, None)["scar_refire_class"] == "refire_above_kill"
    # other side / other window unaffected
    assert _scar_fields(cid, "Down", 0.85, None)["scar_refire_class"] == "first_fire"
    assert _scar_fields("btc-updown-5m-1785200300", "Up", 0.85,
                        None)["scar_refire_class"] == "first_fire"
    assert _scar_fields(cid, "Up", 0.90, 12.5)["scar_kill_min_ask"] == 0.85
    _window_killed_asks.clear()


def test_record_killed_ask_sweeps_stale_windows():
    _window_killed_asks.clear()
    _record_killed_ask("btc-updown-5m-1000000500", "Up", 0.80)  # ancient window
    _record_killed_ask("btc-updown-5m-9999999900", "Up", 0.81)  # sweeps the old one
    assert 1000000500 not in _window_killed_asks
    assert 9999999900 in _window_killed_asks
    _window_killed_asks.clear()


# ── Enforced-veto journal + resolution ────────────────────────────────────────

def test_record_and_resolve_vetoes(tmp_path):
    db = tmp_path / "labels.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE window_labels (window_id TEXT PRIMARY KEY, "
                "resolved_up INTEGER NOT NULL, final_price REAL, "
                "price_to_beat REAL, labeled_at REAL NOT NULL)")
    con.execute("INSERT INTO window_labels VALUES ('w-up', 1, 100.0, 99.0, 0.0)")
    con.execute("INSERT INTO window_labels VALUES ('w-dn', 0, 98.0, 99.0, 0.0)")
    con.commit(); con.close()
    vp = tmp_path / "vetoes.jsonl"
    record_veto(vp, "atr_regime=LO", "w-up", "Up", 0.80, 4.0)   # would have WON +20¢
    record_veto(vp, "atr_regime=LO", "w-dn", "Up", 0.60, 3.0)   # would have LOST −60¢
    record_veto(vp, "atr_regime=LO", "w-unresolved", "Up", 0.70, 2.0)
    record_veto(vp, "side=Down", "w-up", "Up", 0.80, 4.0)       # 2nd gate, won
    r = resolve_vetoes(vp, db)
    assert r["n"] == 4 and r["resolved"] == 3
    # per-gate: one gate's surplus must not hide another's bleed
    lo = r["per_gate"]["atr_regime=LO"]
    assert lo["n"] == 3 and lo["resolved"] == 2
    assert lo["avoided_cs"] == pytest.approx(20.0)   # −mean(+20, −60)
    # avoided $ = −Σ shares·would/sh = −(5·0.20 + 5·(−0.60)) = +2.0
    assert lo["avoided_usd"] == pytest.approx(2.0)
    sd = r["per_gate"]["side=Down"]
    assert sd["avoided_cs"] == pytest.approx(-20.0)  # vetoing a winner costs


def test_resolve_vetoes_empty(tmp_path):
    r = resolve_vetoes(tmp_path / "none.jsonl", tmp_path / "no.db")
    assert r["n"] == 0 and r["avoided_cs"] is None


# ── Mode provenance + registration controls ───────────────────────────────────

def test_foreign_mode_gate_pauses_instead_of_scoring(tmp_path):
    reg_path = tmp_path / "g.json"
    _seed(reg_path)
    reg = load_registry(reg_path)
    reg["gates"][0]["mode"] = "live"
    save_registry(reg_path, reg)
    lo = _ctx(regime_buckets={"atr_regime": "LO", "session": "DAY", "burst": "HOT"})
    fills = [(f"2026-07-{d:02d}", -0.50, 1.0, "Up", 0.88, lo) for d in range(20, 29)]
    rep = scan(_mk_ledger(tmp_path, fills), None, reg_path, mode="paper")
    gr = next(r for r in rep["gates"] if r["name"] == "atr_regime=LO")
    assert gr["sprt_state"] == "paused_other_mode" and gr["n_scored"] == 0
    g = next(g for g in load_registry(reg_path)["gates"] if g["name"] == "atr_regime=LO")
    assert g["sprt"]["frozen_sigma"] is None        # paper days froze nothing


def test_one_active_gate_per_dimension(tmp_path):
    # atr_regime=LO already active; a flaggable atr_regime=HI cell must wait —
    # complementary buckets of one dim could jointly veto the whole population.
    reg_path = tmp_path / "g.json"
    _seed(reg_path, discovered="2026-07-19")
    fills = []
    for d in range(20, 24):
        day = f"2026-07-{d:02d}"
        hi = _ctx(regime_buckets={"atr_regime": "HI", "session": "DAY", "burst": "HOT"})
        for pnl in (-0.60, -0.50, -0.10 - 0.02 * d):
            fills.append((day, pnl, 1.0, "Up", 0.88, hi))
        for pnl in (0.11, 0.12, 0.13, 0.12):
            fills.append((day, pnl, 1.0, "Up", 0.88,
                          _ctx(regime_buckets={"atr_regime": "MID",
                                               "session": "DAY", "burst": "HOT"})))
    rep = scan(_mk_ledger(tmp_path, fills), None, reg_path)
    assert "atr_regime=HI" not in rep["registered"]


# ── The nightly read wrapper (health-job integration path) ────────────────────

def test_scar_scan_read_wrapper(tmp_path):
    mod = _load_harness()
    db = _toxic_ledger(tmp_path)
    rep = mod.scar_scan_read(db, None, [],
                             registry_path=tmp_path / "g.json",
                             vetoes_path=tmp_path / "v.jsonl")
    assert "atr_regime=LO" in rep["registered"]
    assert rep["vetoes"]["n"] == 0
    assert rep["n_fills"] == 28


# ── Registry corruption + malformed entries + decided-test permanence ─────────

def test_corrupt_registry_raises_and_is_never_wiped(tmp_path):
    # An EXISTING-but-unparseable registry must raise (callers degrade), never
    # be silently replaced with an empty one — that would erase the retired-
    # gate never-re-register ledger and every frozen σ.
    p = tmp_path / "g.json"
    p.write_text("{corrupt json!!")
    with pytest.raises(Exception):
        load_registry(p)
    with pytest.raises(Exception):
        scan(tmp_path / "missing.db", None, p)
    assert p.read_text() == "{corrupt json!!"   # file untouched
    # a MISSING file still bootstraps empty (first run)
    assert load_registry(tmp_path / "none.json") == {"version": 1, "gates": []}


def test_malformed_gate_entry_skipped_not_fatal(tmp_path):
    # A hand-edited entry missing dim/bucket/discovered must not KeyError the
    # whole nightly scan — it is skipped and reported.
    p = tmp_path / "g.json"
    save_registry(p, {"version": 1, "gates": [
        {"name": "handmade", "status": "shadow"}]})
    rep = scan(tmp_path / "missing.db", None, p)
    assert rep["malformed"] == ["handmade"]
    assert rep["gates"] == []


def test_bucketless_gate_never_matches_cold_dim():
    # A gate whose bucket key was mangled away must degrade to no-match, not
    # match every decision whose dim stamp is None (None == None).
    reg = {"version": 1, "gates": [
        {"name": "frv=?", "dim": "frv", "status": "graduated"}]}
    ctx = _ctx(regime_buckets={"frv": None})
    assert fire_time_matches(ctx, "Up", "Mon", reg, statuses=("graduated",)) == []


def test_resolve_vetoes_dedups_window_gate(tmp_path):
    # The once-per-window latch is in-memory only; a crash-restart can journal
    # the same (window, gate) twice — the resolution must count it once.
    db = tmp_path / "labels.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE window_labels (window_id TEXT PRIMARY KEY, "
                "resolved_up INTEGER NOT NULL, final_price REAL, "
                "price_to_beat REAL, labeled_at REAL NOT NULL)")
    con.execute("INSERT INTO window_labels VALUES ('w-up', 1, 100.0, 99.0, 0.0)")
    con.commit(); con.close()
    vp = tmp_path / "vetoes.jsonl"
    record_veto(vp, "atr_regime=LO", "w-up", "Up", 0.80, 4.0)
    record_veto(vp, "atr_regime=LO", "w-up", "Up", 0.80, 4.0)   # restart dupe
    r = resolve_vetoes(vp, db)
    assert r["n"] == 1
    assert r["per_gate"]["atr_regime=LO"]["n"] == 1
    assert r["per_gate"]["atr_regime=LO"]["avoided_usd"] == pytest.approx(-1.0)


def test_graduated_gate_stays_graduated_after_volatile_days(tmp_path):
    # Post-decision volatility must not retro-void a finished test: the gate
    # graduated on its consumed prefix and replays deterministically forever.
    reg_path = tmp_path / "g.json"
    _seed(reg_path)
    lo = _ctx(regime_buckets={"atr_regime": "LO", "session": "DAY", "burst": "HOT"})
    fills = [(f"2026-07-{d:02d}", -(0.10 + 0.02 * (d % 2)), 1.0, "Up", 0.88, lo)
             for d in range(20, 29)]
    scan(_mk_ledger(tmp_path, fills), None, reg_path)
    g = next(g for g in load_registry(reg_path)["gates"]
             if g["name"] == "atr_regime=LO")
    assert g["status"] == "graduated"
    # two wild post-decision days land in the cell
    wild = fills + [("2026-07-30", -0.80, 1.0, "Up", 0.88, lo),
                    ("2026-07-31", 0.60, 1.0, "Up", 0.88, lo)]
    wild_dir = tmp_path / "b"
    wild_dir.mkdir()
    rep = scan(_mk_ledger(wild_dir, wild), None, reg_path)
    g2 = next(g for g in load_registry(reg_path)["gates"]
              if g["name"] == "atr_regime=LO")
    gr = next(r for r in rep["gates"] if r["name"] == "atr_regime=LO")
    assert g2["status"] == "graduated" and gr["sprt_state"] == "accept_h1"
    assert "restarted" not in g2 and g2["sprt"]["frozen_sigma"] is not None


# ── Resolution-mechanism watch (TWAP rollout tripwire) ────────────────────────

def _mk_labels_db(tmp_path, labels):
    """labels: list of (window_ts, final_price, price_to_beat)."""
    import time as _t
    db = tmp_path / "labels_watch.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE window_labels (window_id TEXT PRIMARY KEY, "
                "resolved_up INTEGER NOT NULL, final_price REAL, "
                "price_to_beat REAL, labeled_at REAL NOT NULL)")
    for ts, fp, ptb in labels:
        con.execute("INSERT INTO window_labels VALUES (?, 1, ?, ?, ?)",
                    (f"btc-updown-5m-{ts}", fp, ptb, _t.time()))
    con.commit(); con.close()
    return db


def test_resolution_watch_terminal_snapshot_intact(tmp_path):
    # final_price(N) == price_to_beat(N+1) — the current rule's invariant
    # (516/516 bit-exact on the live ledger 2026-07-30).
    mod = _load_harness()
    db = _mk_labels_db(tmp_path, [
        (1000, 64500.00, 64480.00),
        (1300, 64511.25, 64500.00),
        (1600, 64490.10, 64511.25),
        (1900, 64502.00, 64490.10),
    ])
    r = mod.resolution_snapshot_read(db)
    assert r["checked"] == 3 and r["matched"] == 3
    assert r["mismatches"] == []


def test_resolution_watch_flags_twap_style_divergence(tmp_path):
    # An averaged final_price stops equalling the next boundary snapshot.
    mod = _load_harness()
    db = _mk_labels_db(tmp_path, [
        (1000, 64493.70, 64480.00),   # TWAP-ish average ≠ 64511.25 boundary
        (1300, 64506.10, 64511.25),   #   (and its own final is off too)
        (1600, 64490.10, 64512.80),
    ])
    r = mod.resolution_snapshot_read(db)
    assert r["checked"] == 2 and r["matched"] == 0
    assert r["worst"] > 5.0
    assert len(r["mismatches"]) == 2


def test_resolution_watch_skips_gaps_and_null_prices(tmp_path):
    mod = _load_harness()
    db = _mk_labels_db(tmp_path, [
        (1000, 64500.00, 64480.00),
        (1300, None, 64500.00),        # unlabeled final — pair (1000,1300) ok,
        (1900, 64502.00, 64490.10),    # (1300,1600) missing, (1600,1900) missing
    ])
    r = mod.resolution_snapshot_read(db)
    assert r["checked"] == 1 and r["matched"] == 1
    assert mod.resolution_snapshot_read(tmp_path / "absent.db") is None
