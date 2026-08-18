"""MAX knots from per-tick INTERVAL maxima (the grid-point fit under-bounds).

The engine interpolates margin(k) at every tick; a MAX table fitted at fixed
grid ks can sit below the true error between knots. Here: per window, per raw
tick in the zone (veto-passing, coverage-gated, spot-fresh), |err_plain60|,
bucketed into knot intervals; each knot's MAX = the larger of its two
adjacent intervals' corpus maxima (so linear interpolation between knots
bounds every point of both intervals), rounded up to $1, monotone-enforced.
Real-final windows + synthetic union, same as the 08-18 freeze.
"""
import gzip
import json
import math
from bisect import bisect_right
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
RULE_TS = 1786665600
KNOTS = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 29.0,
         35.0, 40.0, 45.0, 50.0, 55.0, 58.0]
HORIZON = 60.0
SPOT_STALE_S = 3.0
RAW_GAP_MAX = 10.0
FROZEN_S = 20.0
FROZEN_RAW_MOVE = 2.0


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


def covered(recs, start, end):
    prev = start
    for rx, _p in recs:
        if rx <= start:
            continue
        if rx > end:
            break
        if rx - prev > RAW_GAP_MAX:
            return False
        prev = rx
    return end - prev <= RAW_GAP_MAX


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
    return len(spanned) >= 2 and (max(spanned) - min(spanned)) >= FROZEN_RAW_MOVE


def main():
    # interval i covers (KNOTS[i-1], KNOTS[i]]; interval 0 covers (0, KNOTS[0]]
    imax_real = [0.0] * len(KNOTS)
    imax_all = [0.0] * len(KNOTS)
    n_ticks = 0
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wd = json.loads(line)
            ep = wd["ep"]
            close = ep + 300
            t0 = close - HORIZON
            strike = wd["strike"]
            l = sorted(wd["l"])
            l_rx = [(rx, p) for rx, _ts, p in l]
            trecs = sorted(wd.get("t") or [])
            if ep >= RULE_TS:
                final = wd["final"]
                real = True
            else:
                final = running_avg(l_rx, t0, close)
                real = False
            if not final or not strike:
                continue
            for i, (rx, p) in enumerate(l_rx):
                k = close - rx
                if k <= 0 or k > KNOTS[-1]:
                    continue
                if not covered(l_rx, t0, rx):
                    continue
                if twap_frozen_at(trecs, l_rx, rx):
                    continue
                A = running_avg(l_rx, t0, rx)
                if A is None:
                    continue
                w = (rx - t0) / HORIZON
                err = abs(final - (w * A + (1 - w) * p))
                b = bisect_right(KNOTS, k - 1e-9)
                b = min(b, len(KNOTS) - 1)
                if err > imax_all[b]:
                    imax_all[b] = err
                if real and err > imax_real[b]:
                    imax_real[b] = err
                n_ticks += 1
    print(f"{n_ticks} veto/coverage-passing ticks")
    print("interval (k range]   max_real   max_all")
    lo = 0.0
    for i, kk in enumerate(KNOTS):
        print(f"({lo:4.1f},{kk:5.1f}]   {imax_real[i]:8.2f}  {imax_all[i]:8.2f}")
        lo = kk
    # knot value = max of adjacent intervals (both populations), rounded up $1
    out = []
    prev = 0.0
    for i, kk in enumerate(KNOTS):
        v = max(imax_all[i], imax_real[i],
                imax_all[i + 1] if i + 1 < len(KNOTS) else 0.0,
                imax_real[i + 1] if i + 1 < len(KNOTS) else 0.0)
        v = max(math.ceil(v), prev)
        out.append((kk, float(v)))
        prev = v
    print("\nTWAP60_MARGIN_MAX (interval-max convention) = (")
    print("    " + ", ".join(f"({k:.0f}.0, {v:.1f})" for k, v in out) + ",")
    print(")")


if __name__ == "__main__":
    main()
