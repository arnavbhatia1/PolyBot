"""Test MANY late-window edge hypotheses on the dense collected dataset
(scripts/collect_late_window.py → late_window_collect.db).

One rich dataset, many edges. Each candidate is a bot-FORMABLE signal evaluated
lookahead-safe: decide at sample i (info <= t_i), fill at the ask one sample
later (~0.15-0.2s) unconditionally (miss only on recorder gap or ask >= $1),
settle at resolution net of the 0.07*p*(1-p) fee, ONE entry per window.

Stats: window-block-bootstrap p10 (each WINDOW is the resample unit — valid for a
per-window MICROSTRUCTURAL edge) + by-hour clustering t (robustness). A DIRECTIONAL
edge still needs day-spread; a microstructural one does not — both are reported so
the distinction is visible. CONTROL (spot-side@ask, no filter) must be ~0 (G-M);
ORACLE (buy eventual winner) is the upper bound.

  python scripts/analyze_collected_edges.py
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "polybot" / "db" / "late_window_collect.db"
ET = timezone(timedelta(hours=-4))
FEE = 0.07
LATE = 240.0
FILL_GAP_MAX = 0.5
MIN_FILLS = 30


def fee(p): return FEE * p * (1 - p)


def tstat(xs):
    n = len(xs)
    if n < 2:
        return (statistics.mean(xs) if xs else float("nan"), float("nan"), n)
    m = statistics.mean(xs); sd = statistics.stdev(xs)
    return (m, (m / (sd / math.sqrt(n)) if sd > 0 else float("nan")), n)


def boot_p10(vals, iters=3000):
    """Window-block bootstrap: resample the per-window nets with replacement."""
    if len(vals) < 3:
        return float("nan")
    n = len(vals); seed = 98765; means = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            s += vals[seed % n]
        means.append(s / n)
    means.sort()
    return means[int(0.10 * len(means))]


def load():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    labels = {r["window_id"]: (r["resolved_up"], r["price_to_beat"])
              for r in c.execute("SELECT * FROM lw_labels")}
    wins = defaultdict(list)
    for r in c.execute("SELECT * FROM lw_samples ORDER BY window_id, ts"):
        if r["window_id"] in labels:
            wins[r["window_id"]].append(dict(r))
    c.close()
    return wins, labels


def cb_move(rows, i, lb=2.0):
    t = rows[i]["ts"]; cb = rows[i]["coinbase"]
    if cb is None:
        return None
    j = i
    while j > 0 and rows[j]["ts"] > t - lb:
        j -= 1
    p = rows[j]["coinbase"]
    return None if p is None else cb - p


def bn_move(rows, i, lb=2.0):
    t = rows[i]["ts"]; bn = rows[i]["binance"]
    if bn is None:
        return None
    j = i
    while j > 0 and rows[j]["ts"] > t - lb:
        j -= 1
    p = rows[j]["binance"]
    return None if p is None else bn - p


def _move(rows, i, col, lb):
    t = rows[i]["ts"]; v = rows[i].get(col)
    if v is None:
        return None
    j = i
    while j > 0 and rows[j]["ts"] > t - lb:
        j -= 1
    p = rows[j].get(col)
    return None if p is None else v - p


def fill_ask(rows, i, up):
    """Arrival fill: the ask one sample (~150ms) after the decision, taken
    unconditionally — missed only on a recorder gap or an unfillable ask
    (None / >= $1). Conditioning the fill on where the ask moved (bands,
    reprice-skips) selects on the future path and inflates the edge."""
    if i + 1 >= len(rows):
        return None
    if rows[i + 1]["ts"] - rows[i]["ts"] > FILL_GAP_MAX:
        return None
    fa = rows[i + 1]["ask_up"] if up else rows[i + 1]["ask_dn"]
    if fa is None or fa >= 1.0:
        return None
    return fa


def evaluate(wins, labels, sig, ask_cap=0.92):
    per_win = []           # net per share, one per window
    by_hour = defaultdict(list)
    wr = []
    for wid, rows in wins.items():
        resolved_up, strike = labels[wid]
        if strike is None:
            continue
        for i in range(len(rows)):
            if rows[i]["elapsed"] < LATE:
                continue
            up = sig(rows, i, strike)
            if up is None:
                continue
            a = rows[i]["ask_up"] if up else rows[i]["ask_dn"]
            if a is None or a > ask_cap:
                continue
            fa = fill_ask(rows, i, up)
            if fa is None:
                continue
            win = 1.0 if (up == (resolved_up == 1)) else 0.0
            net = win - fa - fee(fa)
            per_win.append(net); wr.append(win)
            by_hour[datetime.fromtimestamp(rows[i]["ts"], ET).strftime("%m-%d %H")].append(net)
            break
    if len(per_win) < 2:
        return None
    hourly = [statistics.mean(v) for v in by_hour.values()]
    m, t, _ = tstat(hourly)
    return dict(n=len(per_win), net=statistics.mean(per_win), win=statistics.mean(wr),
                p10=boot_p10(per_win), hours=len(hourly), mean_hr=m, t_hr=t,
                net_sum=sum(per_win))


# ---- candidate signals: (rows, i, strike) -> side_up bool or None ----
def momentum(rows, i, strike):           # Coinbase move vs CLOB lag
    mv = cb_move(rows, i); cb = rows[i]["coinbase"]
    if mv is None or cb is None or abs(mv) < 8:
        return None
    up = mv > 0
    return up if ((up and cb > strike) or ((not up) and cb < strike)) else None


def orderflow(rows, i, strike):          # Binance CVD direction
    cvd = rows[i]["bn_cvd10"]
    if cvd is None or abs(cvd) < 1.5:
        return None
    return cvd > 0


def lead(rows, i, strike):               # Binance moved more than Coinbase (lead), in last 2s
    bm, cm = bn_move(rows, i), cb_move(rows, i)
    if bm is None or cm is None or abs(bm) < 8:
        return None
    if abs(bm) <= abs(cm):               # only when Binance is ahead of Coinbase's move
        return None
    return bm > 0


def bookimbalance(rows, i, strike):      # CLOB book pressure on the up token
    try:
        bu = json.loads(rows[i]["book_up"]);
    except Exception:
        return None
    bidsz = sum(s for _, s in bu.get("b", [])); asksz = sum(s for _, s in bu.get("a", []))
    if bidsz + asksz < 200:
        return None
    imb = (bidsz - asksz) / (bidsz + asksz)
    if abs(imb) < 0.5:
        return None
    return imb > 0                        # heavy bid pressure on UP -> buy UP


def perp_lead(rows, i, strike):          # Binance PERP moved more than Coinbase (lead)
    pm, cm = _move(rows, i, "perp_price", 2.0), cb_move(rows, i, 2.0)
    if pm is None or cm is None or abs(pm) < 8 or abs(pm) <= abs(cm):
        return None
    return pm > 0


def pm_flow(rows, i, strike):            # aggressive PM-CLOB taker flow toward a side
    up, dn = rows[i].get("pm_net_up"), rows[i].get("pm_net_dn")
    if up is None or dn is None:
        return None
    if up >= 300 and up > dn:
        return True
    if dn >= 300 and dn > up:
        return False
    return None


def depth_imb(rows, i, strike):          # Binance spot book imbalance (bid pressure)
    imb = rows[i].get("bn_depth_imb")
    if imb is None or abs(imb) < 0.4:
        return None
    return imb > 0


def chainlink_lead(rows, i, strike):     # the RESOLUTION feed past strike vs CLOB lag
    cl = rows[i].get("chainlink_px")
    if cl is None or strike is None or abs(cl - strike) < 5:
        return None
    return cl > strike


def _term_run(wins, labels, min_dist):
    """Terminal-determinism with an optional DEEP-distance flip-avoidance filter
    (|coinbase-strike| >= min_dist). One bet/window: buy the book-favorite at the next
    ~150ms sample's ask if it's still <1.0 (else a $0 MISS = fillability); settle
    authoritative, net of fee. min_dist>0 excludes flip-prone near-strike windows."""
    per_day = defaultdict(list); nets = []; n_fire = n_fill = flips = 0
    for wid, rows in wins.items():
        resolved_up, strike = labels[wid]
        if strike is None:
            continue
        for i in range(len(rows)):
            r = rows[i]
            if r["elapsed"] < 296:
                continue
            au, ad, cb = r["ask_up"], r["ask_dn"], r.get("coinbase")
            if au is None or ad is None:
                continue
            if min_dist > 0 and (cb is None or abs(cb - strike) < min_dist):
                continue
            bu, bd = r.get("bid_up"), r.get("bid_dn")
            up_fav = ((bu or au) + au) / 2 > ((bd or ad) + ad) / 2
            fav_ask = au if up_fav else ad
            if fav_ask is None or not (0.96 <= fav_ask <= 0.99):
                continue
            n_fire += 1
            if i + 1 >= len(rows) or rows[i + 1]["ts"] - r["ts"] > FILL_GAP_MAX:
                break
            fa = rows[i + 1]["ask_up"] if up_fav else rows[i + 1]["ask_dn"]
            if fa is None or fa >= 1.0 or fa > fav_ask + 0.01:
                break
            n_fill += 1
            win = 1.0 if (up_fav == (resolved_up == 1)) else 0.0
            if win == 0.0:
                flips += 1
            nets.append(win - fa - fee(fa))
            per_day[datetime.fromtimestamp(r["ts"], ET).strftime("%m-%d")].append(nets[-1])
            break
    return per_day, nets, n_fire, n_fill, flips


def terminal_determinism(wins, labels):
    """Tick-floor terminal pinning + the DEEP-distance flip-avoidance refinement: the
    catastrophic late-flips (the only thing that broke this edge) live in near-strike
    windows; a favorite >=$15 past strike with ~5s left can't flip, so the deep filter
    removes the -$0.99 tail while keeping the tick-floor gap. KILL BAR: net>0 AND
    boot_p10>0 AND t_day>=2 AND fill_rate>=80% over >=8 clean days AND flips stay rare."""
    print("\n=== TERMINAL-DETERMINISM (elapsed>=296, fav ask 0.96-0.99) — all vs DEEP-distance ===")
    for md, name in [(0.0, "all distances"), (15.0, "DEEP |cb-strike|>=$15"), (25.0, "DEEP >=$25")]:
        per_day, nets, fire, fill, flips = _term_run(wins, labels, md)
        if len(nets) < 2:
            print(f"  {name:24} fires={fire} fills={len(nets)} (accruing)"); continue
        daily = [statistics.mean(v) for v in per_day.values()]
        m, t, _ = tstat(daily)
        print(f"  {name:24} fires={fire:4d} fills={fill:4d} fill%={fill/fire if fire else 0:.0%} "
              f"flips={flips} net/sh={statistics.mean(nets):+.4f} p10={boot_p10(nets):+.4f} "
              f"days={len(daily)} t_day={t:+.2f} days+={sum(1 for d in daily if d>0)}/{len(daily)}")
    print("  (deep filter = flip-avoidance; the near-strike windows carry the -$0.99 tail)")


def control(rows, i, strike):            # G-M sanity: spot side at ask, no filter
    cb = rows[i]["coinbase"]
    return None if cb is None else (cb > strike)


def main():
    if not DB.exists():
        print("no collected DB yet — run scripts/collect_late_window.py first.")
        return
    wins, labels = load()
    labeled = {w for w in wins if labels.get(w, (None, None))[1] is not None}
    total = sum(len(v) for v in wins.values())
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    n_lab = c.execute("SELECT COUNT(*) FROM lw_labels").fetchone()[0]
    n_win = c.execute("SELECT COUNT(DISTINCT window_id) FROM lw_samples").fetchone()[0]
    c.close()
    print(f"collected: {n_win} windows ({total} samples), {n_lab} labeled, "
          f"{len(labeled)} labeled+sampled with a strike")
    if len(labeled) < MIN_FILLS:
        print(f"\nNOT ENOUGH YET: need >= ~{MIN_FILLS} labeled windows for a read "
              f"(~{MIN_FILLS//12}-{MIN_FILLS//6}h of collection; overnight for a robust one). "
              f"Collector keeps running; re-run this anytime.")
        return

    sigs = [("momentum (cb move vs lag)", momentum), ("orderflow (binance CVD)", orderflow),
            ("lead (binance>coinbase move)", lead), ("book imbalance (UP)", bookimbalance),
            ("perp-lead (futures>cb move)", perp_lead), ("pm-flow (clob tape)", pm_flow),
            ("bn-depth-imb", depth_imb), ("chainlink-lead", chainlink_lead),
            ("CONTROL spot-side@ask", control)]
    print(f"\n{'edge':>30} {'fills':>6} {'win%':>6} {'net/sh':>8} {'net_sum':>8} "
          f"{'boot_p10':>9} {'hrs':>4} {'t_hr':>6}  verdict")
    for name, fn in sigs:
        r = evaluate(wins, labels, fn)
        if not r:
            print(f"{name:>30}   (no fills)"); continue
        real = (not name.startswith("CONTROL")
                and r["p10"] > 0 and r["n"] >= MIN_FILLS and r["t_hr"] >= 2)
        v = "EDGE?" if real else ("--" if name.startswith("CONTROL") else "no")
        print(f"{name:>30} {r['n']:>6} {r['win']:>6.1%} {r['net']:>+8.4f} {r['net_sum']:>+8.2f} "
              f"{r['p10']:>+9.4f} {r['hours']:>4} {r['t_hr']:>+6.2f}  [{v}]")
    terminal_determinism(wins, labels)

    print("\nREAD: an edge needs net/sh>0, boot_p10>0 (window-bootstrap), and ideally t_hr>=2."
          "A microstructural edge survives the window-bootstrap; a DIRECTIONAL one ALSO needs "
          "day-spread (collect across days). CONTROL ~0 confirms G-M. Thresholds are fixed; "
          "the binding test is the same threshold holding on held-out windows.")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
