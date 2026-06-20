"""Alt-coin Coinbase->CLOB bid-staleness LATENCY-EXIT test (edge-hunt round 4).

The latency exit was statistically REAL on BTC (round 3: +0.09/sh, t_day +2.99) but
KILLED on executability — BTC's book reprices in <135ms, so the stale-high bid is
gone before a 135ms-RTT order lands (1.8% fill). The alt thesis: wider-spread alts
have ~14x quieter books (measured), so the same stale bid plausibly survives the
RTT window -> the edge may be REACHABLE there, AND colocation (sub-15ms RTT) may
make it reachable where 135ms cannot. This harness runs that exact test on the
corpus collected by scripts/record_alts.py.

THE TEST (per held side S, causal, no look-ahead):
  trigger: Coinbase moved >= TH bps AGAINST S over the trailing L s, held-side bid
           in [0.10,0.90], depth3_bid_S >= Dmin; first-fire per (window,side).
  offline edge   = (sell at held-side bid - taker fee) - hold-to-resolution payoff
                   (the zero-latency ceiling).
  realistic edge = offline x fill_frac, where fill_frac is a POINT-IN-TIME check: a
                   FOK SELL landing at t0+RTT fills iff the stale bid is still >= its
                   level at arrival. Reprice timing is pinned by the ms TAPE (first
                   trade through the bid) and, failing a print, bounded by the next
                   1 Hz BBO sample. Sub-second quote moves 1 Hz cannot time are
                   reported as a [pess..opt] bracket, not a fake point estimate.
                   Evaluated at the current RTT (135ms) AND the post-colo RTT (15ms)
                   so the bar can estimate whether colocation makes it reachable.
  score          = day-clustered t (df=n_days-1) AND window block-bootstrap p10 on the
                   realistic edge at the colo RTT (the deployable scenario).

KILL/PASS BAR (mirrors the BTC go-live bar): realistic edge at the colo RTT
t_day>=2 AND p10>0 over >= MIN_DAYS clean ET days. Until then -> PRELIMINARY.

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
RTT_S = 0.135                  # current warm POST round-trip (Toronto-VPN path)
RTT_COLO = 0.015               # post-colocation target (Dublin / AWS eu-west-1)
ASSUMPTIONS = ("pess", "uniform", "opt")  # silent-reprice timing bracket
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
    """window_id -> {rows:[Row(ts,elapsed,bid_up,ask_up,bid_dn,ask_dn,d3bid_up,
    d3bid_dn,cb)], resolved_up, strike}. Strike = Coinbase spot at window open."""
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
    """token -> sorted [(ts, price, side)]."""
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


def _next_sample_bid(rows, i, bidk, t0):
    """Held-side bid at the first 1 Hz sample after row i (and its dt from t0)."""
    for j in range(i + 1, len(rows)):
        b = rows[j][bidk]
        if b is not None:
            return b, rows[j]["ts"] - t0
    return None, None


def _reprice_after(tape, w, side, t0, B, search_end):
    """Earliest time (s after t0) a held-side trade prints STRICTLY below the stale
    bid B -> the book traded down through your level. None if no such print. (A print
    AT B means the bid is still there and is NOT a reprice.)"""
    toks = w.get("_tokens")
    tok = toks.get(side) if toks else None
    if not tok or tok not in tape:
        return None
    for ts, px, sd in tape[tok]:
        if ts <= t0:
            continue
        if ts - t0 > search_end:
            break
        if px < B - 1e-9:
            return ts - t0
    return None


def classify_fire(rows, i, tape, w, side, t0, B):
    """How the stale held-side bid B decays after the trigger:
      'stable' -> bid still >= B at the next 1 Hz sample (survives any plausible RTT);
      'trade'  -> a trade printed through B at sub-second time tau (tape-pinned);
      'silent' -> bid gone by the next sample but no trade pinned WHEN (gap-bracketed,
                  since 1 Hz BBO cannot resolve a sub-second quote cancel/re-quote)."""
    bidk = "bid_up" if side == "Up" else "bid_down"
    B_next, dt_next = _next_sample_bid(rows, i, bidk, t0)
    if B_next is not None and B_next >= B - 1e-9:
        return {"kind": "stable", "tau": None, "gap": None}
    search_end = dt_next if dt_next is not None else 2.0
    tau = _reprice_after(tape, w, side, t0, B, search_end)
    if tau is not None:
        return {"kind": "trade", "tau": tau, "gap": None}
    return {"kind": "silent", "tau": None, "gap": dt_next if dt_next is not None else 1.0}


def fill_frac(fire, rtt, assumption):
    """Fraction filled if a FOK SELL lands at t0+rtt. Monotone in speed: a lower rtt
    always fills >= a higher rtt (the correct direction for the colo question)."""
    k = fire["kind"]
    if k == "stable":
        return 1.0
    if k == "trade":
        return 1.0 if rtt < fire["tau"] else 0.0
    gap = fire["gap"] or 1.0           # silent reprice somewhere in (t0, t0+gap]
    if assumption == "pess":
        return 0.0
    if assumption == "opt":
        return 1.0 if rtt < gap else 0.0
    return max(0.0, 1.0 - rtt / gap)   # uniform: P(reprice time > rtt)


def run_test(windows, tape, lag, th):
    """Collect per-(window,side) first-fire records: offline edge + reprice class.
    Realistic edge for any (rtt, assumption) is derived from these via summarize()."""
    fires = []
    for wid, w in windows.items():
        rows = w["rows"]
        if len(rows) < 30 or w["strike"] is None:
            continue
        sym = wid.split("-", 1)[0]
        day = _et_day(rows[0]["ts"])
        for side in ("Up", "Down"):
            bidk = "bid_up" if side == "Up" else "bid_down"
            depthk = "depth3_bid_up" if side == "Up" else "depth3_bid_down"
            for i, row in enumerate(rows):
                bid = row[bidk]
                if bid is None or not (0.10 <= bid <= 0.90):
                    continue
                if (row[depthk] or 0) < DMIN:
                    continue
                ag = cb_return_against(rows, i, lag, side)
                if ag is None or ag < th:
                    continue
                won = (w["resolved_up"] == 1) == (side == "Up")
                off = (bid - taker_fee(1.0, bid)) - (1.0 if won else 0.0)
                cls = classify_fire(rows, i, tape, w, side, row["ts"], bid)
                cls.update(sym=sym, day=day, off=off)
                fires.append(cls)
                break
    return fires


def summarize(fires, rtt, assumption):
    """Realistic edge stats for fires at a given (rtt, assumption)."""
    if not fires:
        return None
    by_day = defaultdict(float)
    per_win_real, per_win_off = [], []
    for f in fires:
        real = fill_frac(f, rtt, assumption) * f["off"]
        by_day[(f["sym"], f["day"])] += real
        per_win_real.append(real)
        per_win_off.append(f["off"])
    return dict(
        off_m=sum(per_win_off) / len(per_win_off),
        real_m=sum(per_win_real) / len(per_win_real),
        t_day=day_clustered_t(list(by_day.values())),
        p10=bootstrap_p10(per_win_real),
        fires=len(fires))


# ── reporting ──────────────────────────────────────────────────────────────────

def _print_regime(windows, tape):
    """Where do the reprices land? 'rescued-by-colo' = stale bids that survive past
    15ms but die before 135ms = exactly the band colocation converts from miss->fill."""
    fires = run_test(windows, tape, 3.0, 10.0) or run_test(windows, tape, 5.0, 10.0)
    if not fires:
        print("reprice regime: no strong-signal fires yet.")
        return
    n = len(fires)
    stable = sum(1 for f in fires if f["kind"] == "stable")
    trade = [f for f in fires if f["kind"] == "trade"]
    silent = sum(1 for f in fires if f["kind"] == "silent")
    band = sum(1 for f in trade if RTT_COLO <= f["tau"] < RTT_S)
    sub15 = sum(1 for f in trade if f["tau"] < RTT_COLO)
    survive135 = sum(1 for f in trade if f["tau"] >= RTT_S)
    print(f"reprice regime (strong-signal trigger, n={n}):")
    print(f"  stable (bid survives > ~1s, fills at any RTT) = {stable}")
    print(f"  trade-pinned = {len(trade)}  [gone<15ms={sub15} | RESCUED-BY-COLO(15-135ms)={band} "
          f"| survive>135ms={survive135}]")
    print(f"  silent (sub-second quote move, 1 Hz can't time -> bracketed) = {silent}")


def _per_symbol(fires):
    bysym = defaultdict(list)
    for f in fires:
        bysym[f["sym"]].append(f)
    out = {}
    for sym, fs in bysym.items():
        out[sym] = (
            len(fs),
            sum(x["off"] for x in fs) / len(fs),
            sum(fill_frac(x, RTT_S, "uniform") * x["off"] for x in fs) / len(fs),
            sum(fill_frac(x, RTT_COLO, "uniform") * x["off"] for x in fs) / len(fs),
            sum(1 for x in fs if x["kind"] == "stable"))
    return out


def _print_per_symbol(windows, tape):
    """The wide-book tell: is the offline edge in DOGE (wide ~6.8c spread — the thesis
    candidate) or only in SOL (tight ~1.2c — where a real edge is least credible)?
    Edge in the WIDE book supports the thesis; edge only in tight SOL = likely noise."""
    print("\nper-symbol tell (spread: sol~1.2c xrp~1.3c doge~6.8c) — want the edge in the "
          "WIDE book (doge); edge only in tight sol = likely day-noise:")
    for lag, th in ((3.0, 5.0), (3.0, 10.0), (5.0, 10.0)):
        fires = run_test(windows, tape, lag, th)
        if not fires:
            continue
        ps = _per_symbol(fires)
        print(f"  lag{lag:.0f}/TH{th:.0f} ({len(fires)} fires):")
        for sym in SYMS:
            if sym not in ps:
                print(f"    {sym:4}: 0 fires")
                continue
            n, off, r135, r15, stable = ps[sym]
            print(f"    {sym:4}: n={n:3d}  offline={off:>+8.4f}  real@135={r135:>+8.4f}  "
                  f"real@15={r15:>+8.4f}  stable={stable}/{n}")


def report(windows, tape):
    days = sorted({_et_day(w["rows"][0]["ts"]) for w in windows.values() if w["rows"]})
    print(f"alt latency-exit test | {len(windows)} labeled windows, {len(days)} ET day(s) "
          f"({days[0] if days else '-'}..{days[-1] if days else '-'})")
    if len(days) < MIN_DAYS:
        print(f"-> PRELIMINARY / WAITING FOR DATA ({len(days)}/{MIN_DAYS} clean ET days). "
              f"Directional reads below are NOT significant (df={max(0,len(days)-1)}).")
    print(f"\noffline = zero-latency ceiling | real@135 = today | real@15 = post-colo "
          f"(uniform); 15ms[pess..opt] = sub-second bracket")
    print(f"\n{'lag':>4} {'TH':>4} {'fires':>6} {'offline':>8} {'real@135':>9} "
          f"{'real@15':>8} {'15ms[pess..opt]':>20} {'t_day@15':>9}")
    best = None
    for lag in LAGS:
        for th in THS:
            fires = run_test(windows, tape, lag, th)
            if not fires:
                continue
            cur = summarize(fires, RTT_S, "uniform")
            colo = summarize(fires, RTT_COLO, "uniform")
            lo = summarize(fires, RTT_COLO, "pess")
            hi = summarize(fires, RTT_COLO, "opt")
            ts = f"{colo['t_day']:+.2f}" if colo["t_day"] is not None else "n/a"
            bracket = f"[{lo['real_m']:+.4f}..{hi['real_m']:+.4f}]"
            print(f"{lag:>4.0f} {th:>4.0f} {cur['fires']:>6} {cur['off_m']:>+8.4f} "
                  f"{cur['real_m']:>+9.4f} {colo['real_m']:>+8.4f} {bracket:>20} {ts:>9}")
            if (colo["t_day"] is not None and colo["p10"] is not None
                    and colo["t_day"] >= 2 and colo["p10"] > 0):
                if best is None or colo["real_m"] > best[1]:
                    best = (f"lag{lag:.0f}/TH{th:.0f}", colo["real_m"], colo["t_day"], colo["p10"])
    print()
    _print_per_symbol(windows, tape)
    _print_regime(windows, tape)
    if len(days) < MIN_DAYS:
        print(f"\nVERDICT: not yet readable — mature to >= {MIN_DAYS} clean days (~06-26), "
              "then re-run. Watch whether 'real@15' lifts toward 'offline' (colo helps) "
              "and whether DOGE's offline > SOL/XRP's (wide-book edge is real).")
    elif best:
        print(f"\nVERDICT: PASS @colo — {best[0]} realistic edge {best[1]:+.4f}/win "
              f"t_day {best[2]:+.2f} p10 {best[3]:+.4f} at 15ms RTT. "
              "Latency edge REACHABLE post-colocation (but check it FAILS at 135ms — "
              "that's what justifies the colo).")
    else:
        print("\nVERDICT: FAIL — no config clears t_day>=2 AND p10>0 even at 15ms colo RTT. "
              "Latency edge unreachable on alts even colocated.")


def attach_tokens(windows):
    """Populate w['_tokens']={'Up':token_up,'Down':token_down} from record_alts'
    window_tokens table so the fill-check can find each side's tape. Windows recorded
    before token-logging landed have no row -> _tokens None -> their non-'stable'
    fires can't be tape-pinned (fall to the silent bracket)."""
    tok: dict[str, dict] = {}
    if LABELS_DB.exists():
        lb = sqlite3.connect(f"file:{LABELS_DB}?mode=ro", uri=True)
        try:
            for r in lb.execute("SELECT window_id,token_up,token_down FROM window_tokens"):
                tok[r[0]] = {"Up": r[1], "Down": r[2]}
        except sqlite3.OperationalError:
            pass
        lb.close()
    for wid, w in windows.items():
        w["_tokens"] = tok.get(wid)


# ── selftest ────────────────────────────────────────────────────────────────

def selftest():
    """Synthetic fixtures validate trigger detection/signing, offline edge, the
    reprice classification, RTT monotonicity, and the silent bracket."""
    base = 1_000_000_000.0

    class R(dict):
        def __getitem__(self, k): return dict.get(self, k)

    def row(t, el, bu, bd, cb, du=500.0, dd=500.0):
        return R(ts=t, elapsed_s=el, bid_up=bu, ask_up=(bu + 0.01) if bu else None,
                 bid_down=bd, ask_down=(bd + 0.01) if bd else None,
                 depth3_bid_up=du, depth3_bid_down=dd, coinbase_price=cb)

    # Coinbase DROPS 0.6% at t=5s (against Up); the trigger fires at i=5. Up loses.
    def cb(i):
        return 100.0 * (1 - 0.006 * (i >= 5))
    # --- signing sanity ---
    rows_flat = [row(base + i, float(i), 0.55, 0.45, cb(i)) for i in range(35)]
    assert cb_return_against(rows_flat, 5, 5.0, "Up") > 50, "expected ~+60bps against Up at i=5"
    rows_none = [row(base + 1000 + i, float(i), 0.50, 0.50, 100.0) for i in range(35)]
    assert abs(cb_return_against(rows_none, 5, 5.0, "Up")) < 1e-6

    # CASE stable: Up bid stays 0.55 through the next sample -> fills at any RTT.
    w_stable = {"sol-updown-5m-1": {"rows": rows_flat, "resolved_up": 0, "strike": 100.0,
                                    "_tokens": {"Up": "TOKA"}}}
    fs = run_test(w_stable, {"TOKA": []}, 5.0, 10.0)
    assert len(fs) == 1 and fs[0]["kind"] == "stable", fs
    off = 0.55 - 0.07 * 0.55 * 0.45            # Up lost -> hold 0
    assert abs(fs[0]["off"] - off) < 1e-6, fs[0]
    assert fill_frac(fs[0], RTT_S, "pess") == 1.0, "stable fills at any RTT"

    # CASE trade-pinned: bid drops to 0.40 at the next sample; a SELL prints THROUGH
    # the stale bid (0.54 < 0.55) 50ms after the fire -> tau=0.05.
    rows_drop = [row(base + i, float(i), 0.55 if i <= 5 else 0.40, 0.45, cb(i)) for i in range(35)]
    w_drop = {"sol-updown-5m-1": {"rows": rows_drop, "resolved_up": 0, "strike": 100.0,
                                  "_tokens": {"Up": "TOKA"}}}
    ft = run_test(w_drop, {"TOKA": [(base + 5.05, 0.54, "SELL")]}, 5.0, 10.0)
    assert len(ft) == 1 and ft[0]["kind"] == "trade" and abs(ft[0]["tau"] - 0.05) < 1e-6, ft
    # MONOTONICITY: fills at 15ms (rtt<tau) but MISSES at 135ms (rtt>tau) -> colo rescues it
    assert fill_frac(ft[0], RTT_COLO, "uniform") == 1.0, "should fill at 15ms"
    assert fill_frac(ft[0], RTT_S, "uniform") == 0.0, "should miss at 135ms"

    # CASE silent: bid drops at the next sample, NO trade through it -> gap bracket.
    fz = run_test(w_drop, {"TOKA": []}, 5.0, 10.0)
    assert len(fz) == 1 and fz[0]["kind"] == "silent", fz
    fp = fill_frac(fz[0], RTT_COLO, "pess")
    fu = fill_frac(fz[0], RTT_COLO, "uniform")
    fo = fill_frac(fz[0], RTT_COLO, "opt")
    assert fp <= fu <= fo and fp == 0.0 and fo == 1.0, (fp, fu, fo)
    # and 15ms uniform fills more than 135ms uniform (monotone) on the silent case too
    assert fill_frac(fz[0], RTT_COLO, "uniform") > fill_frac(fz[0], RTT_S, "uniform"), "monotone"
    print("SELFTEST PASS — trigger signing, offline edge, reprice classification, "
          "RTT monotonicity (colo fills >= current), and silent bracket all correct.")


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
    print("\nNOTE: the sub-15ms estimate is bracketed, not exact — 1 Hz BBO cannot time a "
          "sub-second quote cancel/re-quote, so 'silent' fires span [pess..opt]. The "
          "'trade-pinned' fires ARE ms-exact (real tape), and the regime line shows how "
          "many of them sit in the 15-135ms band colocation would convert from miss to fill.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
