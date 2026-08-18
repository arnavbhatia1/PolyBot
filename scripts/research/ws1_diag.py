"""Per-day forensics: raw cadence, reconstruction-vs-official basis, hole rate.

Dates the appearance of (a) the flat median basis, (b) the $14 p99.5 tails,
(c) raw delivery holes; separates estimator mismatch from projection physics.
"""
import gzip
import json
import math
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"


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


def q(xs, f):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(int(f * len(xs)), len(xs) - 1)]


def main():
    days = {}
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wd = json.loads(line)
            day = datetime.fromtimestamp(wd["ep"], timezone.utc).strftime("%m-%d")
            days.setdefault(day, []).append(wd)

    print("day    n_win  raw_gap_med/p99   |final-A30|_med/p99/max   "
          "basis@k15_med/p99   holes>5s  |err_k2|_med/p995")
    for day in sorted(days):
        wins = days[day]
        gaps_all, basis_close, basis_mid, holes, err_k2 = [], [], [], 0, []
        for wd in wins:
            ep = wd["ep"]
            close = ep + 300
            t0 = close - 30.0
            l = sorted(wd["l"])
            l_rx = [(rx, p) for rx, _ts, p in l]
            span = [rx for rx, _p in l_rx if t0 <= rx <= close]
            g = [b - a for a, b in zip(span, span[1:])]
            gaps_all.extend(g)
            if g and max(g) > 5.0:
                holes += 1
            A30 = running_avg(l_rx, t0, close)
            if A30 is not None and wd["final"]:
                basis_close.append(abs(wd["final"] - A30))
            # mid-window basis vs official stream value at k=15
            t = close - 15.0
            trecs = sorted(wd.get("t") or [])
            off = None
            for rx, ts, p in trecs:
                if rx <= t:
                    off = p
                else:
                    break
            A_t = running_avg(l_rx, t - 30.0, t)
            if off is not None and A_t is not None:
                basis_mid.append(abs(off - A_t))
            # plain err at k=2
            t2 = close - 2.0
            i = bisect_right(l_rx, (t2, float("inf"))) - 1
            if i >= 0 and t2 - l_rx[i][0] <= 3.0:
                A = running_avg(l_rx, t0, t2)
                if A is not None and wd["final"]:
                    w = (t2 - t0) / 30.0
                    err_k2.append(abs(wd["final"] - (w * A + (1 - w) * l_rx[i][1])))
        print(f"{day}  {len(wins):5d}  {q(gaps_all, 0.5):4.2f}/{q(gaps_all, 0.99):5.2f}      "
              f"{q(basis_close, 0.5):6.3f}/{q(basis_close, 0.99):6.2f}/{max(basis_close) if basis_close else float('nan'):6.2f}   "
              f"{q(basis_mid, 0.5):6.3f}/{q(basis_mid, 0.99):6.2f}   "
              f"{holes:4d}     {q(err_k2, 0.5):6.3f}/{q(err_k2, 0.995):6.2f}")

    # zoom: the 08-07 05:05 hole window
    for wd0 in days.get("08-07", []):
        if wd0["ep"] == 1786079100:
            l = sorted(wd0["l"])
            print(f"\nep 1786079100 raw records in zone (close={wd0['ep'] + 300}):")
            for rx, ts, p in l[-25:]:
                print(f"  rx={rx:.3f} (k={wd0['ep'] + 300 - rx:6.1f})  ts={ts:.3f}  p={p:.2f}")
            print(f"  final={wd0['final']}  strike={wd0['strike']}")


if __name__ == "__main__":
    main()
