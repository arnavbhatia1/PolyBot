"""live_health_read() — the money-side kill-rule read (scripts/analyze_late_window.py).

Locks the two things that make it the binding-gate metric: per-fill net =
pnl / shares_held (pnl is ALREADY net of fees — size includes the entry fee,
so pnl = payout - size subtracts it once), equal-weight and day-clustered;
and the kill-rule OR-legs (trailing-4d < +2c/sh, trailing-8d t < 2.0)
activate as soon as they have the ET days.
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


def _make_db(path, rows, legs=None):
    """rows = list of (id, pnl, fees, shares_held, exit_ts_iso); legs maps
    row id -> signal_leg stamp (production shape: positions always carry
    indicator_snapshot with a trade_context)."""
    import json as _j
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, shares_held REAL, "
                "indicator_snapshot TEXT)")
    con.execute("CREATE TABLE trade_history (id INTEGER PRIMARY KEY, pnl REAL, "
                "fees REAL, exit_timestamp TEXT)")
    for rid, pnl, fees, sh, ts in rows:
        snap = _j.dumps({"trade_context": {"signal_leg": (legs or {}).get(rid)}})
        con.execute("INSERT INTO positions (id, shares_held, indicator_snapshot) "
                    "VALUES (?,?,?)", (rid, sh, snap))
        con.execute("INSERT INTO trade_history (id, pnl, fees, exit_timestamp) "
                    "VALUES (?,?,?,?)", (rid, pnl, fees, ts))
    con.commit()
    con.close()


def _read(tmp_path, rows, name="live.db", legs=None):
    mod = _load()
    db = tmp_path / name
    _make_db(str(db), rows, legs=legs)
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
    # 4 days x 2 fills each +5c/sh -> trailing4 0.05 > floor, <8 days so t-leg n/a.
    rows = [(i * 2 + j, 5.0, 0.0, 100.0, _ts(d))
            for i, d in enumerate(["07-04", "07-05", "07-06", "07-07"])
            for j in (1, 2)]
    r = _read(tmp_path, rows)
    assert r["n_days"] == 4
    assert r["trailing4_mean"] == pytest.approx(0.05)
    assert r["trailing8_t"] is None
    assert r["kill_rule_tripped"] is False


def test_thin_cents_per_share_is_not_a_kill(tmp_path):
    """A high-price maker fill earns sub-1c/sh by construction — judging it on
    c/sh would halt a leg making money every day. Dollars decide."""
    rows = [(i * 2 + j, 1.0, 0.0, 100.0, _ts(d))
            for i, d in enumerate(["07-04", "07-05", "07-06", "07-07"])
            for j in (1, 2)]
    r = _read(tmp_path, rows)
    assert r["trailing4_mean"] == pytest.approx(0.01)     # 1c/sh — thin
    assert r["trailing4_usd"] == pytest.approx(2.0)       # but +$2/day
    assert r["kill_rule_tripped"] is False


def test_kill_rule_tripped_when_trailing4_dollars_negative(tmp_path):
    rows = [(i * 2 + j, -1.0, 0.0, 100.0, _ts(d))
            for i, d in enumerate(["07-04", "07-05", "07-06", "07-07"])
            for j in (1, 2)]
    r = _read(tmp_path, rows)
    assert r["trailing4_usd"] == pytest.approx(-2.0)
    assert r["kill_rule_tripped"] is True


def test_sparse_fills_keep_accruing_not_tripping(tmp_path):
    """One rung loss after quiet days is an anecdote, not a verdict: with
    fewer than 5 fills in the trailing 4 days the dollars rule keeps accruing
    (measured 08-18: a -$4.50 loss after three zero days read as
    trailing-negative on a leg +$64 on the week)."""
    r = _read(tmp_path, [(i, -1.0, 0.0, 100.0, _ts(d)) for i, d in
                         enumerate(["07-04", "07-05", "07-06", "07-07"], 1)])
    assert r["trailing4_usd"] == pytest.approx(-1.0)
    assert r["kill_rule_tripped"] is None                 # 4 fills < 5 — accruing


def test_one_lock_dip_loss_trips_immediately(tmp_path):
    """Every lock_dip fire is max-tier, so a loss IS a breach of the
    never-breach bound — mechanism failure, not variance. One halts."""
    r = _read(tmp_path, [(1, 5.0, 0.0, 100.0, _ts("07-04")),
                         (2, -8.0, 0.0, 100.0, _ts("07-04"))],
              legs={1: "lock_dip", 2: "lock_dip"})
    assert r["breach_losses"] == 1
    assert r["legs"]["lock_dip"]["n_losses"] == 1
    assert r["kill_rule_tripped"] is True                 # on day 1, not day 4


def test_maker_ladder_loss_does_not_trip_the_breach_clause(tmp_path):
    """Ladder rungs are priced for occasional loss (break-even = price paid) —
    a rung loss feeds the dollars rule only, never the halt-on-sight clause."""
    r = _read(tmp_path, [(1, 5.0, 0.0, 100.0, _ts("07-04")),
                         (2, -3.0, 0.0, 100.0, _ts("07-04"))],
              legs={1: "maker_bid", 2: "maker_bid"})
    assert r["breach_losses"] == 0
    assert r["kill_rule_tripped"] is None                 # < 4 days, no breach


def test_trailing4_uses_only_last_4_days(tmp_path):
    # 5 days: a bad first day must not drag the trailing-4 window.
    rows = [(1, -20.0, 0.0, 100.0, _ts("07-03"))]                 # -0.20/sh (dropped)
    rows += [(i * 2 + j, 5.0, 0.0, 100.0, _ts(d))
             for i, d in enumerate(["07-04", "07-05", "07-06", "07-07"])
             for j in (2, 3)]                                     # last 4 = +0.05
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
    con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, shares_held REAL, "
                "indicator_snapshot TEXT)")
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


def test_legs_breakdown_groups_by_signal_leg(tmp_path):
    """Per-leg ledgers: fills stamped signal_leg group separately; rows without
    a stamp report as 'unstamped' — never silently merged into a leg."""
    r = _read(tmp_path, [
        (1, 2.0, 0.0, 5.0, _ts("07-04")),    # lock_dip win
        (2, -1.0, 0.0, 5.0, _ts("07-04")),   # open_edge loss
        (3, 1.0, 0.0, 5.0, _ts("07-05")),    # unstamped legacy row
    ], legs={1: "lock_dip", 2: "open_edge"})
    legs = r["legs"]
    assert set(legs) == {"lock_dip", "open_edge", "unstamped"}
    assert legs["lock_dip"]["n_fills"] == 1
    assert legs["lock_dip"]["net_per_sh"] == pytest.approx(0.4)
    assert legs["open_edge"]["net_per_sh"] == pytest.approx(-0.2)
    assert legs["open_edge"]["win_rate"] == 0.0


def test_trailing_window_is_calendar_days_not_active_days(tmp_path):
    """A quiet leg must not be judged on tape from three weeks ago: the window
    is the last 4 CALENDAR ET days, zero-filled where nothing filled."""
    rows = [(i, 5.0, 0.0, 100.0, _ts(d)) for i, d in
            enumerate(["07-01", "07-02", "07-03", "07-04"], 1)]
    rows.append((5, -4.0, 0.0, 100.0, _ts("07-20")))
    r = _read(tmp_path, rows)
    assert r["trailing4_days"] == ["2026-07-17", "2026-07-18", "2026-07-19", "2026-07-20"]
    assert r["trailing4_usd"] == pytest.approx(-1.0)      # (0+0+0-4)/4, not the 4 active days
    assert r["kill_rule_tripped"] is None                 # 1 fill < 5 — accruing


def test_one_fill_a_day_bleeding_keeps_the_unchanged_fill_threshold(tmp_path):
    """30 days of a single losing fill a day: the window is the last 4 calendar
    days and the 5-fill floor still says accrue."""
    rows = [(i, -1.0, 0.0, 100.0, _ts(f"07-{i:02d}")) for i in range(1, 31)]
    r = _read(tmp_path, rows)
    assert r["trailing4_days"] == ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30"]
    assert r["kill_rule_tripped"] is None


def test_a_bleeding_leg_trips_even_when_the_aggregate_is_positive(tmp_path):
    """Per-leg verdict: a leg losing $9/day must not hide behind one making $10."""
    rows, legs = [], {}
    for i, d in enumerate(["07-04", "07-05", "07-06", "07-07"]):
        for j, (pnl, leg) in enumerate([(-4.5, "A"), (-4.5, "A"),
                                        (5.0, "B"), (5.0, "B")]):
            rid = i * 4 + j + 1
            rows.append((rid, pnl, 0.0, 100.0, _ts(d)))
            legs[rid] = leg
    r = _read(tmp_path, rows, legs=legs)
    assert r["trailing4_usd"] == pytest.approx(1.0)       # aggregate is POSITIVE
    assert r["tripped_legs"] == ["A"]
    assert r["kill_rule_tripped"] is True


def test_quiet_days_then_one_loss_still_does_not_trip(tmp_path):
    rows = [(i, 5.0, 0.0, 100.0, _ts(d)) for i, d in
            enumerate(["07-04", "07-05", "07-06"], 1)]
    rows.append((4, -4.5, 0.0, 100.0, _ts("07-10")))
    r = _read(tmp_path, rows)
    assert r["tripped_legs"] == []
    assert r["kill_rule_tripped"] is None
