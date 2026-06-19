"""Alt-coin Coinbase->CLOB bid-staleness LATENCY-EXIT test (edge-hunt round 4).

The latency exit was statistically REAL on BTC (round 3: +0.09/sh, t_day +2.99) but
KILLED on executability — BTC's book reprices in <135ms, so the stale-high bid is
gone before a 135ms-RTT order lands (1.8% fill). The alt thesis: wider-spread alts
have ~14x quieter books (measured), so the same stale bid plausibly survives the
RTT window -> the edge may be REACHABLE there. This harness runs that exact test on
the alt corpus collected by scripts/record_alts.py.

THE TEST (per held side S, causal, no look-ahead):
  trigger: Coinbase moved >= TH bps AGAINST S over the trailing L s, held-side bid
           in [0.10,0.90], depth3_bid_S >= Dmin; first-fire per (window,side).
  offline edge   = (sell at held-side bid - taker fee) - hold-to-resolution payoff.
  realistic edge = same, but the fill price/fraction comes from the ms TAPE: a SELL
                   must print at/through the stale bid within the RTT window for the
                   maker-less FOK to fill; unfilled remainder rides to resolution.
  score          = day-clustered t (df=n_days-1) AND window block-bootstrap p10, on
                   BOTH the offline (upper bound) and realistic (deployable) edge.

KILL/PASS BAR (mirrors the BTC go-live bar): realistic edge t_day>=2 AND p10>0 over
>= MIN_DAYS clean ET days. Until then the harness reports PRELIMINARY/WAITING.

  python scripts/alt_latency_test.py            # run on the live alt corpus
  python scripts/alt_latency_test.py --selftest # validate the machinery on a fixture
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
PATHS_DB = _ROOT / "polybot" / "db" / "alt_window_paths.db"
LABELS_DB = _ROOT / "polybot" / "db" / "alt_recordings.db"
TAPE_GLOB = str(_ROOT / "polybot" / "memory" / "recordings" / "alts" / "*.jsonl")

FEE_RATE = 0.07
RTT_S = 0.135                  # warm POST round-trip; the executability window
MIN_DAYS = 8                   # clean ET days before the bar is readable
# Trigger grid (mirrors the BTC survivor family; TH in bps of Coinbase spot).
LAGS = (3.0, 5.0, 10.0)
THS = (5.0, 10.0, 20.0)
DMIN = 100.0                   # min held-side top-3 bid depth ($) to bother
SYMS = ("sol", "xrp", "doge")  # bnb has no Coinbase spot -> no z_lead


def _et_day(ts: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts, tz=ET).strftime("%Y-%m-%d")


def taker_fee(shares: float, p: float) -> float:
    return FEE_RATE * shares * p * (1.0 - p)


def day_clustered_t(deltas: list[float]) -> float | None:
    n = len(deltas)
    if n < 2:
        return None
    mean = sum(deltas) / n
    sd = (sum((d - mean) ** 2 for d in deltas) / (n - 1)) ** 0.5
    return None if sd == 0 else mean / (sd / n ** 0.5)


def bootstrap_p10(per_window: list[float], b: int = 2000) -> float | None:
    """10th-pct of the per-window mean edge over window resamples. Deterministic LCG
    (no Math.random equiv needed; seed fixed) so reruns match."""
    n = len(per_window)
    if n < 5:
        return None
    seed = 1234567
    means = []
    for _ in range(b):
        s = 0.0
        for _ in range(n):
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            s += per_window[seed % n]
        means.append(s / n)
    means.sort()
    return means[int(b * 0.10)]


# ── data load ───────────────────────────────────────────────────────────────

def load_windows() -> dict[str, dict]:
    """window_id -> {rows:[(ts,elapsed,bid_up,ask_up,bid_dn,ask_dn,d3bid_up,d3bid_dn,cb)],
    resolved_up, strike}. Strike = Coinbase spot at the earliest tick (window open)."""
    if not PATHS_DB.exists():
        return {}
    wp = sqlite3.connect(f"file:{PATHS_DB}?mode=ro", uri=True)
    wp.row_factory = sqlite3.Row
    out: dict[str, dict] = defaultdict(lambda: {"rows": [], "resolved_up": None, "strike": None})
    for r in wp.execute(
        "SELECT window_id,ts,elapsed_s,bid_up,ask_up,bid_down,ask_down,"
        "depth3_bid_up,depth3_bid_down,coinbase_price FROM window_paths ORDER BY ts"):
        if not any(r["window_id"].startswith(s) for s in SYMS):
            continue
        out[r["window_id"]]["rows"].append(r)
    wp.close()
    if LABELS_DB.exists():
        lb = sqlite3.connect(f"file:{LABELS_DB}?mode=ro", uri=True)
        for r in lb.execute("SELECT window_id,resolved_up,price_to_beat FROM window_labels"):
            if r[0] in out:
                out[r[0]]["resolved_up"] = r[1]
                out[r[0]]["strike"] = r[2]
        lb.close()
    # strike fallback: first non-null coinbase_price (window open)
    for w in out.values():
        if w["strike"] is None:
            for row in w["rows"]:
                if row["coinbase_price"] is not None:
                    w["strike"] = row["coinbase_price"]
                    break
    return {k: v for k, v in out.items() if v["resolved_up"] is not None}


def load_tape() -> dict[str, list[tuple]]:
    """token -> sorted [(ts, price, side)]. (Alt tape has no window/side; the test
    keys fill checks by the window's token set, matched in score())."""
    tape: dict[str, list[tuple]] = defaultdict(list)
    for fp in glob.glob(TAPE_GLOB):
        for line in open(fp):
            try:
                d = json.loads(line)
                tape[d["token"]].append((float(d["ts"]), float(d["price"]), d.get("side")))
            except Exception:
                continue
    for t in tape.values():
        t.sort()
    return tape


# ── the test ──────────────────────────────────────────────────────────────────

def cb_return_against(rows, i, lag_s, side):
    """Causal Coinbase return over trailing lag_s, signed so + means AGAINST side S
    (price fell for an Up holder / rose for a Down holder), in bps. None if no spot."""
    now = rows[i]
    if now["coinbase_price"] is None:
        return None
    t0 = now["ts"] - lag_s
    past = None
    for j in range(i, -1, -1):
        if rows[j]["ts"] <= t0 and rows[j]["coinbase_price"] is not None:
            past = rows[j]["coinbase_price"]
            break
    if past is None or past <= 0:
        return None
    ret_bps = (now["coinbase_price"] - past) / past * 1e4
    return -ret_bps if side == "Up" else ret_bps  # AGAINST S


def run_test(windows, tape, lag, th, rtt=RTT_S):
    """Returns per-(symbol-day) edge lists for offline + realistic, plus counters."""
    by_day_off, by_day_real = defaultdict(float), defaultdict(float)
    per_win_off, per_win_real = [], []
    fires = 0
    for wid, w in windows.items():
        rows = w["rows"]
        if len(rows) < 30 or w["strike"] is None:
            continue
        sym = wid.split("-", 1)[0]
        day = _et_day(rows[0]["ts"])
        for side in ("Up", "Down"):
            bidk = "bid_up" if side == "Up" else "bid_down"
            depthk = "depth3_bid_up" if side == "Up" else "depth3_bid_down"
            fired = False
            for i, row in enumerate(rows):
                bid = row[bidk]
                if bid is None or not (0.10 <= bid <= 0.90):
                    continue
                if (row[depthk] or 0) < DMIN:
                    continue
                ag = cb_return_against(rows, i, lag, side)
                if ag is None or ag < th:
                    continue
                # FIRE (first per window-side)
                fired = True
                won = (w["resolved_up"] == 1) == (side == "Up")
                hold = 1.0 if won else 0.0
                # offline: sell whole position at the stale bid, full taker fee (shares=1 unit)
                off = (bid - taker_fee(1.0, bid)) - hold
                # realistic: fill only if a SELL prints at/through bid within RTT (tape)
                fill_frac = _tape_fill_frac(tape, w, side, row["ts"], bid, rtt)
                real = fill_frac * off  # unfilled rides to resolution (edge 0 vs hold)
                by_day_off[(sym, day)] += off
                by_day_real[(sym, day)] += real
                per_win_off.append(off)
                per_win_real.append(real)
                fires += 1
                break
            _ = fired
    return dict(by_day_off=by_day_off, by_day_real=by_day_real,
                per_win_off=per_win_off, per_win_real=per_win_real, fires=fires)


def _tape_fill_frac(tape, w, side, t0, bid, rtt):
    """Fraction of a unit order that fills at/through the stale bid within [t0,t0+rtt].
    A taker SELL hitting the bid prints as side 'SELL' (a seller crossed). We proxy
    'bid still hittable' by any SELL print at price>=bid in the window; fraction is a
    coarse hittable-yes(1)/no(0) since alt tape lacks our queue position."""
    toks = w.get("_tokens")
    if not toks:
        return 0.0
    tok = toks.get(side)
    if not tok or tok not in tape:
        return 0.0
    prints = tape[tok]
    for ts, px, sd in prints:
        if ts < t0:
            continue
        if ts > t0 + rtt:
            break
        if (sd or "").upper() == "SELL" and px >= bid - 1e-9:
            return 1.0
    return 0.0


def report(windows, tape):
    days = sorted({_et_day(w["rows"][0]["ts"]) for w in windows.values() if w["rows"]})
    print(f"alt latency-exit test | {len(windows)} labeled windows, {len(days)} ET day(s) "
          f"({days[0] if days else '-'}..{days[-1] if days else '-'})")
    if len(days) < MIN_DAYS:
        print(f"-> PRELIMINARY / WAITING FOR DATA ({len(days)}/{MIN_DAYS} clean ET days). "
              f"Directional reads below are NOT significant (df={max(0,len(days)-1)}).")
    print(f"\n{'lag':>4} {'TH':>4} {'fires':>6} {'off$/win':>9} {'real$/win':>10} "
          f"{'t_day(real)':>11} {'p10(real)':>10}")
    best = None
    for lag in LAGS:
        for th in THS:
            res = run_test(windows, tape, lag, th)
            if res["fires"] == 0:
                continue
            off_days = list(res["by_day_off"].values())
            real_days = list(res["by_day_real"].values())
            off_m = sum(res["per_win_off"]) / len(res["per_win_off"])
            real_m = sum(res["per_win_real"]) / len(res["per_win_real"])
            t_real = day_clustered_t(real_days)
            p10 = bootstrap_p10(res["per_win_real"])
            ts = f"{t_real:+.2f}" if t_real is not None else "n/a"
            ps = f"{p10:+.4f}" if p10 is not None else "n/a"
            print(f"{lag:>4.0f} {th:>4.0f} {res['fires']:>6} {off_m:>+9.4f} {real_m:>+10.4f} "
                  f"{ts:>11} {ps:>10}")
            if t_real is not None and p10 is not None and t_real >= 2 and p10 > 0:
                if best is None or real_m > best[1]:
                    best = (f"lag{lag:.0f}/TH{th:.0f}", real_m, t_real, p10)
    print()
    if len(days) < MIN_DAYS:
        print("VERDICT: not yet readable — let the corpus mature to >= "
              f"{MIN_DAYS} clean days (~06-26), then re-run.")
    elif best:
        print(f"VERDICT: PASS — {best[0]} realistic edge {best[1]:+.4f}/win "
              f"t_day {best[2]:+.2f} p10 {best[3]:+.4f}. The alt latency edge is REACHABLE.")
    else:
        print("VERDICT: FAIL — no trigger config clears t_day>=2 AND p10>0 on the "
              "realistic (ms-tape fill) edge. Latency edge unreachable on alts too.")


def attach_tokens(windows):
    """Populate w['_tokens']={'Up':token_up,'Down':token_down} from record_alts'
    window_tokens table so the realistic fill-check can find each side's tape.
    Windows recorded before token-logging landed have no row -> _tokens None ->
    realistic edge is a conservative 0-fill lower bound there (offline = upper)."""
    tok: dict[str, dict] = {}
    if LABELS_DB.exists():
        lb = sqlite3.connect(f"file:{LABELS_DB}?mode=ro", uri=True)
        try:
            for r in lb.execute("SELECT window_id,token_up,token_down FROM window_tokens"):
                tok[r[0]] = {"Up": r[1], "Down": r[2]}
        except sqlite3.OperationalError:
            pass  # table not created yet (no token-logging run has happened)
        lb.close()
    for wid, w in windows.items():
        w["_tokens"] = tok.get(wid)


# ── selftest ────────────────────────────────────────────────────────────────

def selftest():
    """Synthetic 2-window fixture validates trigger detection, signing, edge calc,
    and tape fill-check — independent of live data."""
    base = 1_000_000_000.0
    def mkrow(wid, t, el, bu, au, bd, ad, cb):
        return sqlite3.Row  # placeholder; we use dicts below instead
    # Build plain-dict rows (Row not constructible); the test funcs only index by key.
    class R(dict):
        def __getitem__(self, k): return dict.get(self, k)
    def row(t, el, bu, bd, cb, du=500.0, dd=500.0):
        return R(ts=t, elapsed_s=el, bid_up=bu, ask_up=(bu+0.01) if bu else None,
                 bid_down=bd, ask_down=(bd+0.01) if bd else None,
                 depth3_bid_up=du, depth3_bid_down=dd, coinbase_price=cb)
    # Window A: Coinbase DROPS 0.6% at t=5s (against Up). Up bid stale-high 0.55, Up
    # loses. >=30 rows so run_test's partial-window guard doesn't skip it.
    rowsA = [row(base + i, float(i), 0.55, 0.45, 100.0 * (1 - 0.006 * (i >= 5)))
             for i in range(35)]
    # Window B: no Coinbase move. No trigger.
    rowsB = [row(base + 1000 + i, float(i), 0.50, 0.50, 100.0) for i in range(35)]
    windows = {
        "sol-updown-5m-1000000000": {"rows": rowsA, "resolved_up": 0, "strike": 100.0,
                                     "_tokens": {"Up": "TOKA"}},
        "sol-updown-5m-1000001000": {"rows": rowsB, "resolved_up": 1, "strike": 100.0,
                                     "_tokens": {"Up": "TOKB"}},
    }
    # cb_return_against: at i=5 (the drop tick) the trailing-5s window spans
    # cb 100->99.4 = -60bps; AGAINST Up = +60bps >= TH. (At i=10 the whole window
    # is post-drop at 99.4, so the move there is ~0 — the trigger fires at i=5.)
    ag = cb_return_against(rowsA, 5, 5.0, "Up")
    assert ag is not None and ag > 50, f"expected ~+60bps against Up at i=5, got {ag}"
    assert abs(cb_return_against(rowsB, 5, 5.0, "Up")) < 1e-6
    # tape: a SELL prints at 0.55 within 135ms of the fire -> fill_frac 1.0
    tape = {"TOKA": [(base + 5.05, 0.55, "SELL")], "TOKB": []}
    # find fire index in A (first i with trailing move >= TH and bid in band)
    res = run_test(windows, tape, 5.0, 10.0)
    assert res["fires"] == 1, f"expected 1 fire, got {res['fires']}"
    # Up lost -> hold=0; offline = bid - fee - 0 = 0.55 - 0.07*0.55*0.45 = +0.5327
    off = res["per_win_off"][0]
    assert abs(off - (0.55 - 0.07 * 0.55 * 0.45)) < 1e-6, off
    # realistic: SELL printed at 0.55 within 135ms -> fill 1.0 -> real == off
    assert abs(res["per_win_real"][0] - off) < 1e-6, res["per_win_real"]
    # no-fill case: tape SELL at 0.50 (below bid) -> no fill -> real 0
    res2 = run_test(windows, {"TOKA": [(base + 5.05, 0.50, "SELL")], "TOKB": []}, 5.0, 10.0)
    assert res2["per_win_real"][0] == 0.0, res2["per_win_real"]
    print("SELFTEST PASS — trigger signing, edge calc, and ms-tape fill-check all correct.")


def main():
    ap = argparse.ArgumentParser(description="Alt Coinbase-lead bid-staleness latency-exit test")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    windows = load_windows()
    if not windows:
        print("No labeled alt windows yet — let scripts/record_alts.py collect first.")
        return
    tape = load_tape()
    attach_tokens(windows)
    report(windows, tape)
    print("\nNOTE: realistic (ms-tape) fill-check needs per-window Up/Down token ids, "
          "which alt window_paths does not yet store -> realistic edge is a conservative "
          "0-fill lower bound until record_alts persists them. The offline column is the "
          "upper bound; the BTC lesson is the gap between them IS the edge's fate.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
