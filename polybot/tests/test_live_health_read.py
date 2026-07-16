"""live_health_read() — the money-side post-live kill-rule read (scripts/analyze_late_window.py).

Locks the two things that make it a faithful analog of the SIM health_read():
per-fill net = pnl / shares_held (pnl is ALREADY net of fees — size includes the
entry fee, so pnl = payout - size subtracts it once) == the harness's
win - fill - fee(fill), equal-weight and day-clustered; and the kill-rule OR-legs
(trailing-4d < +2c/sh, trailing-8d t < 2.0) activate as soon as they have the ET days.
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "alw_test", ROOT / "scripts" / "analyze_late_window.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(path, rows):
    """rows = list of (id, pnl, fees, shares_held, exit_ts_iso)."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, shares_held REAL)")
    con.execute("CREATE TABLE trade_history (id INTEGER PRIMARY KEY, pnl REAL, "
                "fees REAL, exit_timestamp TEXT)")
    for rid, pnl, fees, sh, ts in rows:
        con.execute("INSERT INTO positions (id, shares_held) VALUES (?,?)", (rid, sh))
        con.execute("INSERT INTO trade_history (id, pnl, fees, exit_timestamp) "
                    "VALUES (?,?,?,?)", (rid, pnl, fees, ts))
    con.commit()
    con.close()


def _read(tmp_path, rows, name="live.db"):
    mod = _load()
    db = tmp_path / name
    _make_db(str(db), rows)
    mod.LIVE_DB = db
    return mod.live_health_read()


# ── noon-UTC on distinct dates → distinct America/New_York (EDT, UTC-4) days ──
def _ts(day):  # day like "07-05"
    return f"2026-{day}T12:00:00+00:00"


def test_none_when_db_absent(tmp_path):
    mod = _load()
    mod.LIVE_DB = tmp_path / "does_not_exist.db"
    assert mod.live_health_read() is None


def test_none_when_no_fills(tmp_path):
    assert _read(tmp_path, []) is None


def test_per_share_is_pnl_over_shares_fee_already_netted(tmp_path):
    # pnl is ALREADY net of fees (size = shares*entry + entry_fee; pnl = payout - size),
    # so net/sh = pnl/shares == the harness win - fill - fee(fill). Subtracting the
    # stored `fees` again (the pre-2026-07-13 bug) double-counts it. Fixtures follow the
    # production write convention: a winner filled at 0.55 -> $1 and a loser at 0.30 -> $0.
    r = _read(tmp_path, [
        (1, 6.9486, 0.2782, 16.0595, _ts("07-04")),  # 16.0595 sh @0.55 -> $1: pnl = 16.0595 - (8.8327+0.2782)
        (2, -6.294, 0.294, 20.0, _ts("07-05")),      # 20 sh @0.30 -> $0: pnl = -(6.0+0.294)
    ])
    win = 6.9486 / 16.0595
    loss = -6.294 / 20.0
    assert r["n_fills"] == 2
    assert r["net_per_sh"] == pytest.approx((win + loss) / 2, abs=1e-9)
    assert r["win_rate"] == pytest.approx(0.5)                          # 1 of 2 pnl>0
    assert win == pytest.approx(1 - 0.55 - 0.07 * 0.55 * 0.45, abs=1e-3)  # == harness win-fill-fee
    # the fee must NOT be subtracted a second time (that was the double-count bug)
    buggy = ((6.9486 - 0.2782) / 16.0595 + (-6.294 - 0.294) / 20.0) / 2
    assert r["net_per_sh"] != pytest.approx(buggy, abs=1e-4)


def test_equal_weight_within_day_then_daily_clustered(tmp_path):
    # Two fills same ET day average to the daily mean (equal-weight, one series point).
    r = _read(tmp_path, [
        (1, 2.0, 0.0, 100.0, _ts("07-04")),   # +0.02/sh
        (2, 8.0, 0.0, 100.0, _ts("07-04")),   # +0.08/sh -> day mean +0.05
        (3, 6.0, 0.0, 100.0, _ts("07-05")),   # +0.06/sh
    ])
    assert r["n_days"] == 2
    series = dict(r["series"])
    assert series["2026-07-04"] == pytest.approx(0.05)
    assert series["2026-07-05"] == pytest.approx(0.06)


def test_kill_rule_none_under_4_days(tmp_path):
    r = _read(tmp_path, [
        (1, 5.0, 0.0, 100.0, _ts("07-04")),
        (2, 5.0, 0.0, 100.0, _ts("07-05")),
        (3, 5.0, 0.0, 100.0, _ts("07-06")),
    ])
    assert r["n_days"] == 3
    assert r["trailing4_mean"] is None
    assert r["trailing8_t"] is None
    assert r["kill_rule_tripped"] is None


def test_kill_rule_healthy_when_trailing4_above_floor(tmp_path):
    # 4 days each +5c/sh -> trailing4 0.05 > 0.02 floor, <8 days so t-leg n/a.
    r = _read(tmp_path, [(i, 5.0, 0.0, 100.0, _ts(d)) for i, d in
                         enumerate(["07-04", "07-05", "07-06", "07-07"], 1)])
    assert r["n_days"] == 4
    assert r["trailing4_mean"] == pytest.approx(0.05)
    assert r["trailing8_t"] is None
    assert r["kill_rule_tripped"] is False


def test_kill_rule_tripped_when_trailing4_below_floor(tmp_path):
    # 4 days each +1c/sh -> trailing4 0.01 < 0.02 floor -> tripped.
    r = _read(tmp_path, [(i, 1.0, 0.0, 100.0, _ts(d)) for i, d in
                         enumerate(["07-04", "07-05", "07-06", "07-07"], 1)])
    assert r["trailing4_mean"] == pytest.approx(0.01)
    assert r["kill_rule_tripped"] is True


def test_trailing4_uses_only_last_4_days(tmp_path):
    # 5 days: a bad first day must not drag the trailing-4 window.
    rows = [(1, -20.0, 0.0, 100.0, _ts("07-03"))]                 # -0.20/sh (dropped)
    rows += [(i, 5.0, 0.0, 100.0, _ts(d)) for i, d in
             enumerate(["07-04", "07-05", "07-06", "07-07"], 2)]  # last 4 = +0.05
    r = _read(tmp_path, rows)
    assert r["n_days"] == 5
    assert r["trailing4_mean"] == pytest.approx(0.05)
    assert r["kill_rule_tripped"] is False


def test_shares_held_null_or_zero_rows_skipped(tmp_path):
    # A row with no shares can't yield a per-share number -> excluded by the query.
    r = _read(tmp_path, [
        (1, 5.0, 0.0, 100.0, _ts("07-04")),
        (2, 5.0, 0.0, 0.0, _ts("07-05")),   # shares_held 0 -> skipped
    ])
    assert r["n_fills"] == 1


def test_db_path_and_since_iso_scope_the_read(tmp_path):
    """Paper re-validation: db_path targets the paper DB and since_iso excludes
    pre-epoch fills (they ran different code/config) — the BINDING-gate scope."""
    mod = _load()
    db = tmp_path / "paper.db"
    _make_db(str(db), [
        (1, -5.0, 0.5, 10.0, _ts("07-05")),   # pre-epoch: must be excluded
        (2, 4.0, 0.4, 10.0, _ts("07-09")),
        (3, 2.0, 0.2, 10.0, _ts("07-09")),
    ])
    r = mod.live_health_read(db, "2026-07-08T17:15:00+00:00")
    assert r["n_fills"] == 2
    assert r["n_days"] == 1
    assert r["net_per_sh"] == pytest.approx((4.0 / 10 + 2.0 / 10) / 2)   # pnl/shares (fee already netted)
    assert "since 2026-07-08T17:15:00+00:00" in r["label"]

    # unscoped default keeps everything
    r_all = mod.live_health_read(db)
    assert r_all["n_fills"] == 3 and r_all["n_days"] == 2


def test_join_uses_position_id_when_sequences_drift(tmp_path):
    """trade_history ids drift from position ids whenever the AUTOINCREMENT
    sequences diverge (unclosed positions, a ledger reset — observed offset 101
    after the 07-09 reset). With position_id present, the read must pair rows
    by the TRUE link, not the implicit id coincidence."""
    mod = _load()
    db = tmp_path / "drift.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, shares_held REAL)")
    con.execute("CREATE TABLE trade_history (id INTEGER PRIMARY KEY, pnl REAL, "
                "fees REAL, exit_timestamp TEXT, position_id INTEGER)")
    con.execute("INSERT INTO positions (id, shares_held) VALUES (8982, 5.0)")
    # trade id 9083 (drifted) but position_id links correctly
    con.execute("INSERT INTO trade_history (id, pnl, fees, exit_timestamp, position_id) "
                "VALUES (9083, 2.0, 0.5, ?, 8982)", (_ts("07-09"),))
    # legacy row: NULL position_id falls back to id pairing (id 8982 == position)
    con.execute("INSERT INTO trade_history (id, pnl, fees, exit_timestamp, position_id) "
                "VALUES (8982, 1.0, 0.0, ?, NULL)", (_ts("07-09"),))
    con.commit(); con.close()
    r = mod.live_health_read(db)
    assert r["n_fills"] == 2
    assert r["net_per_sh"] == pytest.approx((2.0/5.0 + 1.0/5.0) / 2)   # pnl/shares (fee already netted)
