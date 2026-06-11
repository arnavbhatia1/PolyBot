"""Adversarial verification of phase1_pnl_decomposition (independent re-derivation).

Loads outcomes + counterfactuals once, dedupes by position_id (keep latest timestamp),
re-derives every headline number of the claimed result from scratch.
Run: python scripts/goal_eval/verify_p1_pnl_decomposition.py
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
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
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def et_day(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(ET).date().isoformat()


def dedupe_latest(recs: list[dict]) -> tuple[list[dict], int]:
    best: dict[int, dict] = {}
    for r in recs:
        pid = r["position_id"]
        if pid not in best or (r.get("timestamp") or "") >= (best[pid].get("timestamp") or ""):
            best[pid] = r
    out = sorted(best.values(), key=lambda r: r.get("timestamp") or "")
    return out, len(recs) - len(out)


def main() -> None:
    raw_outcomes = load_records("outcomes")
    raw_cfs = load_records("counterfactuals")
    print(f"raw: outcomes={len(raw_outcomes)} cfs={len(raw_cfs)}")

    trades, dup_o = dedupe_latest(raw_outcomes)
    cfs, dup_c = dedupe_latest(raw_cfs)
    print(f"dedupe: outcomes dropped={dup_o} -> {len(trades)} ; cfs dropped={dup_c} -> {len(cfs)}")

    # ---- exit_reason inventory + loss_cut flag scan ------------------------
    print("exit_reason values:", dict(Counter(t.get("exit_reason") for t in trades)))
    lc = 0
    for c in raw_cfs:
        for key in ("context_at_scalp", "context_at_worst_moment"):
            ctx = c.get(key) or {}
            if any("loss_cut" in k for k in ctx) and ctx.get(
                next(k for k in ctx if "loss_cut" in k)
            ):
                lc += 1
    lc_keys = Counter(
        k
        for c in raw_cfs
        for key in ("context_at_scalp", "context_at_worst_moment")
        for k in (c.get(key) or {})
        if "loss_cut" in k
    )
    print(f"CF records w/ truthy loss_cut flag: {lc} ; loss_cut-ish keys seen: {dict(lc_keys)}")

    # ---- window-winner map from ALL raw scalp-arm CFs ----------------------
    winner: dict[int, bool] = {}
    conflicts = 0
    for c in raw_cfs:
        if (c.get("actual") or {}).get("exit_reason") == "hold":
            continue
        rp = (c.get("counterfactual") or {}).get("resolution_price")
        if rp in (0.0, 1.0):
            pid = c["position_id"]
            won = rp == 1.0
            if pid in winner and winner[pid] != won:
                conflicts += 1
            winner[pid] = won
    print(f"winner map: {len(winner)} pids, resolution_price conflicts={conflicts}")

    # ---- duplicate-CF forensics (claimed: 298 mis-keyed scalp+hold pairs) ---
    cnt = Counter(c["position_id"] for c in raw_cfs)
    dup_pids = sorted(p for p, n in cnt.items() if n > 1)
    by_pid = defaultdict(list)
    for c in raw_cfs:
        by_pid[c["position_id"]].append(c)
    out_by_pid = {t["position_id"]: t for t in trades}
    pair_shapes = Counter()
    lose_winner = 0  # naive keep-latest would keep a hold-arm for a scalped outcome
    miskey_match = 0  # hold-arm actual.pnl matches a neighboring outcome's pnl
    for p in dup_pids:
        arms = tuple(
            sorted((c.get("actual") or {}).get("exit_reason") or "?" for c in by_pid[p])
        )
        pair_shapes[arms] += 1
        recs = sorted(by_pid[p], key=lambda c: c.get("timestamp") or "")
        latest = recs[-1]
        t = out_by_pid.get(p)
        if (
            t is not None
            and t.get("exit_reason") != "resolution"
            and (latest.get("actual") or {}).get("exit_reason") == "hold"
        ):
            lose_winner += 1
        for c in by_pid[p]:
            if (c.get("actual") or {}).get("exit_reason") == "hold":
                apnl = (c.get("actual") or {}).get("pnl")
                for nb in (p - 1, p + 1, p - 2, p + 2):
                    nt = out_by_pid.get(nb)
                    if nt is not None and apnl is not None and abs(nt["pnl"] - apnl) < 0.01:
                        miskey_match += 1
                        break
    dup_scalped = sum(
        1
        for p in dup_pids
        if out_by_pid.get(p) is not None and out_by_pid[p].get("exit_reason") != "resolution"
    )
    print(
        f"dup CF pids: {len(dup_pids)} (records dropped {dup_c}); arm shapes {dict(pair_shapes)}; "
        f"dup pids whose outcome is scalped: {dup_scalped}; "
        f"naive-latest-would-keep-hold-arm (scalped outcome): {lose_winner}; "
        f"hold-arms matching neighbor outcome pnl: {miskey_match}"
    )

    # ---- core fields per trade ---------------------------------------------
    rows = []
    missing_ctx = 0
    for t in trades:
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        side = t["side"]
        price = ctx.get("market_price_down" if side == "Down" else "market_price_up")
        p_cal = ctx.get("model_probability")
        p_raw = ctx.get("model_probability_raw")
        if price is None or p_cal is None or p_raw is None:
            missing_ctx += 1
        er = t.get("exit_reason")
        if er == "resolution":
            won = bool(t["correct"])
        elif t["position_id"] in winner:
            won = winner[t["position_id"]]
        else:
            won = None
        rows.append(
            dict(
                pid=t["position_id"],
                day=et_day(t["timestamp"]),
                er=er,
                pnl=t["pnl"],
                fees=t.get("fees") or 0.0,
                size=t["size"],
                entry=t["entry_price"],
                exitp=t.get("exit_price"),
                gain=t.get("gain_pct"),
                price=price,
                p_cal=p_cal,
                p_raw=p_raw,
                won=won,
            )
        )
    print(f"trades missing ctx prob/price fields: {missing_ctx}")

    days = sorted({r["day"] for r in rows})
    n_days = len(days)
    print(f"n_days={n_days}  ({days[0]} .. {days[-1]})")

    # ---- headline totals ----------------------------------------------------
    tot_pnl = sum(r["pnl"] for r in rows)
    tot_fees = sum(r["fees"] for r in rows)
    gross = tot_pnl + tot_fees
    print(
        f"\nTOTALS: n={len(rows)} net={tot_pnl:+.2f} fees={tot_fees:.2f} "
        f"gross={gross:+.2f} fee/gross={tot_fees / gross:.4f}"
    )

    # ---- day-clustered bootstrap (seed 42, 1000 iters, global day list) -----
    random.seed(42)
    samples = [[random.choice(days) for _ in range(n_days)] for _ in range(1000)]

    def boot(sel_rows):
        dp = defaultdict(float)
        for r in sel_rows:
            dp[r["day"]] += r["pnl"]
        stats_ = sorted(sum(dp.get(d, 0.0) for d in s) for s in samples)
        return (
            sum(stats_) / len(stats_),
            stats_[100],
            stats_[900],
        )

    m, p10, p90 = boot(rows)
    print(f"total bootstrap: mean={m:+.2f} p10={p10:+.2f} p90={p90:+.2f}")

    # ---- by exit type --------------------------------------------------------
    print(f"\n{'exit':>12} {'n':>5} {'pnl':>10} {'fees':>9} {'win%':>6} {'days+':>6} {'p10':>10} {'p90':>10}")
    for er in sorted({r["er"] for r in rows}):
        sel = [r for r in rows if r["er"] == er]
        dp = defaultdict(float)
        for r in sel:
            dp[r["day"]] += r["pnl"]
        days_pos = sum(1 for d in days if dp.get(d, 0.0) > 0)
        m, p10, p90 = boot(sel)
        print(
            f"{er:>12} {len(sel):>5} {sum(r['pnl'] for r in sel):>+10.2f} "
            f"{sum(r['fees'] for r in sel):>9.2f} "
            f"{100 * sum(1 for r in sel if r['pnl'] > 0) / len(sel):>6.1f} "
            f"{days_pos:>3}/{n_days:<2} {p10:>+10.2f} {p90:>+10.2f}"
        )

    # ---- agreement (window winner == chosen side) ----------------------------
    print(f"\n{'group':>22} {'n':>5} {'pnl':>10} {'p10':>10} {'p90':>10}")
    groups = {
        "agree": [r for r in rows if r["won"] is True],
        "disagree": [r for r in rows if r["won"] is False],
        "unknown": [r for r in rows if r["won"] is None],
    }
    for g, sel in groups.items():
        if not sel:
            continue
        m, p10, p90 = boot(sel)
        print(
            f"{g:>22} {len(sel):>5} {sum(r['pnl'] for r in sel):>+10.2f} {p10:>+10.2f} {p90:>+10.2f}"
        )
    print("\ncross-tab exit x agreement:")
    for er in sorted({r["er"] for r in rows}):
        for g, sel in groups.items():
            s2 = [r for r in sel if r["er"] == er]
            if s2:
                print(f"  {er}_{g}: n={len(s2)} pnl={sum(r['pnl'] for r in s2):+.2f}")
    unk = groups["unknown"]
    unk_scalp_no_cf = [r for r in unk if r["er"] != "resolution"]
    print(f"unknown (scalp w/o scalp-arm CF): {len(unk_scalp_no_cf)} pids={sorted(r['pid'] for r in unk)[:30]}")
    cf_pids_any = {c["position_id"] for c in raw_cfs}
    no_cf_at_all = [r["pid"] for r in unk if r["pid"] not in cf_pids_any]
    only_hold_arm = [r["pid"] for r in unk if r["pid"] in cf_pids_any]
    print(f"  no CF record at all: {len(no_cf_at_all)} ; only hold-arm CF: {sorted(only_hold_arm)}")

    # ---- per-day OLS slope of (won - entry) on edge --------------------------
    def slope_test(xkey: str, ykey_entry: str, label: str):
        slopes = []
        for d in days:
            pts = [
                (r[xkey] - r["price"], (1.0 if r["won"] else 0.0) - r[ykey_entry])
                for r in rows
                if r["won"] is not None and r[xkey] is not None and r["price"] is not None
                and r["day"] == d
            ]
            if len(pts) < 3:
                continue
            mx = sum(x for x, _ in pts) / len(pts)
            my = sum(y for _, y in pts) / len(pts)
            sxx = sum((x - mx) ** 2 for x, _ in pts)
            if sxx == 0:
                continue
            slopes.append(sum((x - mx) * (y - my) for x, y in pts) / sxx)
        mean_s = sum(slopes) / len(slopes)
        sd = statistics.pstdev(slopes)
        t = mean_s / (sd / math.sqrt(len(slopes))) if sd > 0 else float("inf")
        print(f"  {label}: mean_slope={mean_s:+.4f} t_day={t:+.2f} n_days={len(slopes)}")
        return mean_s, t

    print("\nslope of (won - entry_price[fill]) on edge, day-clustered:")
    slope_test("p_cal", "entry", "calibrated")
    slope_test("p_raw", "entry", "raw")
    print("slope of (won - chosen_mkt_price) on edge, day-clustered (alt y):")

    def slope_test_alt(xkey: str, label: str):
        slopes = []
        for d in days:
            pts = [
                (r[xkey] - r["price"], (1.0 if r["won"] else 0.0) - r["price"])
                for r in rows
                if r["won"] is not None and r[xkey] is not None and r["price"] is not None
                and r["day"] == d
            ]
            if len(pts) < 3:
                continue
            mx = sum(x for x, _ in pts) / len(pts)
            my = sum(y for _, y in pts) / len(pts)
            sxx = sum((x - mx) ** 2 for x, _ in pts)
            if sxx == 0:
                continue
            slopes.append(sum((x - mx) * (y - my) for x, y in pts) / sxx)
        mean_s = sum(slopes) / len(slopes)
        sd = statistics.pstdev(slopes)
        t = mean_s / (sd / math.sqrt(len(slopes))) if sd > 0 else float("inf")
        print(f"  {label}: mean_slope={mean_s:+.4f} t_day={t:+.2f} n_days={len(slopes)}")

    slope_test_alt("p_cal", "calibrated")
    slope_test_alt("p_raw", "raw")

    # ---- edge buckets (calibrated) -------------------------------------------
    print("\ncalibrated edge buckets (edge = p_cal - chosen_mkt_price):")
    buckets = [(-9.0, 0.0), (0.0, 0.02), (0.02, 0.04), (0.04, 0.07), (0.07, 0.12), (0.12, 9.0)]
    print(f"{'bucket':>14} {'n':>5} {'win_rate':>8} {'mean_entry':>10} {'net$':>9} {'gain%':>7}")
    n_bucketed = 0
    for lo, hi in buckets:
        b = [
            r
            for r in rows
            if r["p_cal"] is not None and r["price"] is not None and lo <= r["p_cal"] - r["price"] < hi
        ]
        if not b:
            continue
        n_bucketed += len(b)
        known = [r for r in b if r["won"] is not None]
        wr = sum(1 for r in known if r["won"]) / len(known) if known else float("nan")
        me = sum(r["entry"] for r in b) / len(b)
        mmp = sum(r["price"] for r in b) / len(b)
        net = sum(r["pnl"] for r in b)
        mg = 100 * sum(r["gain"] for r in b) / len(b)
        print(
            f"{lo:>+6.2f}..{hi:<+6.2f} {len(b):>5} {wr:>8.3f} {me:>10.3f} {net:>+9.2f} {mg:>+7.2f}"
            f"   (mean_mkt_price={mmp:.3f})"
        )
    print(f"bucketed n total: {n_bucketed}")
    raw_b = Counter()
    for r in rows:
        if r["p_raw"] is None or r["price"] is None:
            continue
        e = r["p_raw"] - r["price"]
        for lo, hi in buckets:
            if lo <= e < hi:
                raw_b[(lo, hi)] += 1
    print("raw-edge bucket counts:", {f"{lo}..{hi}": n for (lo, hi), n in sorted(raw_b.items())})

    # ---- fee sanity: reconstruct pnl from recorded prices --------------------
    res = [r for r in rows if r["er"] == "resolution"][:20]
    resid = []
    for r in res:
        shares = r["size"] / r["entry"]
        recon = shares * r["exitp"] - r["size"] - r["fees"]
        resid.append((recon - r["pnl"]) / r["size"] * 100)
    print(
        f"\nfee sanity (first 20 resolution trades): recon-from-entry_price minus disk pnl, % of size: "
        f"mean_abs={sum(abs(x) for x in resid) / len(resid):.3f} max={max(abs(x) for x in resid):.2f} "
        f"mean_signed={sum(resid) / len(resid):+.3f}"
    )
    # broader: all resolution trades
    resid_all = []
    for r in [r for r in rows if r["er"] == "resolution"]:
        shares = r["size"] / r["entry"]
        recon = shares * r["exitp"] - r["size"] - r["fees"]
        resid_all.append((recon - r["pnl"]) / r["size"] * 100)
    neg = sum(1 for x in resid_all if x < -0.05)
    print(
        f"all {len(resid_all)} resolution trades: mean_signed={sum(resid_all) / len(resid_all):+.3f}% "
        f"mean_abs={sum(abs(x) for x in resid_all) / len(resid_all):.3f}% "
        f"frac_recon_below_disk(>0.05%)={neg / len(resid_all):.3f}"
    )
    # fee formula spot-check: fees == 0.07 * shares * p * (1-p) at recorded entry?
    ferr = []
    for r in res:
        shares = r["size"] / r["entry"]
        ferr.append(abs(r["fees"] - 0.07 * shares * r["entry"] * (1 - r["entry"])))
    print(f"fee-formula |resid| on same 20 (vs 0.07*shares*p*(1-p) at recorded entry): max={max(ferr):.4f}")

    # ---- per-day pnl table ----------------------------------------------------
    print(f"\n{'day':>12} {'n':>5} {'net':>9} {'scalp':>9} {'resol':>9}")
    for d in days:
        sel = [r for r in rows if r["day"] == d]
        sc = sum(r["pnl"] for r in sel if r["er"] != "resolution")
        rs = sum(r["pnl"] for r in sel if r["er"] == "resolution")
        print(f"{d:>12} {len(sel):>5} {sc + rs:>+9.2f} {sc:>+9.2f} {rs:>+9.2f}")


if __name__ == "__main__":
    main()
