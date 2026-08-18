"""Identify the post-08-14 resolution source: test candidate finals per window.

Candidates (no-kline set):
  a30rx   rx-clock ZOH 30s avg of raw stream at close (engine reconstruction)
  a30ts   payload-clock ZOH 30s avg at close
  a60rx   rx-clock 60s avg (window doubled?)
  twapi   official stream linearly interpolated at the exact close instant
  twlast  official stream last report strictly before close
  rawat   raw stream first report with ts >= close
  rawbef  raw stream last report with ts < close
Kline set (added when data exists):
  kl30    30s mean of Binance 1s closes ending at close
  kl60    60s mean
  klat    Binance 1s close at the boundary second
Reports per-day median/p90/max |final - candidate| for 08-14..17 (and 08-12..13
as the control where the old rule held).
"""
import gzip
import json
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"


def running_avg(recs, start, end):
    seed = None
    pts = []
    for c, p in recs:
        if c <= start:
            seed = p
        elif c <= end:
            pts.append((c, p))
    if seed is None:
        if not pts or pts[0][0] > start + 2.0:
            return None
        seed = pts[0][1]
    acc, prev_t, prev_p = 0.0, start, seed
    for c, p in pts:
        acc += prev_p * (c - prev_t)
        prev_t, prev_p = c, p
    acc += prev_p * (end - prev_t)
    return acc / (end - start) if end > start else prev_p


def load_klines():
    ts_arr, px_arr = [], []
    for name in ("binance_1s.csv", "binance_1s_late.csv"):
        p = DATA / name
        if not p.exists():
            continue
        with open(p) as f:
            next(f)
            for line in f:
                a, b = line.rstrip("\n").split(",")
                ts_arr.append(int(a))
                px_arr.append(float(b))
    z = sorted(zip(ts_arr, px_arr))
    return [t for t, _ in z], [p for _, p in z]


def main():
    wins = []
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wins.append(json.loads(line))
    kl_ts, kl_px = load_klines()
    print(f"klines merged: {len(kl_ts)} rows")

    cands = ["a30rx", "a30ts", "a60rx", "twapi", "twlast", "rawat", "rawbef",
             "kl30", "kl60", "klat"]
    day_diffs = {}
    for wd in wins:
        ep = wd["ep"]
        close = ep + 300
        day = datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d")
        if day < "08-12":
            continue
        final = wd["final"]
        if not final:
            continue
        l = sorted(wd["l"])
        l_rx = [(rx, p) for rx, _ts, p in l]
        l_ts = sorted((ts, p) for _rx, ts, p in l)
        trecs = sorted((ts, p) for _rx, ts, p in wd.get("t") or [])
        v = {}
        v["a30rx"] = running_avg(l_rx, close - 30, close)
        v["a30ts"] = running_avg(l_ts, close - 30, close)
        v["a60rx"] = running_avg(l_rx, close - 60, close)
        i = bisect_right(trecs, (close, float("inf")))
        lo = trecs[i - 1] if i >= 1 else None
        hi = trecs[i] if i < len(trecs) else None
        if lo and lo[0] == close:
            v["twapi"] = lo[1]
        elif lo and hi and hi[0] > lo[0]:
            f = (close - lo[0]) / (hi[0] - lo[0])
            v["twapi"] = lo[1] + f * (hi[1] - lo[1])
        v["twlast"] = next((p for ts, p in reversed(trecs) if ts < close), None)
        v["rawat"] = next((p for ts, p in l_ts if ts >= close), None)
        v["rawbef"] = next((p for ts, p in reversed(l_ts) if ts < close), None)
        if kl_ts:
            i0 = bisect_right(kl_ts, close - 30)
            i1 = bisect_right(kl_ts, close)
            if i1 - i0 >= 25:
                v["kl30"] = sum(kl_px[i0:i1]) / (i1 - i0)
            i0 = bisect_right(kl_ts, close - 60)
            if i1 - i0 >= 50:
                v["kl60"] = sum(kl_px[i0:i1]) / (i1 - i0)
            j = bisect_right(kl_ts, close) - 1
            if j >= 0 and close - kl_ts[j] <= 2:
                v["klat"] = kl_px[j]
        dd = day_diffs.setdefault(day, {c: [] for c in cands})
        for c in cands:
            if v.get(c) is not None:
                dd[c].append(abs(final - v[c]))

    def q(xs, f):
        if not xs:
            return float("nan")
        xs = sorted(xs)
        return xs[min(int(f * len(xs)), len(xs) - 1)]

    for day in sorted(day_diffs):
        dd = day_diffs[day]
        print(f"\n{day} (n~{max(len(x) for x in dd.values())})   med / p90 / max   |final - candidate|")
        for c in cands:
            xs = dd[c]
            if not xs:
                continue
            print(f"  {c:7s} n={len(xs):4d}  {q(xs, 0.5):8.3f} {q(xs, 0.9):8.3f} {max(xs):8.3f}")


if __name__ == "__main__":
    main()
