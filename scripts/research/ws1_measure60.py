"""WS1 rebuilt for the 60s resolution rule (live since 08-14 00:00 UTC).

Projection: proj60(t) = w*A60 + (1-w)*spot, w = (t-(close-60))/60, zone 60s.
Targets: served final_price on 08-14+ (real 60s-rule finals); synthetic
a60rx(close) on 08-07..13 (validated: on 08-14+ served-vs-a60rx med $0.013-0.08).
Estimators: plain / bz / cb / kl bridges (same spot-delta logic as live).
Per-sample flags: thirty-stream stall veto (proxy for relay stalls), raw
delivery-hole metrics (max rx gap + report count in the span).

Outputs data/ws1_errors60.csv + fitted p99.5/max knots per estimator/policy
+ the pre-registered bar checks.
"""
import csv
import gzip
import json
import math
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
RULE_TS = 1786665600            # 2026-08-14 00:00 UTC — first 60s-rule window
K_GRID = [1.1, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 12.0, 15.0,
          20.0, 25.0, 29.0, 35.0, 40.0, 45.0, 50.0, 55.0, 58.0]
KNOTS = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 29.0,
         35.0, 40.0, 45.0, 50.0, 55.0, 58.0]
SPOT_STALE_S = 3.0
FROZEN_S = 20.0
FROZEN_RAW_MOVE = 2.0
HORIZON = 60.0


def running_avg(recs, start, end):
    seed = None
    pts = []
    for rx, p in recs:
        if rx <= start:
            seed = p
        elif rx <= end:
            pts.append((rx, p))
    if seed is None:
        if not pts or pts[0][0] > start + 2.0:
            return None
        seed = pts[0][1]
    acc, prev_t, prev_p = 0.0, start, seed
    for rx, p in pts:
        acc += prev_p * (rx - prev_t)
        prev_t, prev_p = rx, p
    acc += prev_p * (end - prev_t)
    return acc / (end - start) if end > start else prev_p


def bridge_delta(ring, raw_ts, t):
    live = [e for e in ring if e[0] <= t]
    if not live:
        return None
    newest_rx, newest_ts, newest_px = live[-1]
    live = [e for e in live if e[1] >= newest_ts - 10.0]
    if newest_ts <= raw_ts:
        return 0.0
    anchor = None
    for _rx, ts, px in live:
        if ts <= raw_ts:
            anchor = px
        else:
            break
    if anchor is None:
        return 0.0
    return newest_px - anchor


def twap_frozen_at(trecs, l_rx, t):
    vals = [(rx, p) for rx, _ts, p in trecs if rx <= t]
    if not vals:
        return False
    v = vals[-1][1]
    since = vals[-1][0]
    for rx, p in reversed(vals):
        if p == v:
            since = rx
        else:
            break
    if t - since < FROZEN_S:
        return False
    spanned = [p for rx, p in l_rx if since <= rx <= t]
    if len(spanned) < 2:
        return False
    return (max(spanned) - min(spanned)) >= FROZEN_RAW_MOVE


def load_klines():
    ts_arr = []
    px_arr = []
    rows = []
    for name in ("binance_1s.csv", "binance_1s_late.csv"):
        p = DATA / name
        if not p.exists():
            continue
        with open(p) as f:
            next(f)
            for line in f:
                a, b = line.rstrip("\n").split(",")
                rows.append((int(a), float(b)))
    rows.sort()
    for t, p in rows:
        ts_arr.append(t)
        px_arr.append(p)
    return ts_arr, px_arr


def main():
    windows = []
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            windows.append(json.loads(line))
    lags = sorted(rx - ts for w in windows for rx, ts, _p in w["bz"] if rx and ts)
    bz_lag = lags[len(lags) // 2] if lags else 0.45
    kl_ts, kl_px = load_klines()
    print(f"{len(windows)} windows, bz relay lag p50 {bz_lag:.3f}s, "
          f"{len(kl_ts)} kline rows")

    rows = []
    n_syn = n_real = 0
    for wd in windows:
        ep = wd["ep"]
        close = ep + 300
        t0 = close - HORIZON
        strike = wd["strike"]
        l = sorted(wd["l"])
        l_rx = [(rx, p) for rx, _ts, p in l]
        trecs = sorted(wd.get("t") or [])
        # target: real 60s-rule final after the flip, synthetic a60rx before
        if ep >= RULE_TS:
            final = wd["final"]
            src = "real"
            n_real += 1
        else:
            final = running_avg(l_rx, t0, close)
            src = "syn"
            n_syn += 1
        if not final or not strike:
            continue
        # ALSO keep the served final for the synthetic-validation columns
        served = wd["final"]
        bz = wd["bz"]
        cb = wd["cb"]
        kl_ring = None
        if kl_ts:
            i0 = bisect_right(kl_ts, ep + 195)
            i1 = bisect_right(kl_ts, ep + 306)
            if i1 - i0 >= 80:
                kl_ring = [(S + 1 + bz_lag, S + 1.0, px)
                           for S, px in zip(kl_ts[i0:i1], kl_px[i0:i1])]
        for k in K_GRID:
            t = close - k
            i = bisect_right(l_rx, (t, float("inf"))) - 1
            if i < 0:
                continue
            rx_s, p_s = l_rx[i]
            raw_ts_s = l[i][1]
            if t - rx_s > SPOT_STALE_S:
                continue
            A = running_avg(l_rx, t0, t)
            if A is None:
                continue
            w = (t - t0) / HORIZON
            # boundary-inclusive: a raw outage covering the START of the span
            # (reports resuming late) must read as a hole too
            span = [t0] + [rx for rx, _p in l_rx if t0 <= rx <= t] + [t]
            gap = max(b - a for a, b in zip(span, span[1:]))
            out = {"ep": ep, "k": k, "final": final, "strike": strike,
                   "src": src, "served": served,
                   "up": wd["up"], "w": round(w, 4),
                   "veto": int(twap_frozen_at(trecs, l_rx, t)),
                   "gap": round(gap, 2), "nrep": len(span),
                   "plain": w * A + (1 - w) * p_s}
            for name, ring in (("bz", bz), ("cb", cb), ("kl", kl_ring)):
                if not ring:
                    continue
                d = bridge_delta(ring, raw_ts_s, t)
                if d is not None:
                    out[name] = w * A + (1 - w) * (p_s + d)
            rows.append(out)

    with open(DATA / "ws1_errors60.csv", "w", newline="") as f:
        cols = ["ep", "k", "final", "strike", "src", "served", "up", "w",
                "veto", "gap", "nrep", "plain", "bz", "cb", "kl"]
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        wr.writerows(rows)
    print(f"{len(rows)} samples ({n_real} real-final windows, {n_syn} synthetic)")

    def p995(xs):
        if len(xs) < 50:
            return None
        return xs[min(math.ceil(0.995 * len(xs)) - 1, len(xs) - 1)]

    def table(name, cond, label):
        print(f"\n=== |error| {label} ===")
        res = {}
        for k in KNOTS:
            xs = sorted(abs(r["final"] - r[name]) for r in rows
                        if r["k"] == k and name in r and cond(r))
            if not xs:
                continue
            q = p995(xs)
            res[k] = (len(xs), xs[len(xs) // 2], q, xs[-1])
            print(f"k={k:4.1f}  n={len(xs):5d}  med={xs[len(xs) // 2]:7.3f}  "
                  f"p99={xs[min(math.ceil(0.99 * len(xs)) - 1, len(xs) - 1)]:7.2f}  "
                  f"p995={q if q is not None else float('nan'):7.2f}  max={xs[-1]:7.2f}")
        return res

    ok = lambda r: r["veto"] == 0
    okg = lambda r: r["veto"] == 0 and r["gap"] <= 10.0
    t_all = table("plain", ok, "PLAIN-60, all days (veto-passing)")
    table("plain", okg, "PLAIN-60, all days + hole-gate (gap<=10s)")
    t_real = table("plain", lambda r: ok(r) and r["src"] == "real",
                   "PLAIN-60, REAL finals only (08-14+)")
    table("plain", lambda r: okg(r) and r["src"] == "real",
          "PLAIN-60, REAL finals + hole-gate")
    # synthetic-target validation: on real-final days, synthetic target vs real
    print("\n=== synthetic-target validation on 08-14+ (a60rx close vs served) ===")
    ds = sorted(abs(r["served"] - r["final"]) for r in rows
                if r["src"] == "real" and r["k"] == 2.0)
    # (identical by construction on real rows; recompute properly:)
    import statistics
    diffs = []
    for wd in windows:
        if wd["ep"] < RULE_TS or not wd["final"]:
            continue
        l_rx = sorted((rx, p) for rx, _ts, p in wd["l"])
        a60 = running_avg(l_rx, wd["ep"] + 300 - HORIZON, wd["ep"] + 300)
        if a60 is not None:
            diffs.append(abs(wd["final"] - a60))
    diffs.sort()
    if diffs:
        q = lambda f: diffs[min(int(f * len(diffs)), len(diffs) - 1)]
        print(f"n={len(diffs)}  med={q(0.5):.3f}  p90={q(0.9):.3f}  "
              f"p99={q(0.99):.3f}  p995={q(0.995):.3f}  max={diffs[-1]:.2f}")

    for nm in ("bz", "cb", "kl"):
        t_b = table(nm, okg, f"{nm.upper()}-bridged-60 + hole-gate")
        tp = table("plain", lambda r, n=nm: okg(r) and n in r,
                   f"PLAIN-60 paired to {nm} + hole-gate")
        print(f"\n--- bar check {nm} vs plain (paired, k>=6): p995 tighter at every k? ---")
        verdict = True
        for k in [x for x in KNOTS if x >= 6]:
            if k in t_b and k in tp and t_b[k][2] and tp[k][2]:
                tighter = t_b[k][2] <= tp[k][2]
                verdict &= tighter
                print(f"k={k:4.1f}  {nm} p995={t_b[k][2]:7.2f}  plain={tp[k][2]:7.2f}  "
                      f"{'TIGHTER' if tighter else 'WIDER'}   max {t_b[k][3]:7.2f} vs {tp[k][3]:7.2f}")
        print(f"{nm}: p995 tighter at every k>=6: {verdict}")

    print("\n=== kl vs bz agreement (paired, veto-ok) ===")
    dd = sorted(abs(r["kl"] - r["bz"]) for r in rows
                if "kl" in r and "bz" in r and r["veto"] == 0)
    if dd:
        q = lambda f: dd[min(int(f * len(dd)), len(dd) - 1)]
        print(f"n={len(dd)}  p50={q(0.5):.3f} p90={q(0.9):.3f} p99={q(0.99):.3f} max={dd[-1]:.3f}")


if __name__ == "__main__":
    main()
