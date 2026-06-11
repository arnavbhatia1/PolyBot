"""Phase 1 — P&L decomposition of ALL closed trades (outcomes pool only).

1) by exit type   2) by model agreement (window winner)   3) by entry-edge bucket
4) fees vs gross  5) the one summary table

Data loaded ONCE at start. Dedupe by position_id keeping latest timestamp.
Run: python scripts/goal_eval/phase1_pnl_decomposition.py
"""
from __future__ import annotations

import json
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")

MEM = Path(__file__).resolve().parent.parent.parent / "polybot" / "memory"
ET = ZoneInfo("America/New_York")
BOOT_N = 1000


def load_records(dirname: str) -> list[dict]:
    out = []
    for f in sorted((MEM / dirname).glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def dedupe(recs: list[dict]) -> tuple[list[dict], int]:
    """Keep, per position_id, the record with the latest timestamp."""
    best: dict[int, dict] = {}
    dropped = 0
    for r in recs:
        pid = r.get("position_id")
        if pid is None:
            continue
        prev = best.get(pid)
        if prev is None:
            best[pid] = r
        else:
            dropped += 1
            if (r.get("timestamp") or "") >= (prev.get("timestamp") or ""):
                best[pid] = r
    return list(best.values()), dropped


def et_day(r: dict) -> str | None:
    ts = r.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%Y-%m-%d")
    except ValueError:
        return None


def tc(r: dict) -> dict:
    return (r.get("indicator_snapshot") or {}).get("trade_context") or {}


def boot_total_pnl(group: list[dict], all_days: list[str]) -> tuple[float, float, float]:
    """Day-clustered bootstrap of the group's TOTAL net pnl. Resamples the
    global unique-ET-day list with replacement (seed 42, 1000 iters)."""
    by_day = defaultdict(float)
    for r in group:
        by_day[r["_day"]] += r["pnl"]
    random.seed(42)
    stats = []
    for _ in range(BOOT_N):
        sampled = random.choices(all_days, k=len(all_days))
        stats.append(sum(by_day.get(d, 0.0) for d in sampled))
    stats.sort()
    mean = sum(stats) / len(stats)
    return mean, stats[int(0.10 * BOOT_N)], stats[int(0.90 * BOOT_N) - 1]


def days_positive(group: list[dict]) -> tuple[int, int]:
    by_day = defaultdict(float)
    for r in group:
        by_day[r["_day"]] += r["pnl"]
    return sum(1 for v in by_day.values() if v > 0), len(by_day)


def ols_slope(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def day_t(vals: list[float]) -> float:
    sd = st.pstdev(vals)
    if sd == 0 or len(vals) < 2:
        return float("nan")
    return st.mean(vals) / (sd / len(vals) ** 0.5)


def main() -> None:
    # ---------------- load once ----------------
    raw_outcomes = load_records("outcomes")
    raw_cfs = load_records("counterfactuals")
    outcomes, dup_out = dedupe(raw_outcomes)
    cfs, dup_cf = dedupe(raw_cfs)
    print(f"loaded outcomes: {len(raw_outcomes)} raw -> {len(outcomes)} unique "
          f"({dup_out} duplicates dropped)")
    print(f"loaded counterfactuals: {len(raw_cfs)} raw -> {len(cfs)} unique "
          f"({dup_cf} duplicates dropped)")

    # Window-winner map for scalped trades, from the CF hold-to-resolution arm.
    # Built from ALL raw scalp-arm records, not the deduped set: ~300 scalped
    # positions carry BOTH a (mis-keyed) hold-arm CF and a scalp-arm CF, and for
    # 7 of them the hold-arm has the later timestamp — keep-latest would lose the
    # winner. resolution_price is a window property (verified: 0 conflicts).
    cf_winner: dict[int, bool] = {}
    for c in raw_cfs:
        if (c.get("actual") or {}).get("exit_reason") != "hold":
            rp = (c.get("counterfactual") or {}).get("resolution_price")
            if rp in (0.0, 1.0):
                cf_winner[c["position_id"]] = rp == 1.0

    # ---------------- normalize trades ----------------
    trades, n_bad = [], 0
    for t in outcomes:
        day = et_day(t)
        if t.get("pnl") is None or not t.get("size") or day is None:
            n_bad += 1
            continue
        t["_day"] = day
        reason = t.get("exit_reason") or "?"
        if reason == "resolution":
            t["_won"] = bool(t.get("correct"))
        elif t.get("position_id") in cf_winner:
            t["_won"] = cf_winner[t["position_id"]]
        else:
            t["_won"] = None  # scalp with no CF — winner unknowable
        trades.append(t)
    all_days = sorted({t["_day"] for t in trades})
    n_days = len(all_days)
    n_scalp_no_cf = sum(1 for t in trades
                        if t.get("exit_reason") != "resolution" and t["_won"] is None)
    print(f"usable trades: {len(trades)} (excluded {n_bad} missing pnl/size/timestamp) "
          f"over {n_days} ET days ({all_days[0]} .. {all_days[-1]})")
    print(f"scalped trades lacking a CF winner (excluded from agreement/winrate "
          f"analyses only): {n_scalp_no_cf}")

    total_pnl = sum(t["pnl"] for t in trades)
    total_fees = sum(t.get("fees") or 0.0 for t in trades)

    summary_rows = []  # (label, group) for the final one-table

    # ================= 1) BY EXIT TYPE =================
    print(f"\n{'='*100}\n1) BY EXIT TYPE\n{'='*100}")
    hdr = (f"{'exit_reason':<16}{'n':>6}{'net_pnl$':>11}{'fees$':>9}{'win%':>7}"
           f"{'boot_mean':>11}{'p10':>10}{'p90':>10}{'days+':>9}")
    print(hdr)
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t.get("exit_reason") or "?"].append(t)
    exit_boot = {}
    for reason, g in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        pnl = sum(t["pnl"] for t in g)
        fees = sum(t.get("fees") or 0.0 for t in g)
        wr = sum(1 for t in g if t["pnl"] > 0) / len(g)
        bm, p10, p90 = boot_total_pnl(g, all_days)
        dp, dpres = days_positive(g)
        exit_boot[reason] = dict(n=len(g), pnl=round(pnl, 2),
                                 p10=round(p10, 2), p90=round(p90, 2))
        summary_rows.append((f"exit:{reason}", g))
        print(f"{reason:<16}{len(g):>6}{pnl:>+11.2f}{fees:>9.2f}{wr*100:>6.1f}%"
              f"{bm:>+11.2f}{p10:>+10.2f}{p90:>+10.2f}{f'{dp}/{dpres}':>9}")
    bm, p10, p90 = boot_total_pnl(trades, all_days)
    dp, dpres = days_positive(trades)
    print(f"{'TOTAL':<16}{len(trades):>6}{total_pnl:>+11.2f}{total_fees:>9.2f}"
          f"{sum(1 for t in trades if t['pnl']>0)/len(trades)*100:>6.1f}%"
          f"{bm:>+11.2f}{p10:>+10.2f}{p90:>+10.2f}{f'{dp}/{dpres}':>9}")
    total_boot = (bm, p10, p90)

    # ================= 2) BY MODEL AGREEMENT =================
    print(f"\n{'='*100}\n2) BY MODEL AGREEMENT (did the chosen side win the window?)\n{'='*100}")
    agree = [t for t in trades if t["_won"] is True]
    disagree = [t for t in trades if t["_won"] is False]
    unknown = [t for t in trades if t["_won"] is None]
    print(f"{'split':<22}{'n':>6}{'net_pnl$':>11}{'boot_mean':>11}{'p10':>10}{'p90':>10}{'days+':>9}")
    agr_stats = {}
    for label, g in [("AGREE (side won)", agree), ("DISAGREE (side lost)", disagree),
                     ("UNKNOWN (no CF)", unknown)]:
        if not g:
            print(f"{label:<22}{0:>6}")
            continue
        pnl = sum(t["pnl"] for t in g)
        bm, p10, p90 = boot_total_pnl(g, all_days)
        dp, dpres = days_positive(g)
        agr_stats[label] = pnl
        summary_rows.append((label, g))
        print(f"{label:<22}{len(g):>6}{pnl:>+11.2f}{bm:>+11.2f}{p10:>+10.2f}"
              f"{p90:>+10.2f}{f'{dp}/{dpres}':>9}")
    dis_pnl = sum(t["pnl"] for t in disagree)
    print(f"\n>> money from directionally-WRONG trades: {dis_pnl:+.2f} "
          f"({dis_pnl/total_pnl*100:+.1f}% of total {total_pnl:+.2f})")

    print("\ncross-tab exit_reason x agreement (n / $):")
    cols = ["AGREE", "DISAGREE", "UNKNOWN"]
    print(f"{'exit_reason':<16}" + "".join(f"{c:>24}" for c in cols))
    for reason, g in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        cells = []
        for w in (True, False, None):
            sub = [t for t in g if t["_won"] is w]
            cells.append(f"{len(sub):>6} /{sum(t['pnl'] for t in sub):>+11.2f}    ")
        print(f"{reason:<16}" + "".join(f"{c:>24}" for c in cells))

    # ================= 3) BY ENTRY-EDGE BUCKET =================
    print(f"\n{'='*100}\n3) BY ENTRY-EDGE BUCKET (edge = model_probability - entry_price)\n{'='*100}")
    buckets = [(-9.0, 0.0, "<0"), (0.0, 0.02, "0-0.02"), (0.02, 0.04, "0.02-0.04"),
               (0.04, 0.07, "0.04-0.07"), (0.07, 0.12, "0.07-0.12"), (0.12, 9.0, ">=0.12")]

    def bucket_table(prob_field: str, title: str):
        rows = []
        for t in trades:
            p = tc(t).get(prob_field)
            ep = t.get("entry_price")
            if p is None or ep is None:
                continue
            rows.append((p - ep, t))
        n_missing = len(trades) - len(rows)
        print(f"\n-- {title} (n={len(rows)}, missing prob/entry on {n_missing}) --")
        print(f"{'bucket':<12}{'n':>6}{'win_rate':>10}{'(wr_n)':>8}{'mean_entry':>12}"
              f"{'net$':>11}{'mean_gain%':>12}")
        for lo, hi, lab in buckets:
            b = [(e, t) for e, t in rows if lo <= e < hi]
            if not b:
                print(f"{lab:<12}{0:>6}")
                continue
            won = [t for _, t in b if t["_won"] is not None]
            wr = (sum(t["_won"] for t in won) / len(won)) if won else float("nan")
            me = sum(t["entry_price"] for _, t in b) / len(b)
            pnl = sum(t["pnl"] for _, t in b)
            mg = sum(t.get("gain_pct") or 0.0 for _, t in b) / len(b)
            print(f"{lab:<12}{len(b):>6}{wr:>10.3f}{len(won):>8}{me:>12.3f}"
                  f"{pnl:>+11.2f}{mg*100:>+11.2f}%")
        return rows

    bucket_table("model_probability", "CALIBRATED edge")
    bucket_table("model_probability_raw", "RAW edge")

    # slope of (won - entry_price) on edge, per ET day with >=10 trades
    def slope_test(prob_field: str) -> tuple[float, float, int]:
        by_day = defaultdict(list)
        for t in trades:
            p = tc(t).get(prob_field)
            ep = t.get("entry_price")
            if p is None or ep is None or t["_won"] is None:
                continue
            by_day[t["_day"]].append((p - ep, (1.0 if t["_won"] else 0.0) - ep))
        slopes = []
        for d in sorted(by_day):
            pts = by_day[d]
            if len(pts) < 10:
                continue
            s = ols_slope([x for x, _ in pts], [y for _, y in pts])
            if s is not None:
                slopes.append(s)
        if not slopes:
            return float("nan"), float("nan"), 0
        return st.mean(slopes), day_t(slopes), len(slopes)

    print("\n-- slope significance: OLS of (won - entry_price) on edge, per ET day >=10 trades --")
    print(f"{'edge type':<14}{'mean_slope':>12}{'t_day':>8}{'n_days':>8}")
    sc, tcal, ndc = slope_test("model_probability")
    sr, traw, ndr = slope_test("model_probability_raw")
    print(f"{'calibrated':<14}{sc:>+12.4f}{tcal:>8.2f}{ndc:>8}")
    print(f"{'raw':<14}{sr:>+12.4f}{traw:>8.2f}{ndr:>8}")
    print("(slope ~1.0 would mean claimed edge fully realizes; 0 = edge is noise)")

    # ================= 4) FEES VS GROSS =================
    print(f"\n{'='*100}\n4) FEES VS GROSS\n{'='*100}")
    res_trades = [t for t in trades if t.get("exit_reason") == "resolution"
                  and t.get("entry_price") and t.get("fees") is not None][:20]
    resid = []
    for t in res_trades:
        shares = t["size"] / t["entry_price"]
        payout = shares * (t.get("exit_price") or 0.0)
        pred = payout - t["size"] - t["fees"]
        resid.append(abs(pred - t["pnl"]) / t["size"])
    if resid:
        signed = []
        for t in res_trades:
            shares = t["size"] / t["entry_price"]
            signed.append((shares * (t.get("exit_price") or 0.0) - t["size"]
                           - t["fees"] - t["pnl"]) / t["size"])
        print(f"sanity check on {len(res_trades)} resolution trades: "
              f"pnl vs (shares*payout - cost - fees), shares = size/entry_price")
        print(f"  mean |residual|/size = {st.mean(resid)*100:.3f}%   "
              f"max = {max(resid)*100:.3f}%   mean signed = {st.mean(signed)*100:+.3f}%")
        print(f"  -> pnl IS net of fees ({'consistent' if st.mean(resid) < 0.02 else 'INCONSISTENT — investigate'}). "
              f"Residual is one-sided: reconstruction with the recorded entry_price")
        print(f"     slightly overstates pnl because the actual fill price (slippage/"
              f"tick-snap) was worse; pnl on disk = size * gain_pct at the real fill.")
    gross = total_pnl + total_fees
    ratio = total_fees / gross if gross > 0 else total_fees / abs(gross)
    print(f"\ntotal net pnl   = {total_pnl:+10.2f}")
    print(f"total fees      = {total_fees:10.2f}")
    print(f"gross (net+fee) = {gross:+10.2f}")
    print(f"fee/gross       = {total_fees/gross:.3f}" if gross != 0 else "gross = 0")
    print(f"fee/|gross|     = {total_fees/abs(gross):.3f}")

    # ================= 5) THE ONE TABLE =================
    print(f"\n{'='*100}\n5) P&L SOURCE DECOMPOSITION (the one table)\n{'='*100}")
    print(f"{'source':<24}{'n':>6}{'$contrib':>11}{'boot_p10':>11}{'boot_p90':>11}"
          f"{'days+ /present':>16}")
    one_table = {}
    for label, g in summary_rows:
        pnl = sum(t["pnl"] for t in g)
        _, p10, p90 = boot_total_pnl(g, all_days)
        dp, dpres = days_positive(g)
        one_table[label] = dict(n=len(g), pnl=round(pnl, 2),
                                p10=round(p10, 2), p90=round(p90, 2),
                                days_pos=dp, days_present=dpres)
        print(f"{label:<24}{len(g):>6}{pnl:>+11.2f}{p10:>+11.2f}{p90:>+11.2f}"
              f"{f'{dp}/{dpres}':>16}")
    print(f"{'TOTAL':<24}{len(trades):>6}{total_pnl:>+11.2f}{total_boot[1]:>+11.2f}"
          f"{total_boot[2]:>+11.2f}{f'{days_positive(trades)[0]}/{n_days}':>16}")

    # machine-readable spine
    print("\nKEY_NUMBERS_JSON:")
    print(json.dumps(dict(
        total_net_pnl=round(total_pnl, 2), total_fees=round(total_fees, 2),
        gross_pnl=round(gross, 2), fee_to_gross_ratio=round(total_fees / gross, 4),
        n_trades=len(trades), n_days=n_days,
        n_duplicates_dropped_outcomes=dup_out, n_duplicates_dropped_cfs=dup_cf,
        scalps_no_cf=n_scalp_no_cf,
        by_exit_type=exit_boot,
        agree_pnl=round(sum(t["pnl"] for t in agree), 2),
        disagree_pnl=round(dis_pnl, 2),
        unknown_pnl=round(sum(t["pnl"] for t in unknown), 2),
        slope_cal=dict(mean=round(sc, 4), t=round(tcal, 2), n_days=ndc),
        slope_raw=dict(mean=round(sr, 4), t=round(traw, 2), n_days=ndr),
        total_boot=dict(mean=round(total_boot[0], 2), p10=round(total_boot[1], 2),
                        p90=round(total_boot[2], 2)),
        one_table=one_table,
    ), indent=1))


if __name__ == "__main__":
    main()
