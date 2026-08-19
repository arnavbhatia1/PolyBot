"""WS1: out-of-fit validation of the ladder floor (need 0.5 vs 1.0/1.5/2.0).

The 08-18 grid scored need 0.5 on the same tape its tables were fit on.
Here: walk-forward splits + leave-one-day-out over the 60s-era real-final
days. For each split, p99.5 tables are fit ONLY on the fit days (same
conventions: veto-passing, coverage-gated, one sample per window per knot,
round-up $0.5, monotone) and the engine-true ladder replay scores ONLY the
held-out days. ANTI-side controls on the held-out days. Sign flips reported
per day with exact one-sided 95% binomial upper bounds.

Pre-registered bar (charter, written before this run): need 0.5 keeps its
place only if on strictly out-of-fit days (i) every rung's win rate clears
break-even+10pp, (ii) EW >= +5c/sh, (iii) ANTI stays decisively negative,
(iv) no full-sweep loss on an arm the 1.0 floor would have vetoed
(place_mult < 1.0). Corpus too thin to evaluate (i)-(ii) -> terminal state
(c): pin the unblock corpus size, stage need 1.0 interim.
"""
import gzip
import json
import math
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
import importlib.util

SP = Path(__file__).parent
DATA = SP / "data"
RULE_TS = 1786665600
KNOTS = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 29.0,
         35.0, 40.0, 45.0, 50.0, 55.0, 58.0]
NEEDS = [0.5, 1.0, 1.5, 2.0]

spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)


def day_of(ep):
    return datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d")


def cp_upper(f, n, alpha=0.05):
    """One-sided (1-alpha) Clopper-Pearson upper bound on a binomial rate."""
    if n == 0:
        return float("nan")
    if f >= n:
        return 1.0

    def logcdf(p):
        # log P(X <= f) via log-sum-exp
        if p <= 0:
            return 0.0
        if p >= 1:
            return float("-inf")
        terms = []
        for i in range(f + 1):
            terms.append(math.lgamma(n + 1) - math.lgamma(i + 1)
                         - math.lgamma(n - i + 1)
                         + i * math.log(p) + (n - i) * math.log(1 - p))
        m = max(terms)
        return m + math.log(sum(math.exp(t - m) for t in terms))

    lo, hi = f / n if n else 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if logcdf(mid) > math.log(alpha):
            lo = mid
        else:
            hi = mid
    return hi


def fit_p995(fit_days):
    """p99.5 knots from real-final windows of fit_days only."""
    samples = {k: [] for k in KNOTS}
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wd = json.loads(line)
            ep = wd["ep"]
            if ep < RULE_TS or day_of(ep) not in fit_days:
                continue
            final, strike = wd["final"], wd["strike"]
            if not final or not strike:
                continue
            close = ep + 300
            t0 = close - lr.HORIZON
            l = sorted(wd["l"])
            l_rx = [(rx, p) for rx, _ts, p in l]
            trecs = sorted(wd.get("t") or [])
            for k in KNOTS:
                t = close - k
                i = bisect_right(l_rx, (t, float("inf"))) - 1
                if i < 0 or t - l_rx[i][0] > lr.SPOT_STALE_S:
                    continue
                if not lr.covered(l_rx, t0, t):
                    continue
                if lr.twap_frozen_at(trecs, l_rx, t):
                    continue
                A = lr.running_avg(l_rx, t0, t)
                if A is None:
                    continue
                w = (t - t0) / lr.HORIZON
                samples[k].append(abs(final - (w * A + (1 - w) * l_rx[i][1])))
    knots = []
    prev = 0.0
    for k in KNOTS:
        xs = sorted(samples[k])
        if len(xs) < 50:
            continue
        q = xs[min(math.ceil(0.995 * len(xs)) - 1, len(xs) - 1)]
        v = max(math.ceil(q / 0.5) * 0.5, prev)
        knots.append((k, v))
        prev = v
    return knots


def score(label, table, scored_eps, need, anti=False):
    res = lr.run(need=need, anti=anti, table=table, eps=scored_eps)
    fills = [r for r in res if r["filled"] > 0]
    pnl = sum(r["pnl"] for r in fills)
    flips = [r for r in res if r["side"] != r["winner"]]
    rung_stat = {}
    for r in fills:
        for rp, sh in r["rungs"].items():
            s = rung_stat.setdefault(rp, [0, 0])
            s[0] += 1
            s[1] += 1 if r["win"] else 0
    ew = (sum(r["pnl"] for r in fills) /
          sum(r["filled"] for r in fills)) if fills else float("nan")
    rs = " ".join(f"{rp}:{s[1]}/{s[0]}" for rp, s in sorted(rung_stat.items(),
                                                            reverse=True))
    print(f"  {label:26s} need {need:3.1f}{' ANTI' if anti else '     '} "
          f"arm {len(res):4d} flip {len(flips):2d} "
          f"fill {len(fills):3d} win {sum(1 for r in fills if r['win']):3d} "
          f"pnl {pnl:+8.2f}$ EW {ew * 100 if ew == ew else float('nan'):+6.1f}c/sh "
          f"rungs[{rs}]")
    return dict(res=res, fills=fills, pnl=pnl, flips=flips, rung=rung_stat)


def main():
    # window epochs per day
    eps_by_day = {}
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wd = json.loads(line)
            if wd["ep"] >= RULE_TS:
                eps_by_day.setdefault(day_of(wd["ep"]), set()).add(wd["ep"])
    days = sorted(eps_by_day)
    print("real-final days:", {d: len(eps_by_day[d]) for d in days})

    splits = [("A fit 14-15 / score 16-18", days[:2], days[2:]),
              ("B fit 16-18 / score 14-15", days[2:], days[:2])]
    for d in days:
        splits.append((f"LODO score {d}", [x for x in days if x != d], [d]))

    lodo_flip = {}
    lodo_res = {n: [] for n in NEEDS}
    for label, fit_days, sc_days in splits:
        tab = fit_p995(fit_days)
        sc_eps = set().union(*(eps_by_day[d] for d in sc_days))
        print(f"\n=== {label} (fit n_knots={len(tab)}, "
              f"k6={dict(tab).get(6.0)}, k25={dict(tab).get(25.0)}) ===")
        by_need = {}
        for need in NEEDS:
            out = score(label, tab, sc_eps, need)
            by_need[need] = out
            if label.startswith("LODO"):
                lodo_res[need].extend(out["res"])
                if need == 0.5:
                    d = sc_days[0]
                    lodo_flip[d] = (len(out["flips"]), len(out["res"]))
        # bar clause (iv): 0.5-fills LOST in windows the 1.0 floor never armed
        armed_10 = {r["ep"] for r in by_need[1.0]["res"]}
        thin = [r for r in by_need[0.5]["fills"]
                if not r["win"] and r["ep"] not in armed_10]
        if thin:
            print(f"  ** clause-(iv) VIOLATION: {len(thin)} loss(es) on "
                  f"0.5-only arms: {[r['ep'] for r in thin]}")
        for need in (0.5, 1.0):
            score(label, tab, sc_eps, need, anti=True)

    print("\n=== LODO pooled (every day scored strictly out-of-fit) ===")
    for need in NEEDS:
        res = lodo_res[need]
        fills = [r for r in res if r["filled"] > 0]
        pnl = sum(r["pnl"] for r in fills)
        ew = (pnl / sum(r["filled"] for r in fills)) if fills else float("nan")
        rung_stat = {}
        for r in fills:
            for rp in r["rungs"]:
                s = rung_stat.setdefault(rp, [0, 0])
                s[0] += 1
                s[1] += 1 if r["win"] else 0
        rs = " ".join(f"{rp}:{s[1]}/{s[0]}" for rp, s in sorted(rung_stat.items(), reverse=True))
        flips = sum(1 for r in res if r["side"] != r["winner"])
        print(f"need {need:3.1f}: arm {len(res):4d} flip {flips} "
              f"fill {len(fills)} pnl {pnl:+8.2f}$ "
              f"EW {ew * 100 if ew == ew else float('nan'):+6.1f}c/sh  rungs[{rs}]")

    print("\n=== OOS sign record per day (need 0.5, LODO tables) ===")
    for d in days:
        f, n = lodo_flip.get(d, (0, 0))
        print(f"{d}: flips {f}/{n} armed  CP95-upper flip rate "
              f"{cp_upper(f, n):.4f}" if n else f"{d}: no arms")


if __name__ == "__main__":
    main()
