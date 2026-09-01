"""R21 (09-01, operator-directed): the stale-quote race with a DIRECT Binance tick feed.

Question: after a Binance jump, how long until the favored token ask on Polymarket
reprices, does a competing taker hit it first, and what would a taker with total
signal-to-order-live latency L earn buying the stale ask, scored on the window
OUTCOME (hold to resolution) and mark-to-market after the reprice?
Inputs: Binance spot aggTrades (exchange microseconds), micro b-records (CLOB BBO,
our rx clock), tape prints, labels. Final 90 s of each window only (micro coverage).
"""
import gzip
import json
import sqlite3
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np

SP = Path(__file__).parent
D = SP / "data" / "vps-0831"
REC = SP.resolve().parents[1] / "polybot" / "memory" / "recordings"
DAYS = ["2026-08-20", "2026-08-24", "2026-08-25", "2026-08-27", "2026-08-29"]
JUMPS = [15.0, 25.0, 40.0]          # dollars over 300 ms
L_TOTALS = [0.10, 0.15, 0.20, 0.25, 0.35, 0.50, 0.80]
PM_DELIVERY = 0.10                    # CLOB WS delivery lag to our box (assumed)
K_MIN, K_MAX = 6.0, 90.0

con = sqlite3.connect(f"file:{D / 'paper_0901.db'}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
tok = {}
for r in con.execute("SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'"):
    ep = int(r["window_id"].rsplit("-", 1)[1])
    if r["resolved_up"] is None:
        continue
    wu = bool(r["resolved_up"])
    if r["token_up"]:
        tok[r["token_up"]] = (ep, True, wu)
    if r["token_down"]:
        tok[r["token_down"]] = (ep, False, not wu)
ep2tok = defaultdict(dict)
for t, (ep, is_up, _w) in tok.items():
    ep2tok[ep]["up" if is_up else "down"] = t

stats = {J: dict(events=0, in_window=0, usable=0, lag=[], hit_before_reprice=0,
                 hit_t=[], k_le25=0, per_L={L: dict(n=0, wins=0, pnl=0.0, mtm=0.0, mtm_n=0)
                                            for L in L_TOTALS}) for J in JUMPS}
def opener(stem):
    p = REC / f"{stem}.jsonl.gz"
    if p.exists():
        return gzip.open(p, "rt", encoding="utf-8")
    return open(REC / f"{stem}.jsonl", encoding="utf-8")


day_n = 0
for day in DAYS:
    with zipfile.ZipFile(D / "binance_agg" / f"agg_{day}.zip") as z:
        with z.open(z.namelist()[0]) as f:
            arr = np.loadtxt(f, delimiter=",", usecols=(1, 5), dtype=np.float64)
    px, tus = arr[:, 0], arr[:, 1]
    ts = tus / 1e6
    t_start = np.floor(ts[0])
    grid = ((ts - t_start) / 0.05).astype(np.int64)
    ng = int(grid[-1]) + 1
    last = np.full(ng, np.nan)
    last[grid] = px
    m = np.isnan(last)
    idx = np.where(~m, np.arange(ng), 0)
    np.maximum.accumulate(idx, out=idx)
    last = last[idx]
    d300 = np.zeros(ng)
    d300[6:] = last[6:] - last[:-6]
    bbo = defaultdict(list)
    with opener(f"micro_{day}") as f:
        for line in f:
            if len(line) < 9 or line[7] != "b":
                continue
            r = json.loads(line)
            if r["token"] in tok:
                try:
                    bbo[r["token"]].append((float(r["ts"]), float(r["bid"]), float(r["ask"])))
                except (TypeError, ValueError):
                    pass
    for v in bbo.values():
        v.sort()
    bbo_ts = {t: np.array([x[0] for x in v]) for t, v in bbo.items()}
    prints = defaultdict(list)
    with opener(f"tape_{day}") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r["token"] in tok:
                try:
                    prints[r["token"]].append((float(r["ts"]), float(r["price"]), r["side"]))
                except (TypeError, ValueError):
                    pass
    for v in prints.values():
        v.sort()
    pr_ts = {t: np.array([x[0] for x in v]) for t, v in prints.items()}
    day_n += 1
    for J in JUMPS:
        ev_idx = np.where(np.abs(d300) >= J)[0]
        last_ev = -1e9
        for gi in ev_idx:
            T = t_start + gi * 0.05
            if T - last_ev < 2.0:
                continue
            last_ev = T
            st = stats[J]
            st["events"] += 1
            ep = int(T // 300) * 300
            k = ep + 300 - T
            if not (K_MIN <= k <= K_MAX) or ep not in ep2tok:
                continue
            st["in_window"] += 1
            side = "up" if d300[gi] > 0 else "down"
            t_fav = ep2tok[ep].get(side)
            if t_fav is None or t_fav not in bbo_ts:
                continue
            arr_ts = bbo_ts[t_fav]
            j = int(np.searchsorted(arr_ts, T + PM_DELIVERY)) - 1
            if j < 0:
                continue
            ask0 = bbo[t_fav][j][2]
            if not (0.03 < ask0 < 0.97):
                continue
            st["usable"] += 1
            if k <= 25:
                st["k_le25"] += 1
            lag = np.inf
            new_mid = None
            for qq in range(j + 1, len(arr_ts)):
                if bbo[t_fav][qq][2] != ask0:
                    lag = arr_ts[qq] - PM_DELIVERY - T
                    new_mid = (bbo[t_fav][qq][1] + bbo[t_fav][qq][2]) / 2
                    break
            st["lag"].append(lag)
            t_hit = np.inf
            if t_fav in pr_ts:
                s = int(np.searchsorted(pr_ts[t_fav], T))
                for qq in range(s, len(pr_ts[t_fav])):
                    tq, pq, sq = prints[t_fav][qq]
                    if tq > T + (lag if np.isfinite(lag) else 5.0) + 0.5:
                        break
                    if sq == "BUY" and pq <= ask0 + 1e-9:
                        t_hit = tq - T
                        break
            if t_hit < lag:
                st["hit_before_reprice"] += 1
                st["hit_t"].append(t_hit)
            won = tok[t_fav][2]
            fee = 0.07 * ask0 * (1 - ask0)
            pnl = (1 - ask0 - fee) if won else (-ask0 - fee)
            for L in L_TOTALS:
                if lag > L and t_hit > L:
                    b = st["per_L"][L]
                    b["n"] += 1
                    b["wins"] += int(won)
                    b["pnl"] += pnl
                    if new_mid is not None:
                        b["mtm"] += new_mid - ask0 - fee
                        b["mtm_n"] += 1
    print(f"{day}: ticks {len(px)}, bbo tokens {len(bbo)}, done", flush=True)


def q(xs, f):
    xs = sorted(x for x in xs if np.isfinite(x))
    return round(xs[min(int(f * len(xs)), len(xs) - 1)], 3) if xs else None


print(f"\n=== Binance to Polymarket stale-quote race, {day_n} days, final 90 s, "
      f"PM delivery {PM_DELIVERY}s ===")
for J, st in stats.items():
    print(f"\n-- jump >= {J:.0f} USD in 300 ms: events {st['events']} ({st['events']/day_n:.0f}/day), "
          f"in final 90 s {st['in_window']}, usable (fav ask in (0.03,0.97)) {st['usable']} "
          f"[k<=25: {st['k_le25']}]")
    print(f"   reprice lag q25/50/75: {q(st['lag'],.25)}/{q(st['lag'],.5)}/{q(st['lag'],.75)} s; "
          f"never repriced in-window: {sum(1 for x in st['lag'] if not np.isfinite(x))}")
    print(f"   competing taker hit the stale ask BEFORE reprice: {st['hit_before_reprice']}/{st['usable']} "
          f"(hit time q50 {q(st['hit_t'],.5)} s)")
    print(f"   {'L(s)':>5} {'fillable':>8} {'/day':>6} {'win%':>6} {'EW c/sh':>8} {'MTM c/sh':>9}")
    for L, b in st["per_L"].items():
        if b["n"]:
            mtm = 100 * b["mtm"] / b["mtm_n"] if b["mtm_n"] else float("nan")
            print(f"   {L:5.2f} {b['n']:8d} {b['n']/day_n:6.1f} {100*b['wins']/b['n']:6.1f} "
                  f"{100*b['pnl']/b['n']:8.2f} {mtm:9.2f}")
        else:
            print(f"   {L:5.2f} {0:8d}")
json.dump({str(J): {k: (v if k != "per_L" else {str(L): b for L, b in v.items()})
                    for k, v in st.items() if k not in ("lag", "hit_t")}
           for J, st in stats.items()}, open(D / "r21_race.json", "w"), indent=1)
print("saved r21_race.json")
