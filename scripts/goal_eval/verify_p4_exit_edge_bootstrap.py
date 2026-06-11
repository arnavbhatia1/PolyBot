"""Adversarial re-derivation of p4-exit-edge-bootstrap headline numbers.

Independent implementation (loader copied from scripts/diagnose_edge.py only).
Computes: deduped CF pool, actual vs always-hold totals, delta, day-clustered
t, day-clustered bootstrap (seed 42), fee-corrected variants, splits,
duplicate forensics, reconciliation vs outcomes.
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MEM = Path(__file__).resolve().parent.parent.parent / "polybot" / "memory"
ET = ZoneInfo("America/New_York")


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
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def dedupe(records: list[dict]) -> tuple[dict, int]:
    """Keep latest-timestamp record per position_id. Returns (by_pid, n_dropped)."""
    by_pid: dict = {}
    for r in records:
        pid = r.get("position_id")
        if pid is None:
            continue
        old = by_pid.get(pid)
        if old is None or (r.get("timestamp") or "") >= (old.get("timestamp") or ""):
            by_pid[pid] = r
    return by_pid, len(records) - len(by_pid)


def t_day(day_vals: dict[str, float]) -> tuple[float, int]:
    vals = list(day_vals.values())
    n = len(vals)
    if n < 2:
        return float("nan"), n
    m = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    if sd == 0:
        return float("nan"), n
    return m / (sd / math.sqrt(n)), n


def boot(day_recs: dict[str, list[float]], seed: int = 42, iters: int = 1000):
    days = sorted(day_recs)
    random.seed(seed)
    stats = []
    for _ in range(iters):
        sample = random.choices(days, k=len(days))
        stats.append(sum(sum(day_recs[d]) for d in sample))
    stats.sort()
    mean = statistics.mean(stats)
    p10 = stats[int(0.10 * iters)]
    p90 = stats[int(0.90 * iters)]
    return mean, p10, p90


def main() -> None:
    raw_cfs = load_records("counterfactuals")
    raw_outs = load_records("outcomes")
    print(f"raw loaded: cfs={len(raw_cfs)}  outcomes={len(raw_outs)}")

    cf_by_pid, cf_dropped = dedupe(raw_cfs)
    out_by_pid, out_dropped = dedupe(raw_outs)
    print(f"dedupe: cf_dropped={cf_dropped}  outcome_dropped={out_dropped}")
    print(f"unique: cf={len(cf_by_pid)}  outcomes={len(out_by_pid)}")

    # ---- build pool ------------------------------------------------------
    pool = []          # dicts: pid, kind, act, hold_arm, delta, day, mp, loss_cut
    excl_nonbinary = 0
    excl_nopnl = 0
    for pid, c in sorted(cf_by_pid.items()):
        actual = c.get("actual") or {}
        cf = c.get("counterfactual") or {}
        kind = "hold" if actual.get("exit_reason") == "hold" else "scalp"
        a_pnl = actual.get("pnl")
        if a_pnl is None:
            excl_nopnl += 1
            continue
        if kind == "scalp":
            rp = cf.get("resolution_price")
            if rp not in (0.0, 1.0):
                excl_nonbinary += 1
                continue
            if cf.get("pnl") is None:
                excl_nopnl += 1
                continue
            hold_arm = cf["pnl"]
            ctx = c.get("context_at_scalp") or {}
        else:
            hold_arm = a_pnl
            ctx = c.get("context_at_worst_moment") or {}
        pool.append(dict(
            pid=pid, kind=kind, act=a_pnl, hold=hold_arm, delta=a_pnl - hold_arm,
            day=et_day(c.get("timestamp") or ""), mp=ctx.get("market_price"),
            loss_cut=bool(ctx.get("loss_cut")),
            exit_price=actual.get("exit_price"),
            won=(cf.get("resolution_price") == 1.0) if kind == "scalp" else None,
        ))
    n_scalp = sum(1 for r in pool if r["kind"] == "scalp")
    n_hold = len(pool) - n_scalp
    print(f"\npool n={len(pool)} (scalp={n_scalp} hold={n_hold})  "
          f"excluded: nonbinary_res={excl_nonbinary} no_pnl={excl_nopnl}")

    actual_total = sum(r["act"] for r in pool)
    hold_total = sum(r["hold"] for r in pool)
    delta_total = actual_total - hold_total
    print(f"actual_total={actual_total:+.2f}  always_hold_total={hold_total:+.2f}  "
          f"delta={delta_total:+.2f}")
    sc = [r for r in pool if r["kind"] == "scalp"]
    print(f"scalp-only: actual={sum(r['act'] for r in sc):+.2f}  "
          f"hold={sum(r['hold'] for r in sc):+.2f}  "
          f"delta={sum(r['delta'] for r in sc):+.2f}")

    # ---- per-day delta, t, bootstrap --------------------------------------
    day_deltas: dict[str, list[float]] = defaultdict(list)
    for r in pool:
        day_deltas[r["day"]].append(r["delta"])
    day_tot = {d: sum(v) for d, v in sorted(day_deltas.items())}
    print(f"\nper-ET-day delta ({len(day_tot)} days):")
    for d, v in sorted(day_tot.items()):
        ns = sum(1 for r in pool if r["day"] == d and r["kind"] == "scalp")
        print(f"  {d}: delta={v:+9.2f}  n_scalp={ns:>4}  n_all={len(day_deltas[d]):>4}")
    t, nd = t_day(day_tot)
    pos = sum(1 for v in day_tot.values() if v > 0)
    best_d, best_v = max(day_tot.items(), key=lambda kv: kv[1])
    top3 = sorted(day_tot.items(), key=lambda kv: -kv[1])[:3]
    print(f"t_day={t:.3f} (n_days={nd})  days_positive={pos}  "
          f"best_day={best_d} {best_v:+.2f} share={best_v / delta_total:.4f}")
    print(f"top3 days: {[(d, round(v, 2)) for d, v in top3]} "
          f"sum={sum(v for _, v in top3):+.2f}")
    bm, b10, b90 = boot(day_deltas)
    print(f"bootstrap(seed42,1000): mean={bm:+.2f}  p10={b10:+.2f}  p90={b90:+.2f}  "
          f"PASS_p10>0={b10 > 0}")

    # ---- fee correction ----------------------------------------------------
    # outcomes flagged fee_restamped=0.07 but CF pnl still at old 0.018 basis:
    # delta overstates scalp arm by the extra EXIT fee (entry fee cancels;
    # resolution fee is 0 at p in {0,1}).
    fee_corr_n = 0
    fee_corr_sum = 0.0
    corr_by_pid: dict[int, float] = {}
    for r in sc:
        o = out_by_pid.get(r["pid"])
        if not o or o.get("fee_restamped") != 0.07:
            continue
        ep, size = o.get("entry_price"), o.get("size")
        xp = r["exit_price"]
        if not ep or size is None or xp is None:
            continue
        shares = size / ep
        extra = (0.07 - 0.018) * shares * xp * (1 - xp)
        corr_by_pid[r["pid"]] = extra
        fee_corr_n += 1
        fee_corr_sum += extra
    print(f"\nfee correction: n_restamped_scalps={fee_corr_n}  "
          f"extra_exit_fee_sum={fee_corr_sum:+.2f}")
    day_deltas_fc: dict[str, list[float]] = defaultdict(list)
    for r in pool:
        d = r["delta"] - corr_by_pid.get(r["pid"], 0.0)
        day_deltas_fc[r["day"]].append(d)
    day_tot_fc = {d: sum(v) for d, v in day_deltas_fc.items()}
    delta_fc = sum(day_tot_fc.values())
    t_fc, nd_fc = t_day(day_tot_fc)
    bmf, b10f, b90f = boot(day_deltas_fc)
    print(f"fee-corrected delta={delta_fc:+.2f}  t_day={t_fc:.3f} (n_days={nd_fc})")
    print(f"fee-corrected bootstrap: mean={bmf:+.2f}  p10={b10f:+.2f}  p90={b90f:+.2f}  "
          f"PASS_p10>0={b10f > 0}")

    # ---- splits ------------------------------------------------------------
    def split_stats(label, sel):
        dd: dict[str, list[float]] = defaultdict(list)
        for r in sel:
            dd[r["day"]].append(r["delta"])
        tt, nn = t_day({d: sum(v) for d, v in dd.items()})
        print(f"  {label:>16}: n={len(sel):>5}  delta={sum(r['delta'] for r in sel):+9.2f}"
              f"  t_day={tt:>6.2f} (n_days={nn})")

    print("\nsplits (scalp CFs only):")
    split_stats("profit_scalps", [r for r in sc if r["act"] >= 0])
    split_stats("loss_scalps", [r for r in sc if r["act"] < 0])
    lc = [r for r in sc if r["loss_cut"]]
    print(f"  {'loss_cut_flagged':>16}: n={len(lc)}")
    itm = [r for r in sc if r["mp"] is not None and r["mp"] >= 0.5]
    otm = [r for r in sc if r["mp"] is not None and r["mp"] < 0.5]
    no_mp = [r for r in sc if r["mp"] is None]
    split_stats("itm_at_scalp", itm)
    split_stats("otm_at_scalp", otm)
    print(f"  scalps missing market_price: {len(no_mp)}")
    lc_outcomes = sum(1 for o in out_by_pid.values() if o.get("exit_reason") == "loss_cut")
    print(f"  outcomes with exit_reason=='loss_cut': {lc_outcomes}")

    # ---- no-dedupe replication of prior-session levels ---------------------
    a_nd = h_nd = 0.0
    for c in raw_cfs:
        actual = c.get("actual") or {}
        cf = c.get("counterfactual") or {}
        a_pnl = actual.get("pnl")
        if a_pnl is None:
            continue
        if actual.get("exit_reason") == "hold":
            a_nd += a_pnl
            h_nd += a_pnl
        else:
            if cf.get("resolution_price") not in (0.0, 1.0) or cf.get("pnl") is None:
                continue
            a_nd += a_pnl
            h_nd += cf["pnl"]
    print(f"\nNO-dedupe: actual={a_nd:+.2f}  hold={h_nd:+.2f}  delta={a_nd - h_nd:+.2f}")

    # ---- reconciliation vs outcomes ----------------------------------------
    total_out_pnl = sum(o.get("pnl") or 0.0 for o in out_by_pid.values())
    print(f"\nall deduped outcomes: n={len(out_by_pid)}  total_pnl={total_out_pnl:+.2f}")
    no_cf = [o for pid, o in out_by_pid.items() if pid not in cf_by_pid]
    print(f"outcomes without CF: n={len(no_cf)}  pnl={sum(o.get('pnl') or 0 for o in no_cf):+.2f}  "
          f"(resolutions={sum(1 for o in no_cf if o.get('exit_reason') == 'resolution')}, "
          f"other={sum(1 for o in no_cf if o.get('exit_reason') != 'resolution')})")

    gap_total = 0.0
    n_nonzero = n_feegap = 0
    feegap_sum = other_sum = 0.0
    other_pids = []
    for r in pool:
        o = out_by_pid.get(r["pid"])
        if not o or o.get("pnl") is None:
            continue
        gap = r["act"] - o["pnl"]
        gap_total += gap
        if abs(gap) <= 0.005:
            continue
        n_nonzero += 1
        fees = o.get("fees") or 0.0
        expected = fees * (1 - 0.018 / 0.07)
        if abs(gap - expected) <= 0.01:
            n_feegap += 1
            feegap_sum += gap
        else:
            other_sum += gap
            other_pids.append((r["pid"], round(gap, 2)))
    print(f"reconciliation gap (cf.actual.pnl - outcome.pnl over matched pids): "
          f"{gap_total:+.2f}")
    print(f"  nonzero gaps={n_nonzero}; fee-restamp-exact={n_feegap} sum={feegap_sum:+.2f}; "
          f"other={n_nonzero - n_feegap} sum={other_sum:+.2f}")
    print(f"  other pids: {other_pids[:15]}")

    # ---- duplicate forensics ------------------------------------------------
    dup_groups = defaultdict(list)
    for c in raw_cfs:
        pid = c.get("position_id")
        if pid is not None:
            dup_groups[pid].append(c)
    dups = {pid: rs for pid, rs in dup_groups.items() if len(rs) > 1}
    n_pairs = sum(len(rs) - 1 for rs in dups.values())
    hold_scalp_pairs = 0
    hold_matches_next = 0
    scalp_matches_own = 0
    for pid, rs in dups.items():
        kinds = sorted(("hold" if (r.get("actual") or {}).get("exit_reason") == "hold"
                        else "scalp") for r in rs)
        if kinds == ["hold", "scalp"]:
            hold_scalp_pairs += 1
            hold_c = next(r for r in rs if (r["actual"]).get("exit_reason") == "hold")
            scalp_c = next(r for r in rs if (r["actual"]).get("exit_reason") != "hold")
            o_next = out_by_pid.get(pid + 1)
            o_own = out_by_pid.get(pid)
            if o_next and o_next.get("pnl") is not None and \
                    abs(hold_c["actual"]["pnl"] - o_next["pnl"]) <= 0.35:
                hold_matches_next += 1
            if o_own and o_own.get("pnl") is not None and \
                    abs(scalp_c["actual"]["pnl"] - o_own["pnl"]) <= 0.35:
                scalp_matches_own += 1
    print(f"\nCF duplicate forensics: dup_pids={len(dups)}  extra_records={n_pairs}  "
          f"hold+scalp_pairs={hold_scalp_pairs}")
    print(f"  hold-copy pnl matches outcome[pid+1] (<=0.35): {hold_matches_next}")
    print(f"  scalp-copy pnl matches outcome[pid]  (<=0.35): {scalp_matches_own}")
    dup_act_pnl = sum(
        sorted(rs, key=lambda r: r.get("timestamp") or "")[i]["actual"]["pnl"]
        for rs in dups.values() for i in range(len(rs) - 1)
        if all((r.get("actual") or {}).get("pnl") is not None for r in rs))
    print(f"  actual pnl double-counted by no-dedupe (dropped copies): {dup_act_pnl:+.2f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
