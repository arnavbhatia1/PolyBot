"""Phase 3 kill-bar evaluation: nightly exit-value model vs the live ExitBoundary.

Pool: counterfactual records stamped after the recorder epoch (2026-06-11
10:44 ET), loaded exactly like sweep_exit_policy.build(). Each record is
matched to the window_paths row nearest its decision moment
(elapsed = 300 - seconds_remaining, window_id == market_id, tolerance ±2 s);
records with no matching row — or NULL book/coinbase/strike fields at the
match — are skipped, so BOTH policies score the same matched pool.

BOUNDARY policy (live): fire iff
    holding_edge <= effective_exit_threshold(exit_threshold_used,
                                             seconds_remaining, market_price)
— the criterion live uses and sweep_exit_policy replays.

MODEL policy (memory/exit_model/exit_model.json):
    xn       = (x - norm_mu) / norm_sd        x = artifact FEATURES from the row
    p_up     = sigmoid([1, xn] @ beta_prob_up)
    drift_up = [1, xn] @ beta_drift_up        E[bid_up 60s ahead - bid_up now]
    b        = own-side bid (bid_up / bid_down); p_side = p_up (Up) / 1 - p_up
    FIRE iff  b - 0.07*b*(1-b) > p_side  AND  drift_side <= 0
Selling now nets the bid minus the taker fee; holding is worth ~p_side at
resolution, and a positive expected bid drift says waiting improves the exit —
so the model scalps only when the fee-net bid already beats resolution EV and
the bid is not expected to improve. drift_side = drift_up for Up; for Down it
is -drift_up — an APPROXIMATION: bid_down ~= 1 - ask_up, so d(bid_down) ~=
-d(ask_up) ~= -d(bid_up) while the Up spread is stable (the artifact only
carries the Up-bid drift head).

Both policies use the branch-faithful arm selection of
sweep_exit_policy.policy_pnl: loss-cut scalps always keep their actual arm;
hold records flip to their worst-moment hypothetical scalp only when the
policy fires there AND none of the threshold-independent live HOLD branches
held — whipsaw cushion, deep-loss-hold, or loss-cut positive-edge hold.

KILL BAR (tasks/todo.md Phase 3): positive ITM (market_price >= 0.5) $
improvement over >= 5 distinct ET shadow days.

OUT-OF-SAMPLE GUARANTEE: the first run freezes the live artifact to
exit_model_shadow.json and reuses it unchanged for the whole shadow; only
records whose ENTIRE window starts after that artifact's fitted_at are scored.
The model trains per-tick on each window's resolution label, so a window that
overlaps training has leaked its label — those are dropped (window-level, not
decision-tick, cutoff). This keeps the 5-day comparison honest even though the
nightly job keeps refitting the live artifact underneath. --refreeze re-baselines
from the current live artifact (e.g. to restart the shadow window).

  python scripts/shadow_exit_model.py [--db polybot/db/polybot_paper.db]
  python scripts/shadow_exit_model.py --refreeze   # re-snapshot the baseline
  python scripts/shadow_exit_model.py --selftest
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polybot.core.exit_boundary import effective_exit_threshold  # noqa: E402
from polybot.execution.base import DEFAULT_FEE_RATE  # noqa: E402
from polybot.exit_model import ARTIFACT_PATH, FEATURES  # noqa: E402
from polybot.config.param_registry import default_for as _default  # noqa: E402

_LOSS_CUT_FRACTION = _default("loss_cut_fraction")
_LOSS_CUT_TIME_S = _default("loss_cut_time_s")
from scripts.diagnose_edge import load_records  # noqa: E402

ET = ZoneInfo("America/New_York")
RECORDER_EPOCH = datetime(2026, 6, 11, 10, 44, tzinfo=ET)  # recorders went live
WINDOW_LEN_S = 300.0
MATCH_TOL_S = 2.0
KILL_BAR_DAYS = 5
_NEEDED_COLS = ("bid_up", "ask_up", "bid_down", "ask_down", "coinbase_price", "strike")
# Frozen baseline snapshot, beside the live artifact. Pins fitted_at for the shadow.
SHADOW_ARTIFACT_PATH = ARTIFACT_PATH.with_name("exit_model_shadow.json")


def _et_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%Y-%m-%d")


def open_db_with_table(db: Path, table: str) -> tuple[sqlite3.Connection, Path] | None:
    """Read-only connection to whichever DB holds `table`: the given trading DB
    or the recorder's sidecar window_paths.db beside it (the 1 Hz stream can be
    split out of the live DB to keep lock contention off the hot path)."""
    for cand in (db, db.with_name("window_paths.db")):
        if not cand.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{cand.resolve().as_posix()}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            continue
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone():
                return conn, cand
        except sqlite3.OperationalError:
            pass
        conn.close()
    return None


# ── pool ──────────────────────────────────────────────────────────────────────

def build() -> tuple[list[dict], dict]:
    """sweep_exit_policy.build() plus the recorder-epoch filter and the
    decision-moment timestamp (window_ts + 300 - seconds_remaining)."""
    entry_by_pid = {}
    for t in load_records("outcomes"):
        pid = t.get("position_id")
        if pid is not None and t.get("entry_price"):
            entry_by_pid[pid] = (t["entry_price"], t.get("side", ""))
    epoch_ts = RECORDER_EPOCH.timestamp()
    recs, skipped = [], {"missing_fields": 0, "pre_epoch": 0, "bad_window_ts": 0}
    for c in load_records("counterfactuals"):
        actual, cf = c.get("actual") or {}, c.get("counterfactual") or {}
        kind = "hold" if actual.get("exit_reason") == "hold" else "scalp"
        ctx = c.get("context_at_scalp") or c.get("context_at_worst_moment") or {}
        if not ctx or actual.get("pnl") is None or cf.get("pnl") is None:
            skipped["missing_fields"] += 1
            continue
        he, sr = ctx.get("holding_edge"), ctx.get("seconds_remaining")
        mp = ctx.get("market_price")
        if he is None or sr is None or mp is None or not (0 < mp < 1):
            skipped["missing_fields"] += 1
            continue
        try:
            ts_rec = datetime.fromisoformat(
                (c.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            skipped["missing_fields"] += 1
            continue
        if ts_rec < epoch_ts:
            skipped["pre_epoch"] += 1
            continue
        market_id = c.get("market_id") or ""
        try:
            window_ts = int(market_id.rsplit("-", 1)[-1])
        except ValueError:
            skipped["bad_window_ts"] += 1
            continue
        thr = ctx.get("exit_threshold_used")
        entry, side = entry_by_pid.get(
            c.get("position_id"), (ctx.get("entry_price"), c.get("side", "")))
        decision_ts = window_ts + (WINDOW_LEN_S - sr)
        recs.append(dict(kind=kind, he=he, sr=sr, mp=mp,
                         thr=-0.10 if thr is None else thr,
                         loss_cut=bool(ctx.get("loss_cut")),
                         dist=ctx.get("btc_distance_atr"),
                         entry=entry, side=side or c.get("side", ""),
                         act=actual["pnl"], cf=cf["pnl"],
                         market_id=market_id, window_ts=window_ts,
                         decision_ts=decision_ts, day=_et_day(decision_ts)))
    return recs, skipped


def attach_rows(conn: sqlite3.Connection, recs: list[dict],
                tol: float = MATCH_TOL_S) -> tuple[list[dict], dict]:
    """Attach the window_paths row nearest each record's decision moment."""
    matched, skipped = [], {"no_row": 0, "null_fields": 0}
    for r in recs:
        cur = conn.execute(
            "SELECT elapsed_s, bid_up, ask_up, bid_down, ask_down,"
            " depth3_bid_up, depth3_ask_up, depth3_bid_down, depth3_ask_down,"
            " coinbase_price, strike FROM window_paths"
            " WHERE window_id = ? AND ts BETWEEN ? AND ?"
            " ORDER BY ABS(ts - ?) LIMIT 1",
            (r["market_id"], r["decision_ts"] - tol, r["decision_ts"] + tol,
             r["decision_ts"]))
        row = cur.fetchone()
        if row is None:
            skipped["no_row"] += 1
            continue
        d = dict(row)
        if any(d[k] is None for k in _NEEDED_COLS):
            skipped["null_fields"] += 1
            continue
        r["row"] = d
        matched.append(r)
    return matched, skipped


# ── the two policies ──────────────────────────────────────────────────────────

def load_artifact(path: Path) -> dict | None:
    try:
        art = json.loads(path.read_text(encoding="utf-8"))
        for key in ("features", "norm_mu", "norm_sd", "beta_prob_up", "beta_drift_up"):
            if key not in art:
                return None
        return art
    except (OSError, json.JSONDecodeError):
        return None


def _fitted_ts(artifact: dict) -> float | None:
    """The artifact's training cutoff as a unix timestamp (None if unparseable)."""
    raw = artifact.get("fitted_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def shadow_artifact(refreeze: bool = False) -> tuple[dict | None, bool]:
    """The frozen shadow baseline: a snapshot of the live artifact taken on the
    first run and reused unchanged for the rest of the 5-day shadow. Freezing pins
    fitted_at so every shadow day scores the SAME model — one trained before the
    shadow window — keeping the comparison out-of-sample even as the nightly job
    refits the live artifact underneath. Returns (artifact, froze_this_run);
    ``refreeze`` re-snapshots from the current live artifact."""
    if refreeze:
        try:
            SHADOW_ARTIFACT_PATH.unlink()
        except FileNotFoundError:
            pass
    frozen = load_artifact(SHADOW_ARTIFACT_PATH)
    if frozen is not None:
        return frozen, False
    live = load_artifact(ARTIFACT_PATH)
    if live is None:
        return None, False
    SHADOW_ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (mirrors nightly_refit_job): a half-written freeze would be read
    # back as None and silently re-snapshot a newer, leaked baseline mid-shadow.
    tmp = SHADOW_ARTIFACT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(live, indent=1), encoding="utf-8")
    tmp.replace(SHADOW_ARTIFACT_PATH)
    return live, True


def model_predict(artifact: dict, row: dict) -> tuple[float, float]:
    """(p_up, drift_up) from the artifact's two heads on a window_paths row."""
    feat = {
        "bid_up": row["bid_up"], "ask_up": row["ask_up"],
        "bid_down": row["bid_down"], "ask_down": row["ask_down"],
        "spread_up": row["ask_up"] - row["bid_up"],
        "spread_down": row["ask_down"] - row["bid_down"],
        "depth3_bid_up": row["depth3_bid_up"] or 0.0,
        "depth3_ask_up": row["depth3_ask_up"] or 0.0,
        "depth3_bid_down": row["depth3_bid_down"] or 0.0,
        "depth3_ask_down": row["depth3_ask_down"] or 0.0,
        "coinbase_minus_strike": row["coinbase_price"] - row["strike"],
        "elapsed_s": row["elapsed_s"],
    }
    x = [feat[name] for name in artifact.get("features", FEATURES)]
    xn = [(v - m) / (s if s else 1.0)
          for v, m, s in zip(x, artifact["norm_mu"], artifact["norm_sd"])]
    bp, bd = artifact["beta_prob_up"], artifact["beta_drift_up"]
    z = bp[0] + sum(b * v for b, v in zip(bp[1:], xn))
    p_up = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
    drift_up = bd[0] + sum(b * v for b, v in zip(bd[1:], xn))
    return p_up, drift_up


def model_fires(artifact: dict, rec: dict) -> bool:
    row = rec["row"]
    p_up, drift_up = model_predict(artifact, row)
    if rec["side"] == "Down":
        b, p_side, drift = row["bid_down"], 1.0 - p_up, -drift_up
    else:
        b, p_side, drift = row["bid_up"], p_up, drift_up
    return (b - DEFAULT_FEE_RATE * b * (1.0 - b) > p_side) and drift <= 0.0


def boundary_fires(rec: dict) -> bool:
    return rec["he"] <= effective_exit_threshold(rec["thr"], rec["sr"], rec["mp"])


# ── branch-faithful scoring (sweep_exit_policy.policy_pnl arm selection) ──────

def chosen_pnl(rec: dict, fires: bool) -> float:
    if rec["kind"] == "scalp":
        return rec["act"] if (rec["loss_cut"] or fires) else rec["cf"]
    dist = rec["dist"]
    wrong_side = dist is not None and (
        (rec["side"] == "Up" and dist < 0) or (rec["side"] == "Down" and dist > 0))
    whipsaw = wrong_side and abs(dist) <= 0.5
    deep_loss = (rec["he"] < -0.10 and rec["entry"] is not None
                 and rec["mp"] < rec["entry"])
    # Mirror the live loss-cut positive-edge HOLD (signal_engine.evaluate_hold):
    # in the loss-cut regime (deep underwater <entry*frac, <loss_cut_time_s,
    # wrong-side >0.5xATR) with he>0 the model values the residual above the bid,
    # so live HOLDS rather than cutting — don't re-fire the scalp here.
    loss_cut_pos_hold = (
        wrong_side and abs(dist) > 0.5 and rec["he"] > 0
        and rec["entry"] is not None and rec["mp"] < rec["entry"] * _LOSS_CUT_FRACTION
        and rec["sr"] < _LOSS_CUT_TIME_S)
    if not whipsaw and not deep_loss and not loss_cut_pos_hold and fires:
        return rec["cf"]
    return rec["act"]


def score(recs: list[dict], artifact: dict) -> None:
    for r in recs:
        r["fire_boundary"] = boundary_fires(r)
        r["fire_model"] = model_fires(artifact, r)
        r["pnl_boundary"] = chosen_pnl(r, r["fire_boundary"])
        r["pnl_model"] = chosen_pnl(r, r["fire_model"])


def day_clustered_t(deltas: list[float]) -> float | None:
    n = len(deltas)
    if n < 2:
        return None
    mean = sum(deltas) / n
    sd = (sum((d - mean) ** 2 for d in deltas) / (n - 1)) ** 0.5
    return None if sd == 0 else mean / (sd / n ** 0.5)


def _fmt_t(t: float | None, n_days: int) -> str:
    return f"{t:+.2f}" if t is not None else f"n/a ({n_days} day{'s' * (n_days != 1)})"


def report(recs: list[dict]) -> None:
    days = sorted({r["day"] for r in recs})
    print("\nper-ET-day $ (branch-faithful arms on the matched pool):")
    print(f"{'day':>12} {'n':>5} {'boundary$':>10} {'model$':>10} {'delta$':>10}")
    deltas_all, deltas_itm = [], []
    for day in days:
        sel = [r for r in recs if r["day"] == day]
        pb = sum(r["pnl_boundary"] for r in sel)
        pm = sum(r["pnl_model"] for r in sel)
        print(f"{day:>12} {len(sel):>5} {pb:>+10.2f} {pm:>+10.2f} {pm - pb:>+10.2f}")
        deltas_all.append(pm - pb)
        itm_sel = [r for r in sel if r["mp"] >= 0.5]
        deltas_itm.append(sum(r["pnl_model"] - r["pnl_boundary"] for r in itm_sel))

    pb = sum(r["pnl_boundary"] for r in recs)
    pm = sum(r["pnl_model"] for r in recs)
    print(f"\nALL: n={len(recs)}  boundary {pb:+.2f}  model {pm:+.2f}  "
          f"delta {pm - pb:+.2f}  day-clustered t {_fmt_t(day_clustered_t(deltas_all), len(days))}")

    itm = [r for r in recs if r["mp"] >= 0.5]
    itm_b = sum(r["pnl_boundary"] for r in itm)
    itm_m = sum(r["pnl_model"] for r in itm)
    itm_delta = itm_m - itm_b
    print(f"ITM (mp>=0.5): n={len(itm)}  boundary {itm_b:+.2f}  model {itm_m:+.2f}  "
          f"delta {itm_delta:+.2f}  day-clustered t {_fmt_t(day_clustered_t(deltas_itm), len(days))}")
    print(f"fired: boundary {sum(r['fire_boundary'] for r in recs)}/{len(recs)}, "
          f"model {sum(r['fire_model'] for r in recs)}/{len(recs)}")

    print(f"\nKILL BAR (Phase 3): positive ITM delta over >= {KILL_BAR_DAYS} distinct ET days")
    if len(days) < KILL_BAR_DAYS:
        print(f"-> WAITING FOR DATA ({len(days)}/{KILL_BAR_DAYS} shadow days)")
    elif itm_delta > 0:
        print(f"-> PASS (ITM delta {itm_delta:+.2f} over {len(days)} days)")
    else:
        print(f"-> FAIL (ITM delta {itm_delta:+.2f} over {len(days)} days) — do not deploy; diagnose")


# ── selftest ──────────────────────────────────────────────────────────────────

def selftest() -> None:
    """In-memory fixture: betas all-zero -> p_up = 0.5; drift_up = +0.01 (bias only).
    Model fires iff own bid b satisfies b - 0.07*b*(1-b) > 0.5 (b=0.60 -> 0.5832
    fires; b=0.40 -> 0.3832 holds) AND drift_side <= 0 (Up blocked: +0.01;
    Down allowed: -0.01). he = ±0.5 makes the boundary decision unambiguous
    (effective threshold is always within [-0.30, +0.30])."""
    art = {"features": list(FEATURES), "norm_mu": [0.0] * 12, "norm_sd": [1.0] * 12,
           "beta_prob_up": [0.0] * 13, "beta_drift_up": [0.01] + [0.0] * 12}

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE window_paths (
        window_id TEXT, ts REAL, elapsed_s REAL,
        bid_up REAL, ask_up REAL, bid_down REAL, ask_down REAL,
        depth3_bid_up REAL, depth3_ask_up REAL, depth3_bid_down REAL,
        depth3_ask_down REAL, coinbase_price REAL, strike REAL, traded INTEGER)""")

    def mk(idx, kind, side, he, sr, mp, act, cf, loss_cut=False, dist=None,
           entry=None, book=None, strike=60010.0):
        wts = 1_000_000_200 + 300 * idx
        market_id = f"btc-updown-5m-{wts}"
        decision_ts = wts + (WINDOW_LEN_S - sr)
        if book is not None:  # (bid_up, ask_up, bid_down, ask_down); offset tests ±2s tol
            conn.execute("INSERT INTO window_paths VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (market_id, decision_ts + 0.8, WINDOW_LEN_S - sr, *book,
                          0.0, 0.0, 0.0, 0.0, 60000.0, strike, 0))
        return dict(kind=kind, he=he, sr=sr, mp=mp, thr=-0.10, loss_cut=loss_cut,
                    dist=dist, entry=entry, side=side, act=act, cf=cf,
                    market_id=market_id, window_ts=wts, decision_ts=decision_ts,
                    day=_et_day(decision_ts))

    recs = [
        # r1 scalp Down: boundary holds (he=+0.5) -> cf=+5; model fires (b=0.60,
        #    0.60-0.0168=0.5832 > 0.5, drift_down=-0.01) -> act=+2. ITM.
        mk(0, "scalp", "Down", +0.5, 120, 0.62, 2.0, 5.0,
           book=(0.40, 0.42, 0.60, 0.62)),
        # r2 scalp Up: boundary fires (he=-0.5) -> act=+1; model holds (b=0.40
        #    -> 0.3832 < 0.5, and drift_up=+0.01 > 0) -> cf=-3. ITM.
        mk(1, "scalp", "Up", -0.5, 120, 0.55, 1.0, -3.0,
           book=(0.40, 0.42, 0.58, 0.60)),
        # r3 scalp loss-cut: BOTH keep actual arm -> -4. OTM.
        mk(2, "scalp", "Up", -0.5, 60, 0.30, -4.0, -6.0, loss_cut=True,
           book=(0.30, 0.32, 0.68, 0.70)),
        # r4 hold Down: boundary holds -> act=+8; model fires (b=0.90 ->
        #    0.8937 > 0.5, drift_down=-0.01) -> worst-moment scalp cf=+6. ITM.
        mk(3, "hold", "Down", +0.5, 100, 0.90, 8.0, 6.0,
           book=(0.08, 0.10, 0.90, 0.92)),
        # r5 hold Down in the whipsaw cushion (dist=+0.3): BOTH keep act=-2
        #    even though both would fire (he=-0.5; model b=0.70 -> 0.6853 > 0.5).
        mk(4, "hold", "Down", -0.5, 80, 0.30, -2.0, -1.0, dist=+0.3,
           book=(0.28, 0.30, 0.70, 0.72)),
        # r6 scalp with NO window_paths row -> skipped (no_row).
        mk(5, "scalp", "Up", -0.5, 120, 0.55, 1.0, 1.0, book=None),
        # r7 scalp with NULL strike at the match -> skipped (null_fields).
        mk(6, "scalp", "Up", -0.5, 120, 0.55, 1.0, 1.0,
           book=(0.40, 0.42, 0.58, 0.60), strike=None),
    ]
    matched, skips = attach_rows(conn, recs)
    conn.close()
    assert len(matched) == 5 and skips == {"no_row": 1, "null_fields": 1}, (len(matched), skips)

    p_up, drift_up = model_predict(art, matched[0]["row"])
    assert abs(p_up - 0.5) < 1e-12 and abs(drift_up - 0.01) < 1e-12, (p_up, drift_up)

    score(matched, art)
    fires_b = [r["fire_boundary"] for r in matched]
    fires_m = [r["fire_model"] for r in matched]
    assert fires_b == [False, True, True, False, True], fires_b
    assert fires_m == [True, False, False, True, True], fires_m

    pb = sum(r["pnl_boundary"] for r in matched)
    pm = sum(r["pnl_model"] for r in matched)
    # boundary: 5 + 1 - 4 + 8 - 2 = +8 ; model: 2 - 3 - 4 + 6 - 2 = -1
    assert abs(pb - 8.0) < 1e-9 and abs(pm - (-1.0)) < 1e-9, (pb, pm)
    itm = [r for r in matched if r["mp"] >= 0.5]
    itm_b = sum(r["pnl_boundary"] for r in itm)
    itm_m = sum(r["pnl_model"] for r in itm)
    # ITM (r1, r2, r4): boundary 5 + 1 + 8 = +14 ; model 2 - 3 + 6 = +5
    assert abs(itm_b - 14.0) < 1e-9 and abs(itm_m - 5.0) < 1e-9, (itm_b, itm_m)

    # out-of-sample window filter: keep only windows starting strictly after the cutoff.
    cutoff = 1_000_000_200 + 300 * 2 + 1  # just after window idx 2 begins
    oos = [r["market_id"] for r in recs if r["window_ts"] > cutoff]
    assert oos == [f"btc-updown-5m-{1_000_000_200 + 300 * i}" for i in (3, 4, 5, 6)], oos

    # frozen baseline: snapshot once, then ignore the live artifact until --refreeze.
    import tempfile
    global ARTIFACT_PATH, SHADOW_ARTIFACT_PATH
    _saved = (ARTIFACT_PATH, SHADOW_ARTIFACT_PATH)
    try:
        with tempfile.TemporaryDirectory() as td:
            ARTIFACT_PATH = Path(td) / "exit_model.json"
            SHADOW_ARTIFACT_PATH = ARTIFACT_PATH.with_name("exit_model_shadow.json")
            assert shadow_artifact() == (None, False)  # nothing to freeze yet
            ARTIFACT_PATH.write_text(json.dumps({**art, "fitted_at": "2026-06-15T03:45:00+00:00"}),
                                     encoding="utf-8")
            a1, froze1 = shadow_artifact()
            assert froze1 and a1["fitted_at"] == "2026-06-15T03:45:00+00:00", (froze1, a1.get("fitted_at"))
            # live artifact advances (nightly refit) — the frozen baseline must NOT follow.
            ARTIFACT_PATH.write_text(json.dumps({**art, "fitted_at": "2026-06-16T03:45:00+00:00"}),
                                     encoding="utf-8")
            a2, froze2 = shadow_artifact()
            assert not froze2 and a2["fitted_at"] == "2026-06-15T03:45:00+00:00", a2.get("fitted_at")
            a3, froze3 = shadow_artifact(refreeze=True)
            assert froze3 and a3["fitted_at"] == "2026-06-16T03:45:00+00:00", a3.get("fitted_at")
    finally:
        ARTIFACT_PATH, SHADOW_ARTIFACT_PATH = _saved

    report(matched)
    # loss-cut positive-edge HOLD (06-17 fix) — mirrors signal_engine.evaluate_hold:
    # deep-underwater (<entry*0.65), <90s, wrong-side >0.5xATR record with he>0 must
    # HOLD (take act), not re-fire the scalp; he<=0 still re-fires (take cf).
    _lc = dict(kind="hold", he=+0.10, sr=60, mp=0.05, loss_cut=False,
               dist=-1.0, entry=0.80, side="Up", act=-9.0, cf=-9.5)
    assert chosen_pnl(_lc, True) == -9.0, "loss-cut he>0 must HOLD (take act)"
    assert chosen_pnl({**_lc, "he": -0.05}, True) == -9.5, "loss-cut he<=0 re-fires (take cf)"

    print("\nhand-check: boundary $ = 5+1-4+8-2 = +8.00 ; model $ = 2-3-4+6-2 = -1.00")
    print("            ITM delta = +5.00 - 14.00 = -9.00 ; loss-cut + whipsaw arms identical")
    print("SELFTEST PASS")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3 kill bar: exit-value model vs ExitBoundary")
    ap.add_argument("--db", default="polybot/db/polybot_paper.db")
    ap.add_argument("--selftest", action="store_true",
                    help="run the in-memory fixture instead of real data")
    ap.add_argument("--refreeze", action="store_true",
                    help="re-snapshot the frozen shadow baseline from the current live artifact")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return

    # Freeze the baseline BEFORE filtering: the kill bar must score a model trained
    # before the shadow window, reused unchanged across all 5 days (out-of-sample).
    artifact, froze_now = shadow_artifact(refreeze=args.refreeze)
    if artifact is None:
        print(f"NO ARTIFACT YET — neither {SHADOW_ARTIFACT_PATH.name} nor {ARTIFACT_PATH} present.")
        print("The nightly refit writes the live artifact once enough labeled windows exist "
              "(polybot/exit_model.py). Re-run on the first shadow day to freeze it.")
        return
    fitted_ts = _fitted_ts(artifact)
    if fitted_ts is None:
        print(f"ABORT — frozen baseline {SHADOW_ARTIFACT_PATH.name} has no parseable "
              "fitted_at; cannot guarantee an out-of-sample test.")
        return
    print(f"shadow baseline: {SHADOW_ARTIFACT_PATH.name}  fitted_at {artifact.get('fitted_at')}  "
          f"windows {artifact.get('n_windows')}  brier_oos {artifact.get('brier_oos')} "
          f"(market {artifact.get('brier_market_baseline')})")
    if froze_now:
        print(f"  FROZEN this run from the live artifact -> {SHADOW_ARTIFACT_PATH}")
        print(f"  NOTE: out-of-sample days accrue only for windows AFTER fitted_at "
              f"({artifact.get('fitted_at')}). Run this on the FIRST shadow day so all "
              f"{KILL_BAR_DAYS} ET days can count — a late first run silently shortens the "
              f"shadow, and --refreeze only moves the cutoff later, never earlier.")
    else:
        print(f"  reusing the frozen baseline -> {SHADOW_ARTIFACT_PATH}")

    recs, load_skips = build()
    print(f"\ncounterfactual records after recorder epoch "
          f"({RECORDER_EPOCH:%Y-%m-%d %H:%M} ET): {len(recs)}  skipped {load_skips}")

    # Out-of-sample filter: keep only records whose ENTIRE window starts after the
    # model's training cutoff. The model trains per-tick on each window's resolution
    # label, so any window overlapping training has leaked its label — drop it.
    pre = len(recs)
    recs = [r for r in recs if r["window_ts"] > fitted_ts]
    print(f"out-of-sample filter (window starts after fitted_at): kept {len(recs)}, "
          f"dropped {pre - len(recs)} in-sample")
    if not recs:
        print("WAITING FOR DATA — no out-of-sample counterfactual records after the frozen "
              "baseline's training cutoff yet (these accrue over the shadow days).")
        return

    opened = open_db_with_table(Path(args.db), "window_paths")
    if opened is None:
        print(f"WAITING FOR DATA — no window_paths table in {args.db} "
              "or its window_paths.db sidecar.")
        return
    conn, src = opened
    try:
        matched, match_skips = attach_rows(conn, recs)
    except sqlite3.OperationalError as e:
        print(f"WAITING FOR DATA — cannot read window_paths ({e}).")
        return
    finally:
        conn.close()
    print(f"window_paths source: {src}")
    print(f"matched to window_paths (±{MATCH_TOL_S:.0f}s): {len(matched)}  skipped {match_skips}")
    if not matched:
        print("WAITING FOR DATA — no decision moments matched a window_paths row yet.")
        return

    try:
        score(matched, artifact)
    except KeyError as e:
        print(f"artifact feature {e} is not derivable from window_paths — incompatible artifact.")
        return
    report(matched)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
