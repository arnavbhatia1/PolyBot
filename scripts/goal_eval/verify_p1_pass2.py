"""Pass 2: replicate the claimed script's exact conventions where pass 1 diverged,
to separate arithmetic errors from convention differences.
- edge = model_probability - entry_price (fill), buckets + slope (>=10 trades/day)
- bootstrap: random.seed(42) per group, random.choices, p10=idx100, p90=idx899
- fee sanity: first 20 resolution trades in raw glob/insertion order
- dup-CF forensics: their dedupe rule; hold-arm pnl vs neighbor outcomes (wide net)
Run: python scripts/goal_eval/verify_p1_pass2.py
"""
from __future__ import annotations

import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")
MEM = Path(__file__).resolve().parents[2] / "polybot" / "memory"
ET = ZoneInfo("America/New_York")


def load_records(dirname: str) -> list[dict]:
    out = []
    for f in sorted((MEM / dirname).glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def main() -> None:
    raw_outcomes = load_records("outcomes")
    raw_cfs = load_records("counterfactuals")

    # their dedupe (insertion order, >= replaces)
    best: dict[int, dict] = {}
    for r in raw_outcomes:
        pid = r.get("position_id")
        prev = best.get(pid)
        if prev is None or (r.get("timestamp") or "") >= (prev.get("timestamp") or ""):
            best[pid] = r
    trades = list(best.values())

    cf_winner: dict[int, bool] = {}
    for c in raw_cfs:
        if (c.get("actual") or {}).get("exit_reason") != "hold":
            rp = (c.get("counterfactual") or {}).get("resolution_price")
            if rp in (0.0, 1.0):
                cf_winner[c["position_id"]] = rp == 1.0

    for t in trades:
        ts = t["timestamp"]
        t["_day"] = datetime.fromisoformat(ts).astimezone(ET).strftime("%Y-%m-%d")
        if t.get("exit_reason") == "resolution":
            t["_won"] = bool(t.get("correct"))
        elif t.get("position_id") in cf_winner:
            t["_won"] = cf_winner[t["position_id"]]
        else:
            t["_won"] = None
    all_days = sorted({t["_day"] for t in trades})

    def boot(group):
        by_day = defaultdict(float)
        for r in group:
            by_day[r["_day"]] += r["pnl"]
        random.seed(42)
        stats = []
        for _ in range(1000):
            sampled = random.choices(all_days, k=len(all_days))
            stats.append(sum(by_day.get(d, 0.0) for d in sampled))
        stats.sort()
        return sum(stats) / 1000, stats[100], stats[899]

    print("== bootstrap, their RNG convention ==")
    for label, sel in [
        ("TOTAL", trades),
        ("scalp", [t for t in trades if t["exit_reason"] == "scalp"]),
        ("resolution", [t for t in trades if t["exit_reason"] == "resolution"]),
        ("agree", [t for t in trades if t["_won"] is True]),
        ("disagree", [t for t in trades if t["_won"] is False]),
    ]:
        m, p10, p90 = boot(sel)
        print(f"  {label:>10}: mean={m:+.2f} p10={p10:+.2f} p90={p90:+.2f}")

    # ---- buckets + slope, edge = p - entry_price(fill) ----------------------
    def tc(t):
        return (t.get("indicator_snapshot") or {}).get("trade_context") or {}

    print("\n== buckets, edge = model_probability - entry_price (their def) ==")
    buckets = [(-9, 0, "<0"), (0, 0.02, "0-.02"), (0.02, 0.04, ".02-.04"),
               (0.04, 0.07, ".04-.07"), (0.07, 0.12, ".07-.12"), (0.12, 9, ">=.12")]
    for field in ("model_probability", "model_probability_raw"):
        rows = [(tc(t)[field] - t["entry_price"], t) for t in trades
                if tc(t).get(field) is not None and t.get("entry_price") is not None]
        print(f" [{field}] n={len(rows)}")
        for lo, hi, lab in buckets:
            b = [(e, t) for e, t in rows if lo <= e < hi]
            if not b:
                continue
            known = [t for _, t in b if t["_won"] is not None]
            wr = sum(t["_won"] for t in known) / len(known) if known else float("nan")
            me = sum(t["entry_price"] for _, t in b) / len(b)
            pnl = sum(t["pnl"] for _, t in b)
            mg = sum(t.get("gain_pct") or 0.0 for _, t in b) / len(b)
            print(f"  {lab:>8}: n={len(b):>5} wr={wr:.3f} mean_entry={me:.3f} "
                  f"net={pnl:+.2f} gain%={mg*100:+.2f}")

    def slope_test(field):
        by_day = defaultdict(list)
        for t in trades:
            p = tc(t).get(field)
            ep = t.get("entry_price")
            if p is None or ep is None or t["_won"] is None:
                continue
            by_day[t["_day"]].append((p - ep, (1.0 if t["_won"] else 0.0) - ep))
        slopes = []
        for d in sorted(by_day):
            pts = by_day[d]
            if len(pts) < 10:
                continue
            mx = sum(x for x, _ in pts) / len(pts)
            my = sum(y for _, y in pts) / len(pts)
            sxx = sum((x - mx) ** 2 for x, _ in pts)
            if sxx == 0:
                continue
            slopes.append(sum((x - mx) * (y - my) for x, y in pts) / sxx)
        sd = st.pstdev(slopes)
        return st.mean(slopes), st.mean(slopes) / (sd / math.sqrt(len(slopes))), len(slopes)

    print("\n== slope (won - entry) on (p - entry), per-day >=10, their def ==")
    for f in ("model_probability", "model_probability_raw"):
        m, t_, n = slope_test(f)
        print(f"  {f}: mean_slope={m:+.4f} t_day={t_:+.2f} n_days={n}")

    # ---- fee sanity, first 20 resolution trades in insertion order ----------
    res20 = [t for t in trades if t.get("exit_reason") == "resolution"
             and t.get("entry_price") and t.get("fees") is not None][:20]
    resid = []
    for t in res20:
        shares = t["size"] / t["entry_price"]
        resid.append((shares * (t.get("exit_price") or 0.0) - t["size"] - t["fees"]
                      - t["pnl"]) / t["size"] * 100)
    print(f"\n== fee sanity, first-20 insertion order: mean_abs={st.mean([abs(x) for x in resid]):.3f}% "
          f"max_abs={max(abs(x) for x in resid):.2f}% mean_signed={st.mean(resid):+.3f}% "
          f"pids={[t['position_id'] for t in res20][:6]}... ==")

    # ---- dup-CF forensics ----------------------------------------------------
    by_pid = defaultdict(list)
    for c in raw_cfs:
        by_pid[c["position_id"]].append(c)
    dup_pids = sorted(p for p, cs in by_pid.items() if len(cs) > 1)
    out_by_pid = {t["position_id"]: t for t in trades}

    # their dedupe rule applied to CFs: which pid ends holding the hold-arm?
    bestc: dict[int, dict] = {}
    for c in raw_cfs:
        pid = c["position_id"]
        prev = bestc.get(pid)
        if prev is None or (c.get("timestamp") or "") >= (prev.get("timestamp") or ""):
            bestc[pid] = c
    kept_hold_for_scalped = [
        p for p in dup_pids
        if (bestc[p].get("actual") or {}).get("exit_reason") == "hold"
        and out_by_pid.get(p, {}).get("exit_reason") != "resolution"
    ]
    hold_later_strict = []
    for p in dup_pids:
        holds = [c for c in by_pid[p] if (c.get("actual") or {}).get("exit_reason") == "hold"]
        scalps = [c for c in by_pid[p] if (c.get("actual") or {}).get("exit_reason") != "hold"]
        if holds and scalps and max(h["timestamp"] for h in holds) > max(s["timestamp"] for s in scalps):
            hold_later_strict.append(p)
    print(f"\n== dup CF pids: {len(dup_pids)} ==")
    print(f"  keep-latest(their rule) keeps HOLD-arm for scalped outcome: "
          f"{len(kept_hold_for_scalped)} pids={kept_hold_for_scalped}")
    print(f"  hold-arm strictly later ts: {len(hold_later_strict)} pids={hold_later_strict}")

    # hold-arm pnl vs ANY outcome pnl (mis-key hypothesis), widening windows
    matches = {1: 0, 2: 0, 5: 0, 10: 0}
    any_match = 0
    examples = []
    for p in dup_pids:
        for c in by_pid[p]:
            if (c.get("actual") or {}).get("exit_reason") != "hold":
                continue
            apnl = (c.get("actual") or {}).get("pnl")
            if apnl is None:
                continue
            found = None
            for nb_pid, nt in out_by_pid.items():
                if nb_pid != p and abs(nt["pnl"] - apnl) < 0.005:
                    d = abs(nb_pid - p)
                    found = d if found is None else min(found, d)
            if found is not None:
                any_match += 1
                for w in matches:
                    if found <= w:
                        matches[w] += 1
                if len(examples) < 5:
                    examples.append((p, found))
    # also: does the hold-arm pnl match the SAME pid's outcome (i.e. not mis-keyed)?
    same_match = 0
    for p in dup_pids:
        for c in by_pid[p]:
            if (c.get("actual") or {}).get("exit_reason") != "hold":
                continue
            apnl = (c.get("actual") or {}).get("pnl")
            t = out_by_pid.get(p)
            if t is not None and apnl is not None and abs(t["pnl"] - apnl) < 0.005:
                same_match += 1
    print(f"  hold-arm actual.pnl matches ANOTHER pid's outcome pnl (<0.005): {any_match}/298"
          f" (within +-1: {matches[1]}, +-2: {matches[2]}, +-5: {matches[5]}, +-10: {matches[10]})")
    print(f"  hold-arm actual.pnl matches its OWN pid's outcome pnl: {same_match}/298")
    print(f"  examples (pid, nearest-match distance): {examples}")
    c2806 = [c for c in by_pid.get(2806, []) if (c.get("actual") or {}).get("exit_reason") == "hold"]
    if c2806:
        t2807 = out_by_pid.get(2807)
        print(f"  spot 2806 hold-arm pnl={c2806[0]['actual']['pnl']} vs outcome 2807 pnl="
              f"{t2807['pnl'] if t2807 else None}")


if __name__ == "__main__":
    main()
