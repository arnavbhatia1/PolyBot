"""R1 context: how the frozen 08-18 tables held up on the full 14-day corpus.

Per knot: grid-sample exceedance rate of the frozen p99.5 (design 0.5%) and
per-tick exceedance of the frozen MAX (design 0), plus the raw p99.5 fit split
by regime half (08-14..18 UTC vs 08-19..27 UTC) so a widening can be read as
regime-driven or corpus-wide. Measurement only — no table is derived here.
"""
import csv
import gzip
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
OUT = DATA / "vps-0821"
sys.path.insert(0, str(SP))
from ws1_interval_max import (KNOTS, RULE_TS, HORIZON, running_avg, covered,  # noqa: E402
                              twap_frozen_at)
from r1_refit import FROZEN_P995, FROZEN_MAX, load_rows, p995  # noqa: E402

SPLIT_TS = 1787097600            # 2026-08-19 00:00 UTC


def margin(kn, k):
    kn = sorted(kn.items())
    if k <= kn[0][0]:
        return kn[0][1]
    for (x0, y0), (x1, y1) in zip(kn, kn[1:]):
        if k <= x1:
            return y0 + (y1 - y0) * (k - x0) / (x1 - x0)
    return kn[-1][1]


def main():
    rows = load_rows()
    print("frozen p99.5 exceedance on the 14-day corpus (grid samples, design 0.5%)")
    print("k | n | n>frozen p99.5 | rate | raw p99.5 08-14..18 | 08-19..27 | n each")
    exc = {}
    for k in KNOTS:
        xs = [r for r in rows if r["k"] == k]
        over = sum(1 for r in xs if r["err"] > FROZEN_P995[k])
        a = [r["err"] for r in xs if r["ep"] < SPLIT_TS]
        b = [r["err"] for r in xs if r["ep"] >= SPLIT_TS]
        qa, qb = p995(a), p995(b)
        exc[k] = dict(n=len(xs), over=over, rate=over / len(xs) if xs else None,
                      p995_early=qa, p995_late=qb, n_early=len(a), n_late=len(b))
        print(f"k={k:4.1f} | {len(xs):5d} | {over:4d} | {100 * over / len(xs):5.2f}% | "
              f"{qa:7.2f} | {qb:7.2f} | {len(a)}/{len(b)}")

    # per-tick breaches of the frozen MAX (the taker's never-breach premise)
    breaches = []
    n_ticks = 0
    for line in gzip.open(DATA / "win_streams.jsonl.gz", "rt"):
        wd = json.loads(line)
        ep = wd["ep"]
        if ep < RULE_TS or not wd["final"] or not wd["strike"]:
            continue
        close = ep + 300
        t0 = close - HORIZON
        l = sorted(wd["l"])
        l_rx = [(rx, p) for rx, _ts, p in l]
        trecs = sorted(wd.get("t") or [])
        worst = None
        for rx, p in l_rx:
            k = close - rx
            if k < 6.0 or k > KNOTS[-1]:
                continue
            if not covered(l_rx, t0, rx) or twap_frozen_at(trecs, l_rx, rx):
                continue
            A = running_avg(l_rx, t0, rx)
            if A is None:
                continue
            n_ticks += 1
            w = (rx - t0) / HORIZON
            proj = w * A + (1 - w) * p
            err = abs(wd["final"] - proj)
            m = margin(FROZEN_MAX, k)
            if err > m:
                # a breach only matters if it would have LOCKED the wrong side:
                disp = proj - wd["strike"]
                wrong = (disp >= 0) != bool(wd["up"])
                ratio = err / m
                if worst is None or ratio > worst["ratio"]:
                    worst = dict(ep=ep, utc=datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d %H:%M"),
                                 k=round(k, 2), err=round(err, 2), max_margin=round(m, 2),
                                 ratio=round(ratio, 2), disp=round(disp, 2),
                                 wrong_side_lock=bool(wrong and abs(disp) >= m))
        if worst:
            breaches.append(worst)
    breaches.sort(key=lambda b: -b["ratio"])
    wrong = [b for b in breaches if b["wrong_side_lock"]]
    print(f"\nfrozen MAX per-tick breaches (k in [6,58], gated): {len(breaches)} windows of "
          f"{n_ticks} ticks; wrong-side max-tier locks: {len(wrong)}")
    for b in breaches[:12]:
        print(f"  {b['utc']} k={b['k']:5.2f} err ${b['err']:6.2f} vs MAX ${b['max_margin']:6.2f} "
              f"({b['ratio']:.2f}x) disp {b['disp']:+8.2f} wrong-side-lock={b['wrong_side_lock']}")
    json.dump(dict(exceedance=exc, max_breaches=breaches, wrong_side_locks=wrong),
              open(OUT / "r1_regime.json", "w"), indent=1)


if __name__ == "__main__":
    main()
