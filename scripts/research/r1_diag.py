"""R1 diagnostics: per-day error distribution of the plain-60 projection vs the
served real final, plus the worst windows, so a widening can be attributed
(one day / data hole / regime) before any table is adopted."""
import gzip
import json
import math
import sys
from bisect import bisect_right
from datetime import datetime, timezone, timedelta
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
RULE_TS = 1786665600
HORIZON = 60.0
SPOT_STALE_S = 3.0
RAW_GAP_MAX = 10.0
sys.path.insert(0, str(SP))
from ws1_interval_max import running_avg, covered, twap_frozen_at  # noqa: E402

ET = timezone(timedelta(hours=-4))


def utc_day(ep):
    return datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d")


def et_day(ep):
    return datetime.fromtimestamp(ep, ET).strftime("%m-%d")


def q(xs, f):
    if not xs:
        return float("nan")
    return xs[min(math.ceil(f * len(xs)) - 1, len(xs) - 1)]


def main():
    bnd = json.load(open(DATA / "boundaries.json"))
    per_day = {}
    worst = []
    knot_err = {}   # (day, k) -> list of gated plain errors
    n_real = 0
    for line in gzip.open(DATA / "win_streams.jsonl.gz", "rt"):
        wd = json.loads(line)
        ep = wd["ep"]
        if ep < RULE_TS or not wd["final"] or not wd["strike"]:
            continue
        n_real += 1
        close = ep + 300
        t0 = close - HORIZON
        l = sorted(wd["l"])
        l_rx = [(rx, p) for rx, _ts, p in l]
        trecs = sorted(wd.get("t") or [])
        final, strike = wd["final"], wd["strike"]
        a60 = running_avg(l_rx, t0, close)
        d = per_day.setdefault(utc_day(ep), dict(n=0, a60=[], gap=[], holes=0))
        d["n"] += 1
        d["gap"].append(abs(final - strike))
        cov = covered(l_rx, t0, close)
        if not cov:
            d["holes"] += 1
        if a60 is not None:
            e = abs(final - a60)
            d["a60"].append(e)
            # official stream value at/after close and the boundary capture
            t_at = [(rx, ts, p) for rx, ts, p in trecs if ts >= close]
            tval = t_at[0][2] if t_at else None
            b = bnd.get(str(close))
            worst.append((e, ep, utc_day(ep), strike, final, round(a60, 3),
                          tval, b[2] if b else None, cov,
                          len([1 for rx, _p in l_rx if t0 <= rx <= close])))
        for k in (6.0, 12.0, 25.0):
            t = close - k
            i = bisect_right(l_rx, (t, float("inf"))) - 1
            if i < 0 or t - l_rx[i][0] > SPOT_STALE_S or not covered(l_rx, t0, t):
                continue
            if twap_frozen_at(trecs, l_rx, t):
                continue
            A = running_avg(l_rx, t0, t)
            if A is None:
                continue
            w = (t - t0) / HORIZON
            knot_err.setdefault((utc_day(ep), k), []).append(
                abs(final - (w * A + (1 - w) * l_rx[i][1])))

    print(f"{n_real} real-final windows")
    print("day    n  holes | a60rx-vs-served med   p90   p99   max  n>2 | gap p50 | p995@6 p995@12 p995@25 (gated plain) | max@25")
    for day in sorted(per_day):
        d = per_day[day]
        a = sorted(d["a60"])
        g = sorted(d["gap"])
        k6 = sorted(knot_err.get((day, 6.0), []))
        k12 = sorted(knot_err.get((day, 12.0), []))
        k25 = sorted(knot_err.get((day, 25.0), []))
        print(f"{day} {d['n']:4d} {d['holes']:5d} | {q(a,0.5):6.3f} {q(a,0.9):6.3f} {q(a,0.99):6.2f} {q(a,1.0):7.2f} {sum(1 for x in a if x > 2):3d} | {q(g,0.5):6.2f} | "
              f"{q(k6,0.995):6.2f} {q(k12,0.995):7.2f} {q(k25,0.995):7.2f} | {q(k25,1.0):7.2f}")
    worst.sort(reverse=True)
    print("\nworst |served - a60rx(close)|: err ep day strike final a60rx t_at_close bnd_capture covered n_raw")
    for row in worst[:25]:
        print("  ", row)
    json.dump({"per_day": {d: dict(n=v["n"], holes=v["holes"],
                                   a60_med=q(sorted(v["a60"]), 0.5),
                                   a60_p99=q(sorted(v["a60"]), 0.99),
                                   a60_max=q(sorted(v["a60"]), 1.0))
                           for d, v in per_day.items()},
               "worst": worst[:50]},
              open(DATA / "vps-0821" / "r1_diag.json", "w"), indent=1)


if __name__ == "__main__":
    main()
