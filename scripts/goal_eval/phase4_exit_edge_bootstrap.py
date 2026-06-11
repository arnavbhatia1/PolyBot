"""Phase 4 — verify the exit edge is real, not assumed.

Per CF record (deduped by position_id, latest timestamp):
  actual-policy pnl  = actual.pnl
  always-hold pnl    = counterfactual.pnl for scalp-type, actual.pnl for hold-type
  DELTA              = actual - always_hold   (only scalp-type records contribute)

Significance bar (tasks/goal.md): day-clustered bootstrap of the total delta,
1000 iters, seed 42, resampling ET days with replacement — p10 must be > 0.

Robustness: outcomes for days <= 2026-06-04 were fee-restamped to 0.07 but the
CF records' pnl arms were NOT (verified: gap == fees*(1-0.018/0.07) per pid).
Entry fee is common to both arms and cancels in the delta; the scalp arm's
EXIT fee is understated, so a fee-corrected delta is also reported.

Run: python scripts/goal_eval/phase4_exit_edge_bootstrap.py
"""
from __future__ import annotations

import json
import random
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")

MEM = Path(__file__).resolve().parent.parent.parent / "polybot" / "memory"
ET = ZoneInfo("America/New_York")
FEE_NEW, FEE_OLD = 0.07, 0.018       # restamp coefficients (analyze_selective.py)


# ---------------------------------------------------------------- loading
def load_records(dirname: str) -> list[dict]:
    """Glob *.json under memory/<dirname>; rollup files are arrays — flatten."""
    out = []
    for f in sorted((MEM / dirname).glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return [r for r in out if isinstance(r, dict)]


def parse_ts(r: dict) -> datetime:
    try:
        dt = datetime.fromisoformat((r.get("timestamp") or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def dedupe(records: list[dict]) -> tuple[list[dict], int]:
    """Keep, per position_id, the record with the latest timestamp."""
    best: dict = {}
    dropped = 0
    for r in records:
        pid = r.get("position_id")
        if pid is None:
            continue
        if pid in best:
            dropped += 1
            if parse_ts(r) >= parse_ts(best[pid]):
                best[pid] = r
        else:
            best[pid] = r
    return list(best.values()), dropped


def et_day(r: dict) -> str:
    return parse_ts(r).astimezone(ET).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- stats
def day_t(day_vals: list[float]) -> float:
    """Day-clustered t = mean(d_i) / (pstdev(d_i)/sqrt(n_days))."""
    if len(day_vals) < 2:
        return float("nan")
    se = st.pstdev(day_vals) / (len(day_vals) ** 0.5)
    return st.mean(day_vals) / se if se > 0 else float("nan")


def boot_total(by_day: dict[str, float], n_iter: int = 1000):
    """Day-clustered bootstrap of the TOTAL: resample the unique-day list with
    replacement (same count), statistic = sum of sampled days' sums."""
    days = sorted(by_day)
    random.seed(42)
    totals = []
    for _ in range(n_iter):
        sample = random.choices(days, k=len(days))
        totals.append(sum(by_day[d] for d in sample))
    totals.sort()
    return (st.mean(totals), totals[int(0.10 * n_iter)], totals[int(0.90 * n_iter)])


def seg_line(name: str, rows: list[dict]) -> str:
    if not rows:
        return f"  {name:<28}  (none — no such records in the pool)"
    act = sum(r["act"] for r in rows)
    hold = sum(r["hold_arm"] for r in rows)
    delta = act - hold
    by_day = defaultdict(float)
    for r in rows:
        by_day[r["day"]] += r["delta"]
    t = day_t(list(by_day.values()))
    return (f"  {name:<28} n={len(rows):>5}  actual={act:>+10.2f}  hold={hold:>+10.2f}  "
            f"delta={delta:>+10.2f}  t_day={t:>5.2f} ({len(by_day)}d)")


# ---------------------------------------------------------------- main
def main() -> None:
    raw_cfs = load_records("counterfactuals")
    raw_outs = load_records("outcomes")
    cfs, cf_dropped = dedupe(raw_cfs)
    outs, out_dropped = dedupe(raw_outs)
    out_by = {o["position_id"]: o for o in outs if o.get("pnl") is not None}
    print(f"loaded counterfactuals: {len(raw_cfs)} raw -> {len(cfs)} deduped "
          f"({cf_dropped} duplicates dropped)")
    print(f"loaded outcomes:        {len(raw_outs)} raw -> {len(outs)} deduped "
          f"({out_dropped} duplicates dropped)")

    # ---- build CF rows --------------------------------------------------
    rows, skipped = [], defaultdict(int)
    for c in cfs:
        actual, cf = c.get("actual") or {}, c.get("counterfactual") or {}
        kind = "hold" if actual.get("exit_reason") == "hold" else "scalp"
        act, cfp = actual.get("pnl"), cf.get("pnl")
        if act is None:
            skipped["no_actual_pnl"] += 1
            continue
        if kind == "scalp" and cfp is None:
            skipped["scalp_no_cf_pnl"] += 1
            continue
        if kind == "scalp" and cf.get("resolution_price") not in (0.0, 1.0):
            skipped["scalp_nonbinary_resolution"] += 1
            continue
        ctx = c.get("context_at_scalp") or c.get("context_at_worst_moment") or {}
        hold_arm = cfp if kind == "scalp" else act
        delta = act - hold_arm
        # fee correction: on restamped days CF arms are at the OLD coefficient;
        # only the scalp arm's exit fee differs between arms (resolution exit
        # fee is 0 at p in {0,1}; entry fee is common and cancels).
        corr = 0.0
        o = out_by.get(c["position_id"])
        if (kind == "scalp" and o is not None and o.get("fee_restamped") == FEE_NEW
                and actual.get("exit_price") is not None
                and o.get("entry_price") and o.get("size")):
            shares = o["size"] / o["entry_price"]
            ep = actual["exit_price"]
            corr = (FEE_NEW - FEE_OLD) * shares * ep * (1 - ep)
        rows.append(dict(
            pid=c["position_id"], kind=kind, act=act, hold_arm=hold_arm,
            delta=delta, delta_corr=delta - corr, day=et_day(c),
            mp=ctx.get("market_price"),
            loss_cut=bool(ctx.get("loss_cut")) or actual.get("exit_reason") == "loss_cut",
        ))
    if skipped:
        print(f"CF rows skipped: {dict(skipped)}")

    scalps = [r for r in rows if r["kind"] == "scalp"]
    holds = [r for r in rows if r["kind"] == "hold"]

    # ---- 1) policy comparison -------------------------------------------
    actual_total = sum(r["act"] for r in rows)
    hold_total = sum(r["hold_arm"] for r in rows)
    delta_total = actual_total - hold_total
    fee_corr_total = sum(r["delta"] - r["delta_corr"] for r in rows)
    delta_corr_total = delta_total - fee_corr_total
    print(f"\n== 1) POLICY COMPARISON (n_cf={len(rows)}: "
          f"scalp={len(scalps)}, hold={len(holds)}) ==")
    print(f"  actual policy total : {actual_total:>+10.2f}")
    print(f"  always-hold total   : {hold_total:>+10.2f}")
    print(f"  DELTA (exit edge)   : {delta_total:>+10.2f}   "
          f"(only the {len(scalps)} scalp-type records contribute)")
    print(f"  fee-corrected DELTA : {delta_corr_total:>+10.2f}   "
          f"(scalp-arm exit fee at 0.07 on restamped days: -{fee_corr_total:.2f})")

    # ---- 2) day-clustered significance ----------------------------------
    by_day_delta, by_day_corr, by_day_act, by_day_hold, by_day_n = (
        defaultdict(float), defaultdict(float), defaultdict(float),
        defaultdict(float), defaultdict(int))
    for r in rows:
        by_day_delta[r["day"]] += r["delta"]
        by_day_corr[r["day"]] += r["delta_corr"]
        by_day_act[r["day"]] += r["act"]
        by_day_hold[r["day"]] += r["hold_arm"]
        by_day_n[r["day"]] += r["kind"] == "scalp"
    days = sorted(by_day_delta)
    print(f"\n== 2) DAY-CLUSTERED SIGNIFICANCE on the delta ({len(days)} ET days) ==")
    print(f"  {'day':<12}{'n_scalp':>8}{'actual':>10}{'hold':>10}{'delta':>10}{'delta_fee_corr':>15}")
    for d in days:
        print(f"  {d:<12}{by_day_n[d]:>8}{by_day_act[d]:>+10.2f}"
              f"{by_day_hold[d]:>+10.2f}{by_day_delta[d]:>+10.2f}{by_day_corr[d]:>+15.2f}")
    dvals = [by_day_delta[d] for d in days]
    cvals = [by_day_corr[d] for d in days]
    t = day_t(dvals)
    t_corr = day_t(cvals)
    pos = sum(1 for v in dvals if v > 0)
    best = max(dvals)
    best_share = best / delta_total if delta_total else float("nan")
    print(f"  per-day delta: mean={st.mean(dvals):+.2f}  pstdev={st.pstdev(dvals):.2f}  "
          f"t_day={t:.2f}  (n_days={len(days)})   [fee-corrected t_day={t_corr:.2f}]")
    print(f"  days positive: {pos}/{len(days)}   best day {days[dvals.index(best)]} "
          f"= {best:+.2f} ({100 * best_share:.1f}% of total delta)")
    bm, bp10, bp90 = boot_total(by_day_delta)
    cm, cp10, cp90 = boot_total(by_day_corr)
    print(f"  bootstrap total delta (1000 iters, seed 42, resample days): "
          f"mean={bm:+.2f}  p10={bp10:+.2f}  p90={bp90:+.2f}")
    print(f"  bootstrap fee-corrected delta:                              "
          f"mean={cm:+.2f}  p10={cp10:+.2f}  p90={cp90:+.2f}")
    print(f"  GOAL BAR p10 > 0: {'PASS' if bp10 > 0 else 'FAIL'} (as-recorded)  /  "
          f"{'PASS' if cp10 > 0 else 'FAIL'} (fee-corrected)")

    # ---- 3) splits --------------------------------------------------------
    print("\n== 3) SPLITS (scalp-type records; delta = actual - hold-to-resolution) ==")
    print(seg_line("profit scalps (act>=0)", [r for r in scalps if r["act"] >= 0]))
    print(seg_line("loss scalps (act<0)", [r for r in scalps if r["act"] < 0]))
    n_lc_field = sum(1 for r in scalps if r["loss_cut"])
    print(seg_line("loss_cut-flagged", [r for r in scalps if r["loss_cut"]]))
    if n_lc_field == 0:
        print("    (loss_cut flag is False/absent on every scalp CF and no outcome has "
              "exit_reason='loss_cut' — the loss-cut branch never fired in this pool)")
    print(seg_line("non-loss_cut", [r for r in scalps if not r["loss_cut"]]))
    itm = [r for r in scalps if r["mp"] is not None and r["mp"] >= 0.5]
    otm = [r for r in scalps if r["mp"] is not None and r["mp"] < 0.5]
    unk = [r for r in scalps if r["mp"] is None]
    print(seg_line("ITM at scalp (mp>=0.5)", itm))
    print(seg_line("OTM at scalp (mp<0.5)", otm))
    if unk:
        print(seg_line("mp missing", unk))

    # ---- 4) reconciliation -------------------------------------------------
    print("\n== 4) RECONCILIATION ==")
    cf_pids = {r["pid"] for r in rows}
    common = sorted(cf_pids & set(out_by))
    act_by_pid = {r["pid"]: r["act"] for r in rows}
    cf_act_common = sum(act_by_pid[p] for p in common)
    out_common = sum(out_by[p]["pnl"] for p in common)
    gap = cf_act_common - out_common
    print(f"  position_ids: CF={len(cf_pids)}  outcomes(with pnl)={len(out_by)}  "
          f"common={len(common)}")
    print(f"  sum CF actual.pnl (common pids) = {cf_act_common:>+10.2f}")
    print(f"  sum outcomes pnl  (common pids) = {out_common:>+10.2f}")
    print(f"  gap = {gap:+.2f}")

    # (a) gap decomposition: fee restamp vs mis-keyed records
    fee_match = fee_match_sum = other = other_sum = 0
    for p in common:
        g = act_by_pid[p] - out_by[p]["pnl"]
        if abs(g) <= 0.005:
            continue
        exp = (out_by[p].get("fees") or 0.0) * (1 - FEE_OLD / FEE_NEW) \
            if out_by[p].get("fee_restamped") == FEE_NEW else 0.0
        if abs(g - exp) < 0.02 and exp:
            fee_match += 1
            fee_match_sum += g
        else:
            other += 1
            other_sum += g
    print(f"  gap decomposition: {fee_match} pids match the fee restamp exactly "
          f"(outcome pnl re-costed 0.018->0.07, CF arms not; gap == fees*(1-0.018/0.07); "
          f"sum {fee_match_sum:+.2f});")
    print(f"                     {other} pids unexplained/mis-keyed (sum {other_sum:+.2f})")

    # (b) outcomes with no CF record
    miss = [o for o in outs if o.get("pnl") is not None and o["position_id"] not in cf_pids]
    mr = Counter(o.get("exit_reason") for o in miss)
    print(f"  outcomes without CF: {len(miss)} (pnl sum "
          f"{sum(o['pnl'] for o in miss):+.2f}; by exit_reason {dict(mr)})   "
          f"CFs without outcome: {len(cf_pids - set(out_by))}")

    # (c) duplicate characterization + no-dedupe reproduction of prior numbers
    by_pid_all = defaultdict(list)
    for c in raw_cfs:
        if c.get("position_id") is not None:
            by_pid_all[c["position_id"]].append(c)
    dup_pids = [p for p, v in by_pid_all.items() if len(v) > 1]
    pair_kinds = Counter()
    dropped_act = 0.0
    hold_copy_matches_next = scalp_copy_matches_own = 0
    for p in dup_pids:
        rs = sorted(by_pid_all[p], key=parse_ts)
        pair_kinds[tuple(sorted(
            ("hold" if r["actual"].get("exit_reason") == "hold" else "scalp")
            for r in rs))] += 1
        for r in rs[:-1]:
            dropped_act += r["actual"].get("pnl") or 0.0
        holds_c = [r for r in rs if r["actual"].get("exit_reason") == "hold"]
        scalps_c = [r for r in rs if r["actual"].get("exit_reason") != "hold"]
        nxt = out_by.get(p + 1)
        own = out_by.get(p)
        if holds_c and nxt and abs((holds_c[0]["actual"].get("pnl") or 0) - nxt["pnl"]) < 0.35:
            hold_copy_matches_next += 1
        if scalps_c and own and abs((scalps_c[0]["actual"].get("pnl") or 0) - own["pnl"]) < 0.35:
            scalp_copy_matches_own += 1
    nd_act = sum(c["actual"]["pnl"] for c in raw_cfs
                 if (c.get("actual") or {}).get("pnl") is not None)
    nd_hold = 0.0
    for c in raw_cfs:
        actual, cf = c.get("actual") or {}, c.get("counterfactual") or {}
        if actual.get("pnl") is None:
            continue
        if actual.get("exit_reason") == "hold" or cf.get("pnl") is None:
            nd_hold += actual["pnl"]
        else:
            nd_hold += cf["pnl"]
    print(f"  duplicates: {len(dup_pids)} pids appear >1x (pair kinds {dict(pair_kinds)});")
    print(f"    dropped copies' actual.pnl sums {dropped_act:+.2f}; "
          f"scalp-copy matches own outcome on {scalp_copy_matches_own}, "
          f"hold-copy matches outcome of pid+1 on {hold_copy_matches_next} "
          f"(off-by-one keying — the dropped hold-copies are OTHER positions' trades)")
    print(f"  prior session claim: actual +1238 / always-hold +337 / delta +901 'on 12 days'")
    print(f"  NO-dedupe totals on the current pool: actual={nd_act:+.2f}  "
          f"hold={nd_hold:+.2f}  delta={nd_act - nd_hold:+.2f}")
    print(f"    -> the prior levels reproduce EXACTLY without dedupe: they double-counted "
          f"{dropped_act:+.2f} of (mostly hold-type) actual pnl; the DELTA was robust "
          f"({nd_act - nd_hold:+.2f} vs deduped {delta_total:+.2f}) because hold-type "
          f"records contribute zero delta")

    # ---- 5) scalp-only counterfactual ---------------------------------------
    print("\n== 5) SCALP-ONLY counterfactual ==")
    s_act = sum(r["act"] for r in scalps)
    s_hold = sum(r["hold_arm"] for r in scalps)
    s_by_day, s_by_day_corr = defaultdict(float), defaultdict(float)
    for r in scalps:
        s_by_day[r["day"]] += r["delta"]
        s_by_day_corr[r["day"]] += r["delta_corr"]
    s_t = day_t(list(s_by_day.values()))
    print(f"  n_scalp={len(scalps)}  actual={s_act:+.2f}  hold={s_hold:+.2f}  "
          f"delta={s_act - s_hold:+.2f}  t_day={s_t:.2f} ({len(s_by_day)} days w/ scalps)")
    sm, sp10, sp90 = boot_total(s_by_day)
    scm, scp10, scp90 = boot_total(s_by_day_corr)
    print(f"  bootstrap (1000 iters, seed 42, scalp-day pool): "
          f"mean={sm:+.2f}  p10={sp10:+.2f}  p90={sp90:+.2f}  "
          f"-> p10 > 0: {'PASS' if sp10 > 0 else 'FAIL'}")
    print(f"  fee-corrected: mean={scm:+.2f}  p10={scp10:+.2f}  p90={scp90:+.2f}  "
          f"-> p10 > 0: {'PASS' if scp10 > 0 else 'FAIL'}")

    # machine-readable summary
    print("\n== KEY NUMBERS ==")
    print(json.dumps(dict(
        n_cf=len(rows), n_scalp=len(scalps), n_hold=len(holds),
        actual_total=round(actual_total, 2), always_hold_total=round(hold_total, 2),
        delta_total=round(delta_total, 2),
        delta_total_fee_corrected=round(delta_corr_total, 2),
        t_day=round(t, 2), t_day_fee_corrected=round(t_corr, 2), n_days=len(days),
        days_positive=pos, best_day_share=round(best_share, 4),
        boot_mean=round(bm, 2), boot_p10=round(bp10, 2), boot_p90=round(bp90, 2),
        boot_mean_fee_corrected=round(cm, 2), boot_p10_fee_corrected=round(cp10, 2),
        boot_p90_fee_corrected=round(cp90, 2),
        n_duplicates_dropped=cf_dropped + out_dropped,
        cf_duplicates_dropped=cf_dropped, outcome_duplicates_dropped=out_dropped,
        reconciliation_gap_usd=round(gap, 2),
        gap_fee_restamp_pids=fee_match, gap_fee_restamp_sum=round(fee_match_sum, 2),
        n_outcomes_without_cf=len(miss),
        outcomes_without_cf_pnl=round(sum(o['pnl'] for o in miss), 2),
        no_dedupe_actual_total=round(nd_act, 2),
        no_dedupe_always_hold_total=round(nd_hold, 2),
        scalp_only_boot=dict(mean=round(sm, 2), p10=round(sp10, 2), p90=round(sp90, 2)),
    ), indent=2))


if __name__ == "__main__":
    main()
