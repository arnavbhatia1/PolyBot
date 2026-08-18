"""WS3: lock-dip supply under the 60s rule.

A dip event = a contiguous span where (a) the PLAIN projection's displacement
clears the MAX-tier margin at the latest decision tick with k in [6,60]
(coverage-gated, stall-vetoed, spot-fresh — engine-faithful), on the WINNER
side, and (b) the winner's ask <= 0.96 (executable). Measures frequency,
depth, duration, FOK reachability (ask still within one tick at t+RTT), and
how the BRIDGED projection changes which events qualify. Wrong-side max
locks (breach-dips) counted separately.

Bar (charter): the leg keeps its config if dips >= 1 per 3 trading days AND
harness EW >= +2c/sh after the real taker fee. Otherwise stage disarm as
dormant-pending-regime.
"""
import gzip
import json
import sys
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from polybot.core.signal_engine import TWAP_MARGIN_MAX, twap_margin  # noqa: E402

RULE_TS = 1786665600
HORIZON = 60.0
K_MIN, K_MAX = 6.0, 60.0
ASK_CAP = 0.96
RTT = 0.436
PAD = 0.01
SPOT_STALE_S = 3.0
RAW_GAP_MAX = 10.0
FROZEN_S = 20.0
FROZEN_RAW_MOVE = 2.0

import importlib.util
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)


def main():
    wins = {}
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wd = json.loads(line)
            if wd["ep"] >= RULE_TS:
                wins[wd["ep"]] = wd
    books = {}
    with gzip.open(DATA / "winner_books.jsonl.gz", "rt") as f:
        for line in f:
            r = json.loads(line)
            books[r["ep"]] = r["asks"]
    kl_ts, kl_px = lr.load_klines()
    lags = sorted(rx - ts for w in wins.values() for rx, ts, _p in w["bz"] if rx and ts)
    bz_lag = lags[len(lags) // 2] if lags else 0.45
    print(f"{len(wins)} windows, {len(books)} with winner books")

    events = []
    breach_lock_windows = 0
    br_only = br_lost = 0     # bridged-vs-plain qualification deltas (event level)
    for ep, wd in sorted(wins.items()):
        close = ep + 300
        t0 = close - HORIZON
        strike, final = wd["strike"], wd["final"]
        if not strike or not final or ep not in books:
            continue
        winner = "Up" if wd["up"] else "Down"
        l = sorted(wd["l"])
        l_rx = [(rx, p) for rx, _ts, p in l]
        trecs = sorted(wd.get("t") or [])
        bz = wd["bz"]
        if not bz and kl_ts:
            i0 = bisect_right(kl_ts, ep + 195)
            i1 = bisect_right(kl_ts, ep + 306)
            bz = [(S + 1 + bz_lag, S + 1.0, px)
                  for S, px in zip(kl_ts[i0:i1], kl_px[i0:i1])]

        # lock-state series at raw ticks (plain + bridged)
        lock_pl, lock_br = [], []      # (rx, state) state in {0: no, 1: winner-lock, -1: wrong-side-lock}
        for i, (rx, p) in enumerate(l_rx):
            k = close - rx
            if k > K_MAX or k < K_MIN:
                continue
            st_pl = st_br = 0
            if not lr.twap_frozen_at(trecs, l_rx, rx) and lr.covered(l_rx, t0, rx):
                A = lr.running_avg(l_rx, t0, rx)
                if A is not None:
                    w = (rx - t0) / HORIZON
                    m = twap_margin(TWAP_MARGIN_MAX, k)
                    disp = w * A + (1 - w) * p - strike
                    if abs(disp) >= m:
                        st_pl = 1 if ("Up" if disp >= 0 else "Down") == winner else -1
                    raw_ts = l[i][1]           # payload ts of this report
                    d = lr.bridge_delta(bz, raw_ts, rx) if bz else None
                    dispb = w * A + (1 - w) * (p + (d or 0.0)) - strike
                    if abs(dispb) >= m:
                        st_br = 1 if ("Up" if dispb >= 0 else "Down") == winner else -1
            lock_pl.append((rx, st_pl))
            lock_br.append((rx, st_br))
        if any(s == -1 for _, s in lock_pl):
            breach_lock_windows += 1

        def state_at(series, t):
            out = 0
            for rx, s in series:
                if rx <= t:
                    out = s
                else:
                    break
            return out

        asks = sorted((float(t), float(a)) for t, a in books[ep])

        def scan(series):
            evs = []
            cur = None
            for i, (ts, a) in enumerate(asks):
                k = close - ts
                if k < K_MIN or k > K_MAX:
                    continue
                ok = state_at(series, ts) == 1 and 0.0 < a <= ASK_CAP
                if ok:
                    if cur is None:
                        cur = dict(start=ts, min_ask=a, entry=a, reach=False)
                    cur["min_ask"] = min(cur["min_ask"], a)
                    # reachability: ask at ts+RTT still within one tick
                    j = bisect_right(asks, (ts + RTT, float("inf"))) - 1
                    if j >= 0 and asks[j][1] <= a + PAD and asks[j][1] > 0:
                        cur["reach"] = True
                else:
                    if cur is not None:
                        cur["dur"] = round(ts - cur["start"], 2)
                        evs.append(cur)
                        cur = None
            if cur is not None:
                cur["dur"] = round(close - cur["start"], 2)
                evs.append(cur)
            return evs

        ev_pl = scan(lock_pl)
        ev_br = scan(lock_br)
        for e in ev_pl:
            e["ep"] = ep
            events.append(e)
        br_only += max(0, len(ev_br) - len(ev_pl))
        br_lost += max(0, len(ev_pl) - len(ev_br))

    days = {datetime.fromtimestamp(e["ep"], timezone.utc).strftime("%m-%d")
            for e in events}
    n_days = len({datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d")
                  for ep in wins})
    print(f"\nPLAIN max-tier winner-side dip events (ask<={ASK_CAP}, k in [6,60]):")
    print(f"  {len(events)} events over {n_days} days "
          f"({len({e['ep'] for e in events})} windows) -> "
          f"{len(events) / max(1, n_days):.2f}/day")
    if events:
        depths = sorted(e["min_ask"] for e in events)
        durs = sorted(e["dur"] for e in events)
        reach = sum(1 for e in events if e["reach"])
        q = lambda xs, f: xs[min(int(f * len(xs)), len(xs) - 1)]
        print(f"  min-ask q25/50/75: {q(depths, .25):.3f}/{q(depths, .5):.3f}/{q(depths, .75):.3f}")
        print(f"  <=0.93: {sum(1 for d in depths if d <= 0.93)}  <=0.90: {sum(1 for d in depths if d <= 0.90)}  <=0.85: {sum(1 for d in depths if d <= 0.85)}")
        print(f"  duration q25/50/75: {q(durs, .25):.2f}/{q(durs, .5):.2f}/{q(durs, .75):.2f}s")
        print(f"  FOK-reachable at RTT {RTT}s: {reach}/{len(events)}")
        for e in events:
            d = datetime.fromtimestamp(e["ep"], timezone.utc).strftime("%m-%d %H:%M")
            print(f"    {d} entry {e['entry']:.3f} min {e['min_ask']:.3f} "
                  f"dur {e['dur']:5.2f}s reach {e['reach']}")
    print(f"\nwrong-side max locks (breach-dips possible): {breach_lock_windows} windows")
    print(f"bridged-vs-plain qualification: +{br_only} events bridged-only, "
          f"-{br_lost} plain-only")


if __name__ == "__main__":
    main()
