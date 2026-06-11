"""Adversarial re-derivation of p3-mispricing headline numbers.

Independent implementation (NOT copied from phase3_mispricing.py):
  Q1 cross-book sum distribution over all stamped decisions (trades+ghosts)
  Q2 near-expiry extreme-price fade (>=0.85/<90s and >=0.90/<60s)
  Q3 depth_usd_top20 quartile signed edge
Run: python scripts/goal_eval/verify_p3_mispricing.py
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")

MEM = Path(__file__).resolve().parent.parent.parent / "polybot" / "memory"
ET = ZoneInfo("America/New_York")
FEE = 0.07


def load_records(dirname: str) -> list[dict]:
    out = []
    for f in sorted((MEM / dirname).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def et_day(ts: str) -> str:
    from datetime import datetime
    return datetime.fromisoformat(ts).astimezone(ET).strftime("%Y-%m-%d")


def dedupe_by_pid(recs: list[dict], ts_key: str = "timestamp") -> tuple[list[dict], int]:
    best: dict[int, dict] = {}
    for r in recs:
        pid = r.get("position_id")
        if pid is None:
            continue
        cur = best.get(pid)
        if cur is None or (r.get(ts_key) or "") >= (cur.get(ts_key) or ""):
            best[pid] = r
    return list(best.values()), len(recs) - len(best)


def day_t(rows: list[tuple[str, float]]) -> tuple[float, int]:
    """rows: (day, value). t on per-day means, population stdev."""
    by_day = defaultdict(list)
    for d, v in rows:
        by_day[d].append(v)
    means = [statistics.fmean(vs) for vs in by_day.values()]
    n = len(means)
    if n < 2:
        return float("nan"), n
    sd = statistics.pstdev(means)
    if sd == 0:
        return float("nan"), n
    return statistics.fmean(means) / (sd / math.sqrt(n)), n


def day_boot(rows: list[tuple[str, float]]) -> tuple[float, float, float]:
    by_day = defaultdict(list)
    for d, v in rows:
        by_day[d].append(v)
    days = sorted(by_day)
    random.seed(42)
    stats = []
    for _ in range(1000):
        sample = random.choices(days, k=len(days))
        vals = [v for d in sample for v in by_day[d]]
        stats.append(statistics.fmean(vals))
    stats.sort()
    return statistics.fmean(stats), stats[int(0.10 * len(stats))], stats[int(0.90 * len(stats))]


def pct(sorted_vals: list[float], q: float) -> float:
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def main() -> None:
    trades_raw = load_records("outcomes")
    ghosts_raw = load_records("ghost_outcomes")
    cfs_raw = load_records("counterfactuals")

    trades, tr_dropped = dedupe_by_pid(trades_raw)
    cfs, cf_dropped = dedupe_by_pid(cfs_raw)
    # ghosts have no position_id; dedupe on identity tuple
    seen, ghosts = set(), []
    for g in ghosts_raw:
        key = (g.get("timestamp"), g.get("market_id"), g.get("gate_name"), g.get("side"))
        if key in seen:
            continue
        seen.add(key)
        ghosts.append(g)
    gh_dropped = len(ghosts_raw) - len(ghosts)
    ghosts_res = [g for g in ghosts if g.get("resolved")]

    print(f"raw counts: trades={len(trades_raw)} ghosts={len(ghosts_raw)} cfs={len(cfs_raw)}")
    print(f"deduped:    trades={len(trades)} (-{tr_dropped})  ghosts={len(ghosts)} (-{gh_dropped}, "
          f"resolved={len(ghosts_res)}, unresolved={len(ghosts) - len(ghosts_res)})  cfs={len(cfs)} (-{cf_dropped})")

    # ---- window-winner recovery -------------------------------------------
    cf_winner: dict[int, bool] = {}   # chosen side won the window
    for c in cfs:
        a = c.get("actual") or {}
        if a.get("exit_reason") == "hold":
            xp = a.get("exit_price")
            if xp in (0.0, 1.0):
                cf_winner[c["position_id"]] = xp == 1.0
        else:
            rp = (c.get("counterfactual") or {}).get("resolution_price")
            if rp in (0.0, 1.0):
                cf_winner[c["position_id"]] = rp == 1.0

    scalp_no_cf = 0
    decisions = []   # dict(day, pu, pd, won_up(bool|None), src, depth, chosen, chosen_p)
    for t in trades:
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        pu, pd = ctx.get("market_price_up"), ctx.get("market_price_down")
        if pu is None or pd is None:
            continue
        if t.get("exit_reason") == "resolution":
            chosen_won = bool(t.get("correct"))
        elif t.get("position_id") in cf_winner:
            chosen_won = cf_winner[t["position_id"]]
        else:
            scalp_no_cf += 1
            chosen_won = None
        won_up = None if chosen_won is None else (chosen_won if t["side"] == "Up" else not chosen_won)
        decisions.append(dict(day=et_day(t["timestamp"]), pu=pu, pd=pd, won_up=won_up,
                              src="trade", depth=ctx.get("depth_usd_top20"),
                              sec=ctx.get("seconds_remaining")))
    for g in ghosts_res:
        ctx = (g.get("indicator_snapshot") or {}).get("trade_context") or {}
        pu, pd = ctx.get("market_price_up"), ctx.get("market_price_down")
        if pu is None or pd is None:
            continue
        chosen_won = bool(g["ghost_correct"])
        won_up = chosen_won if g["side"] == "Up" else not chosen_won
        decisions.append(dict(day=et_day(g["timestamp"]), pu=pu, pd=pd, won_up=won_up,
                              src="ghost", depth=ctx.get("depth_usd_top20"),
                              sec=ctx.get("seconds_remaining")))

    n_tr = sum(d["src"] == "trade" for d in decisions)
    n_gh = sum(d["src"] == "ghost" for d in decisions)
    print(f"\ndecisions with both prices stamped: {len(decisions)} (trades {n_tr}, ghosts {n_gh})")
    print(f"scalped trades with no CF winner: {scalp_no_cf}")

    # ---- Q1: cross-book sum -----------------------------------------------
    sums = sorted(d["pu"] + d["pd"] for d in decisions)
    n = len(sums)
    print("\n== Q1 sum s = ask_up + ask_down ==")
    for q in (0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99):
        print(f"  p{int(q*100):>2}: {pct(sums, q):.4f}")
    print(f"  min={sums[0]:.4f} max={sums[-1]:.4f}")
    below = sum(s < 0.98 for s in sums)
    above = sum(s > 1.02 for s in sums)
    print(f"  below 0.98: {below}/{n}  above 1.02: {above}/{n}")
    arbs = locked = 0
    for d in decisions:
        s = d["pu"] + d["pd"]
        fee = FEE * (d["pu"] * (1 - d["pu"]) + d["pd"] * (1 - d["pd"]))
        profit = 1.0 - s - fee
        if profit > 0:
            arbs += 1
            locked += profit
    print(f"  buy-both arbs after fee: {arbs}  locked profit per 1-share pair total: {locked:.4f}")
    spreads = sorted(s - 1.0 for s in sums)
    print(f"  spread proxy (s-1) median: {statistics.median(spreads):.4f}")

    # ---- Q2: extreme-price fade pool --------------------------------------
    def build_pool(p_thresh: float, sec_thresh: float):
        pool = []  # (day, ext_price, ext_won(bool), cheap_cost, src)
        for d in decisions:
            sec = d["sec"]
            if sec is None or sec >= sec_thresh or d["won_up"] is None:
                continue
            for ext_p, cheap_p, ext_won in ((d["pu"], d["pd"], d["won_up"]),
                                            (d["pd"], d["pu"], not d["won_up"])):
                if ext_p is not None and ext_p >= p_thresh:
                    pool.append((d["day"], ext_p, ext_won, cheap_p, d["src"]))
        for c in cfs:
            a = c.get("actual") or {}
            ctx = c.get("context_at_worst_moment") if a.get("exit_reason") == "hold" \
                else c.get("context_at_scalp")
            if not ctx:
                continue
            mp, sec = ctx.get("market_price"), ctx.get("seconds_remaining")
            if mp is None or sec is None or sec >= sec_thresh:
                continue
            won = cf_winner.get(c["position_id"])
            if won is None:
                continue
            if mp >= p_thresh:
                pool.append((et_day(c["timestamp"]), mp, won, 1 - mp + 0.01, "cf"))
            elif (1 - mp) >= p_thresh:
                pool.append((et_day(c["timestamp"]), 1 - mp, not won, mp + 0.01, "cf"))
        return pool

    def fade_stats(label: str, pool):
        if not pool:
            print(f"\n== Q2 {label}: EMPTY ==")
            return
        n = len(pool)
        wr = statistics.fmean(1.0 if w else 0.0 for _, _, w, _, _ in pool)
        mp = statistics.fmean(p for _, p, _, _, _ in pool)
        mix = defaultdict(int)
        for *_, src in pool:
            mix[src] += 1
        cheap_costs = [q for *_, q, _ in pool]
        nets = [(d, ((0.0 if w else 1.0) / q) - 1.0 - FEE * (1 - q))
                for d, _, w, q, _ in pool]
        net = statistics.fmean(v for _, v in nets)
        t, nd = day_t(nets)
        bm, b10, b90 = day_boot(nets)
        cheap_wr = 1 - wr
        mean_cost = statistics.fmean(cheap_costs)
        # breakeven cheap-side WR: solve wr_be/E[1/q-ish] ... report simple: cost*(1+fee terms)
        be = statistics.fmean(q * (1 + FEE * (1 - q)) for q in cheap_costs)
        print(f"\n== Q2 {label} ==")
        print(f"  n={n}  mix={dict(mix)}  days={nd}")
        print(f"  extreme WR={wr:.4f}  mean ext price={mp:.4f}  gap={wr - mp:+.4f}")
        print(f"  cheap-side WR={cheap_wr:.4f}  mean cost={mean_cost:.4f}  breakeven~{be:.4f}")
        print(f"  fade net per $1: {net:+.4f}   day-clustered t={t:.2f} (n_days={nd})")
        print(f"  bootstrap mean={bm:+.4f}  p10={b10:+.4f}  p90={b90:+.4f}")

    fade_stats(">=0.85 & <90s", build_pool(0.85, 90))
    fade_stats(">=0.90 & <60s", build_pool(0.90, 60))

    # ---- Q3: depth quartiles ----------------------------------------------
    print("\n== Q3 depth_usd_top20 quartiles (signed edge = won - chosen ask... using won_up vs up price? No: chosen-side) ==")
    pool = [d for d in decisions if d["won_up"] is not None and d.get("depth") and d["depth"] > 0]
    cov = len([d for d in decisions if d.get("depth") and d["depth"] > 0]) / len(decisions)
    print(f"  depth>0 coverage over all decisions: {cov:.3f}  usable (winner known): {len(pool)}")
    n_tr_d = sum(d["src"] == "trade" for d in pool)
    print(f"  pool mix: trades={n_tr_d} ghosts={len(pool) - n_tr_d}")
    pool.sort(key=lambda d: d["depth"])
    k = len(pool) // 4
    for i in range(4):
        q = pool[i * k: (i + 1) * k if i < 3 else len(pool)]
        # signed edge of UP side? use chosen... we kept won_up; chosen-side info lost.
        # Recompute chosen-side edge: stored? fall back: evaluate both sides symmetric ->
        # won_up - pu  ==  -(won_down - pd) + (1 - s)... use UP-side and note.
        rows = [(d["day"], (1.0 if d["won_up"] else 0.0) - d["pu"]) for d in q]
        e = statistics.fmean(v for _, v in rows)
        t, nd = day_t(rows)
        lo, hi = q[0]["depth"], q[-1]["depth"]
        print(f"  Q{i+1}: n={len(q)}  depth [{lo:,.0f}..{hi:,.0f}]  UP-side edge={e:+.4f}  t={t:.2f} (days={nd})")
    print("  NOTE: UP-side edge used above; rerun below with CHOSEN-side edge")

    # chosen-side variant (need chosen flag): rebuild quickly
    pool2 = []
    for t_ in trades:
        ctx = (t_.get("indicator_snapshot") or {}).get("trade_context") or {}
        pu, pd = ctx.get("market_price_up"), ctx.get("market_price_down")
        dep = ctx.get("depth_usd_top20")
        if pu is None or pd is None or not dep or dep <= 0:
            continue
        if t_.get("exit_reason") == "resolution":
            won = bool(t_.get("correct"))
        elif t_.get("position_id") in cf_winner:
            won = cf_winner[t_["position_id"]]
        else:
            continue
        price = pu if t_["side"] == "Up" else pd
        pool2.append((et_day(t_["timestamp"]), dep, (1.0 if won else 0.0) - price))
    for g in ghosts_res:
        ctx = (g.get("indicator_snapshot") or {}).get("trade_context") or {}
        pu, pd = ctx.get("market_price_up"), ctx.get("market_price_down")
        dep = ctx.get("depth_usd_top20")
        if pu is None or pd is None or not dep or dep <= 0:
            continue
        price = pu if g["side"] == "Up" else pd
        pool2.append((et_day(g["timestamp"]), dep, (1.0 if g["ghost_correct"] else 0.0) - price))
    pool2.sort(key=lambda r: r[1])
    print(f"\n  CHOSEN-side signed edge, same quartiles (n={len(pool2)}):")
    k = len(pool2) // 4
    for i in range(4):
        q = pool2[i * k: (i + 1) * k if i < 3 else len(pool2)]
        rows = [(d, v) for d, _, v in q]
        e = statistics.fmean(v for _, v in rows)
        t, nd = day_t(rows)
        print(f"  Q{i+1}: n={len(q)}  depth [{q[0][1]:,.0f}..{q[-1][1]:,.0f}]  edge={e:+.4f}  t={t:.2f} (days={nd})")

    days_all = sorted({d["day"] for d in decisions})
    print(f"\nET days in decision pool: {len(days_all)}  ({days_all[0]} .. {days_all[-1]})")


if __name__ == "__main__":
    main()
