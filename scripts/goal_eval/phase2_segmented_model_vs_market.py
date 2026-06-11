"""Phase 2 — segmented model-vs-market Brier comparison.

The model loses to the market price on average (paired per-day Brier,
t=2.23 market-better). This script tests SEGMENTS: does any pocket of
decisions exist where the model (full stack p_raw, or L1-only p_l1)
beats the chosen-side market price with day-clustered significance?

Pools:
  A — closed trades only (resolution outcome via `correct`, scalps via
      counterfactual resolution_price; scalps without a CF excluded).
  B — trades + resolved ghosts (sensitivity).

Pass bar (judged on pool A): n>=30 AND day-clustered t > 2 (model better)
AND per-day paired diff positive on >=3 distinct days.

Run: python scripts/goal_eval/phase2_segmented_model_vs_market.py
"""
from __future__ import annotations

import json
import math
import random
import statistics as st
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from polybot.core.aux_layers import (  # noqa: E402
    MIN_STUDENT_T_DF,
    autocorr_vol_scale,
    student_t_cdf,
)
from polybot.core.signal_engine import (  # noqa: E402
    _ATR_FLOOR_FRACTION,
    _ATR_HISTORY_MIN_SAMPLES,
    _ATR_LONG_TERM_MIN_SAMPLES,
)

MEM = REPO / "polybot" / "memory"
ET = ZoneInfo("America/New_York")

# L1 constants (settings.yaml values, mirrored from scheduler._kelly_bankroll_returns)
ATR_SIGMA_RATIO = 1.3
STUDENT_T_DF = 5
MIN_ATR = 12.0
ATR_REGIME_SHIFT_THRESHOLD = 0.60


# ---------------------------------------------------------------- loading
def load_records(dirname: str) -> list[dict]:
    out = []
    for f in sorted((MEM / dirname).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def dedupe_by_pid(records: list[dict], label: str) -> list[dict]:
    """Keep the record with the latest timestamp per position_id."""
    best: dict = {}
    no_pid = []
    for r in records:
        pid = r.get("position_id")
        if pid is None:
            no_pid.append(r)
            continue
        ts = r.get("timestamp") or ""
        if pid not in best or ts > (best[pid].get("timestamp") or ""):
            best[pid] = r
    dropped = len(records) - len(best) - len(no_pid)
    print(f"  {label}: {len(records)} loaded -> {len(best) + len(no_pid)} "
          f"after dedupe ({dropped} duplicates dropped, {len(no_pid)} without position_id)")
    return list(best.values()) + no_pid


def et_day(ts: str) -> str | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%Y-%m-%d")
    except (ValueError, AttributeError, TypeError):
        return None


# ------------------------------------------------------------ row building
def build_rows():
    trades = dedupe_by_pid(load_records("outcomes"), "outcomes")
    cfs = dedupe_by_pid(load_records("counterfactuals"), "counterfactuals")
    ghosts = load_records("ghost_outcomes")

    # hold-to-resolution winner for scalped trades, via the CF resolution price
    cf_res: dict[int, bool] = {}
    for c in cfs:
        cf = c.get("counterfactual") or {}
        rp = cf.get("resolution_price")
        if (c.get("actual") or {}).get("exit_reason") != "hold" and rp in (0.0, 1.0):
            cf_res[c["position_id"]] = rp == 1.0

    rows = []
    skipped = defaultdict(int)
    for t in trades:
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        side = (t.get("side") or "")
        p_raw = ctx.get("model_probability_raw")
        p_mkt = ctx.get("market_price_down" if side == "Down" else "market_price_up")
        if p_raw is None or p_mkt is None or not (0 < p_raw < 1) or not (0 < p_mkt < 1):
            skipped["trade_missing_prob_or_price"] += 1
            continue
        if t.get("exit_reason") == "resolution":
            won = bool(t.get("correct"))
            basis = "resolution"
        elif t.get("position_id") in cf_res:
            won = cf_res[t["position_id"]]
            basis = "scalp_cf"
        else:
            skipped["scalp_no_cf"] += 1
            continue
        day = et_day(t.get("timestamp") or "")
        if day is None:
            skipped["trade_bad_timestamp"] += 1
            continue
        rows.append(_mk_row(ctx, side, p_raw, p_mkt, won, day,
                            t.get("timestamp") or "", "trade", basis))

    n_ghost_unresolved = 0
    for g in ghosts:
        if not g.get("resolved") or g.get("ghost_correct") is None:
            n_ghost_unresolved += 1
            continue
        ctx = (g.get("indicator_snapshot") or {}).get("trade_context") or {}
        side = (g.get("side") or "")
        p_raw = ctx.get("model_probability_raw")
        p_mkt = ctx.get("market_price_down" if side == "Down" else "market_price_up")
        if p_raw is None or p_mkt is None or not (0 < p_raw < 1) or not (0 < p_mkt < 1):
            skipped["ghost_missing_prob_or_price"] += 1
            continue
        day = et_day(g.get("timestamp") or "")
        if day is None:
            skipped["ghost_bad_timestamp"] += 1
            continue
        rows.append(_mk_row(ctx, side, p_raw, p_mkt, bool(g["ghost_correct"]), day,
                            g.get("timestamp") or "", "ghost", "ghost"))

    skipped["ghost_unresolved"] = n_ghost_unresolved
    print(f"  skips: {dict(skipped)}")
    return rows


def _mk_row(ctx, side, p_raw, p_mkt, won, day, ts, kind, basis):
    btc = ctx.get("btc_price") or 0
    strike = ctx.get("strike_price") or 0
    atr = ctx.get("atr") or 0
    mpu, mpd = ctx.get("market_price_up"), ctx.get("market_price_down")
    return dict(
        kind=kind, basis=basis, side=side, won=won, day=day, ts=ts,
        p_raw=p_raw, p_mkt=p_mkt,
        btc=btc, strike=strike, atr=atr,
        secs=ctx.get("seconds_remaining") or 0,
        autocorr=ctx.get("regime_autocorr"),
        regime_state=ctx.get("regime_state"),
        depth=ctx.get("depth_usd_top20"),
        spread=(mpu + mpd - 1.0) if (mpu is not None and mpd is not None) else None,
        dist_atr=(abs(btc - strike) / atr) if (btc > 0 and strike > 0 and atr > 0) else None,
    )


# ------------------------------------------------------- L1 re-derivation
def derive_l1(rows: list[dict], key: str) -> None:
    """Mirror scheduler._kelly_bankroll_returns L1 block: rolling 20/200
    ATR-floor buffers advanced once per record in chronological order."""
    atr_short: deque = deque(maxlen=20)
    atr_long: deque = deque(maxlen=200)
    s_sum = l_sum = 0.0
    for r in sorted(rows, key=lambda x: x["ts"]):
        btc, strike, atr_raw, secs = r["btc"], r["strike"], r["atr"], r["secs"]
        if btc <= 0 or strike <= 0 or secs <= 0 or atr_raw <= 0:
            r[key] = None
            continue
        # buffers advance BEFORE the floor is read (mirrors _record_atr ordering)
        if len(atr_short) == atr_short.maxlen:
            s_sum -= atr_short[0]
        atr_short.append(atr_raw)
        s_sum += atr_raw
        if len(atr_long) == atr_long.maxlen:
            l_sum -= atr_long[0]
        atr_long.append(atr_raw)
        l_sum += atr_raw

        n_short = len(atr_short)
        if n_short >= _ATR_HISTORY_MIN_SAMPLES:
            rolling_short = s_sum / n_short
            base_floor = max(MIN_ATR, _ATR_FLOOR_FRACTION * rolling_short)
            n_long = len(atr_long)
            if n_long >= _ATR_LONG_TERM_MIN_SAMPLES:
                rolling_long = l_sum / n_long
                if rolling_long > 0 and rolling_short / rolling_long < ATR_REGIME_SHIFT_THRESHOLD:
                    base_floor = max(base_floor,
                                     rolling_long * ATR_REGIME_SHIFT_THRESHOLD * _ATR_FLOOR_FRACTION)
            atr_eff = max(atr_raw, base_floor)
        else:
            atr_eff = max(atr_raw, MIN_ATR)

        ac = r["autocorr"]
        if ac is None:
            rs = (r["regime_state"] or "").lower()
            ac = 0.20 if rs.startswith("trending") else (-0.20 if rs.startswith("mean") else 0.0)
        df_eff = max(MIN_STUDENT_T_DF, STUDENT_T_DF)
        minutes = max(secs / 60.0, 0.01)
        vol = (atr_eff / ATR_SIGMA_RATIO) * math.sqrt(minutes) * autocorr_vol_scale(float(ac))
        z = ((btc - strike) / vol) * math.sqrt(df_eff / (df_eff - 2))
        cdf = max(1e-6, min(1 - 1e-6, student_t_cdf(z, df_eff)))
        r[key] = cdf if r["side"] != "Down" else 1.0 - cdf


# ------------------------------------------------------------ statistics
def paired_day_stats(rows: list[dict], model_key: str) -> dict | None:
    """Per-decision Brier diff (brier_market - brier_model); per-ET-day means;
    day-clustered t. POSITIVE mean = model better."""
    valid = [r for r in rows if r.get(model_key) is not None]
    if not valid:
        return None
    by_day = defaultdict(list)
    for r in valid:
        w = 1.0 if r["won"] else 0.0
        d = (r["p_mkt"] - w) ** 2 - (r[model_key] - w) ** 2
        by_day[r["day"]].append(d)
    day_means = [st.mean(v) for v in by_day.values()]
    n_days = len(day_means)
    mean_diff = st.mean(day_means)
    if n_days >= 2:
        sd = st.pstdev(day_means)
        t = mean_diff / (sd / math.sqrt(n_days)) if sd > 0 else float("nan")
    else:
        t = float("nan")
    return dict(n=len(valid), n_days=n_days, mean_diff=mean_diff, t=t,
                days_pos=sum(1 for d in day_means if d > 0))


def day_bootstrap(rows: list[dict], model_key: str, iters: int = 1000):
    """Day-clustered bootstrap of the mean per-decision paired Brier diff."""
    valid = [r for r in rows if r.get(model_key) is not None]
    by_day = defaultdict(list)
    for r in valid:
        w = 1.0 if r["won"] else 0.0
        by_day[r["day"]].append((r["p_mkt"] - w) ** 2 - (r[model_key] - w) ** 2)
    days = sorted(by_day)
    random.seed(42)
    stats = []
    for _ in range(iters):
        sample = [d for day in random.choices(days, k=len(days)) for d in by_day[day]]
        stats.append(st.mean(sample))
    stats.sort()
    return st.mean(stats), stats[int(0.10 * iters)], stats[int(0.90 * iters)]


# ------------------------------------------------------------- segments
def tercile_fn(rows, field, label):
    vals = sorted(r[field] for r in rows if r.get(field) is not None and r[field] > 0)
    if len(vals) < 9:
        return None, None
    c1, c2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    names = (f"{label} T1(<={c1:.0f})", f"{label} T2({c1:.0f}-{c2:.0f})", f"{label} T3(>{c2:.0f})")

    def fn(r):
        v = r.get(field)
        if v is None or v <= 0:
            return None
        return names[0] if v <= c1 else (names[1] if v <= c2 else names[2])
    return fn, (c1, c2)


def regime_bucket(r):
    a = r.get("autocorr")
    if a is None:
        return None
    return "trend(ac>+0.15)" if a > 0.15 else ("meanrev(ac<-0.15)" if a < -0.15 else "neutral")


def time_bucket(r):
    s = r["secs"]
    return "early(>=240s)" if s >= 240 else ("mid(120-240s)" if s >= 120 else "late(<120s)")


def spread_bucket(r):
    s = r.get("spread")
    if s is None:
        return None
    return "sum<=1.00" if s <= 0 else ("sum1.00-1.02" if s <= 0.02 else "sum>1.02")


def dist_bucket(r):
    d = r.get("dist_atr")
    if d is None:
        return None
    return "dist<0.5atr" if d < 0.5 else ("dist0.5-1.0atr" if d < 1.0 else "dist>=1.0atr")


# ------------------------------------------------------------------ main
def main() -> None:
    print("== load & dedupe ==")
    rows = build_rows()
    pool_a = [r for r in rows if r["kind"] == "trade"]
    pool_b = rows
    derive_l1(pool_a, "p_l1_A")          # buffers advanced over trades only
    derive_l1(pool_b, "p_l1_B")          # buffers advanced over trades + ghosts

    n_res = sum(1 for r in pool_a if r["basis"] == "resolution")
    n_scalp = sum(1 for r in pool_a if r["basis"] == "scalp_cf")
    days_a = sorted({r["day"] for r in pool_a})
    print(f"\npool A (trades): n={len(pool_a)} (resolution {n_res}, scalp-via-CF {n_scalp}), "
          f"n_days={len(days_a)} ({days_a[0]}..{days_a[-1]})")
    print(f"pool B (trades+ghosts): n={len(pool_b)} "
          f"(ghosts {len(pool_b) - len(pool_a)})")
    n_l1_a = sum(1 for r in pool_a if r.get("p_l1_A") is not None)
    print(f"p_l1 derivable: pool A {n_l1_a}/{len(pool_a)}, "
          f"pool B {sum(1 for r in pool_b if r.get('p_l1_B') is not None)}/{len(pool_b)}")
    depth_cov_a = sum(1 for r in pool_a if r.get("depth") is not None)
    print(f"depth_usd_top20 coverage: pool A {depth_cov_a}/{len(pool_a)} "
          f"({100 * depth_cov_a / len(pool_a):.1f}%), "
          f"pool B {sum(1 for r in pool_b if r.get('depth') is not None)}/{len(pool_b)} "
          f"({100 * sum(1 for r in pool_b if r.get('depth') is not None) / len(pool_b):.1f}%)")

    pools = [("A", pool_a, "p_l1_A"), ("B", pool_b, "p_l1_B")]
    predictors = lambda l1key: [("raw", "p_raw"), ("l1", l1key)]

    # ---- overall -------------------------------------------------------
    print("\n== OVERALL (paired per-day Brier diff: market - model; POSITIVE = model better) ==")
    hdr = (f"{'pool':>4} {'pred':>4} {'n':>6} {'n_days':>6} {'mean_diff':>10} "
           f"{'t_day':>7} {'days+':>6}")
    print(hdr)
    overall = {}
    for pname, prows, l1key in pools:
        for plabel, pkey in predictors(l1key):
            s = paired_day_stats(prows, pkey)
            overall[(pname, plabel)] = s
            print(f"{pname:>4} {plabel:>4} {s['n']:>6} {s['n_days']:>6} "
                  f"{s['mean_diff']:>+10.5f} {s['t']:>7.2f} {s['days_pos']:>3}/{s['n_days']}")
    for plabel, pkey in [("raw", "p_raw"), ("l1", "p_l1_A")]:
        m, p10, p90 = day_bootstrap(pool_a, pkey)
        print(f"  bootstrap pool A {plabel}-vs-mkt (1000 day-resamples, seed 42): "
              f"mean={m:+.5f}  p10={p10:+.5f}  p90={p90:+.5f}")

    # ---- segments -------------------------------------------------------
    seg_defs = [("Regime (autocorr +/-0.15)", regime_bucket),
                ("Regime state (stamped)", lambda r: r.get("regime_state") or None),
                ("Time-of-window", time_bucket),
                ("Spread proxy (up+down-1)", spread_bucket),
                ("Distance-from-strike", dist_bucket)]

    survivors = []
    n_tests = 0
    all_rows_out = []
    for seg_name, base_fn in seg_defs + [("__ATR__", None), ("__DEPTH__", None)]:
        print(f"\n== segment: "
              f"{'ATR terciles (within-pool)' if seg_name == '__ATR__' else 'Depth terciles (where stamped)' if seg_name == '__DEPTH__' else seg_name} ==")
        print(f"{'bucket':<26} {'pool':>4} {'pred':>4} {'n':>6} {'n_days':>6} "
              f"{'mean_diff':>10} {'t_day':>7} {'days+':>6}  flag")
        for pname, prows, l1key in pools:
            if seg_name == "__ATR__":
                fn, _ = tercile_fn(prows, "atr", "atr")
            elif seg_name == "__DEPTH__":
                fn, _ = tercile_fn(prows, "depth", "depth")
            else:
                fn = base_fn
            if fn is None:
                continue
            buckets = defaultdict(list)
            for r in prows:
                k = fn(r)
                if k is not None:
                    buckets[k].append(r)
            for bname in sorted(buckets):
                brows = buckets[bname]
                for plabel, pkey in predictors(l1key):
                    s = paired_day_stats(brows, pkey)
                    if s is None:
                        continue
                    if pname == "A":
                        n_tests += 1
                    passes = (pname == "A" and s["n"] >= 30
                              and s["t"] == s["t"] and s["t"] > 2.0
                              and s["days_pos"] >= 3 and s["mean_diff"] > 0)
                    flag = "<<< PASS" if passes else ""
                    print(f"{bname:<26} {pname:>4} {plabel:>4} {s['n']:>6} {s['n_days']:>6} "
                          f"{s['mean_diff']:>+10.5f} {s['t']:>7.2f} {s['days_pos']:>3}/{s['n_days']}  {flag}")
                    rec = dict(segment=bname, pool=pname, predictor=plabel, **s)
                    all_rows_out.append(rec)
                    if passes:
                        survivors.append(rec)

    # ---- verdict --------------------------------------------------------
    print(f"\n== verdict ==")
    print(f"segment tests run on pool A (bucket x predictor): {n_tests}")
    print(f"pass bar: n>=30, day-clustered t>2 model-better, per-day diff positive on >=3 days")
    if survivors:
        print(f"SURVIVORS ({len(survivors)}):")
        for s in survivors:
            print(f"  {s['segment']} [{s['predictor']}] n={s['n']} t={s['t']:.2f} "
                  f"days+={s['days_pos']}/{s['n_days']} mean_diff={s['mean_diff']:+.5f}")
        print(f"NOTE: with ~{n_tests} tests, expect ~{n_tests * 0.025:.1f} false positives at "
              f"t>2 one-sided by chance — a lone marginal survivor is a HYPOTHESIS, not an edge.")
    else:
        print(f"NO SURVIVORS — no segment with n>=30 where the model beats the market "
              f"at day-clustered t>2 with >=3 positive days, in either predictor.")
        print(f"(with ~{n_tests} tests even 1-2 marginal survivors would be expected by chance)")


if __name__ == "__main__":
    main()
