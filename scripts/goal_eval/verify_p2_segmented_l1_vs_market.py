"""Adversarial re-derivation of p2-segmented-l1-vs-market.

Independent implementation (written BEFORE reading the analysis agent's script):
  Pool A = deduped outcomes; winner = outcome.correct for resolution exits,
           CF resolution_price==1.0 for scalps; scalps without a CF arm excluded.
  Pool B = pool A + resolved ghosts (ghost_correct).
  diff   = brier_market - brier_model (positive = model better), per record,
           predictors: model_probability_raw (chosen side) and re-derived L1-only
           chosen-side prob (production scheduler replay recipe: dynamic ATR floor,
           min_atr=12.0, floor frac 0.30, short warmup 5, long-term min 50,
           atr_sigma_ratio=1.3, student_t_df=5, autocorr vol scale, clip 1e-6).
  Day-clustered t over ET days; day-clustered bootstrap seed 42 / 1000 iters.

Run: python scripts/goal_eval/verify_p2_segmented_l1_vs_market.py
"""
from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import pstdev
from zoneinfo import ZoneInfo

from scipy.special import stdtr

MEM = Path(__file__).resolve().parent.parent.parent / "polybot" / "memory"
ET = ZoneInfo("America/New_York")

# production constants (signal_engine.py / settings.yaml, read-only)
ATR_SIGMA_RATIO = 1.3
STUDENT_T_DF = 5
MIN_ATR = 12.0
ATR_SHIFT_THRESH = 0.60
FLOOR_FRAC = 0.30
SHORT_MAX, SHORT_MIN = 20, 5
LONG_MAX, LONG_MIN = 200, 50  # production _ATR_LONG_TERM_MIN_SAMPLES = 50
L1_CLIP = 1e-6


def load_records(dirname: str) -> list[dict]:
    out = []
    for f in sorted((MEM / dirname).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def dedupe_by_pid(records: list[dict]) -> tuple[list[dict], int]:
    best: dict[int, dict] = {}
    for r in records:
        pid = r.get("position_id")
        if pid is None:
            continue
        ts = r.get("timestamp") or ""
        if pid not in best or ts > (best[pid].get("timestamp") or ""):
            best[pid] = r
    dropped = len(records) - len(best)
    return list(best.values()), dropped


def et_day(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(ET).date().isoformat()


class AtrFloor:
    """Mirrors signal_engine._record_atr + _effective_atr_floor (scheduler replay)."""

    def __init__(self, long_min: int = LONG_MIN):
        self.short: list[float] = []
        self.long: list[float] = []
        self.long_min = long_min

    def update_and_floor(self, atr_raw: float) -> float:
        if atr_raw > 0:
            self.short.append(atr_raw)
            if len(self.short) > SHORT_MAX:
                self.short.pop(0)
            self.long.append(atr_raw)
            if len(self.long) > LONG_MAX:
                self.long.pop(0)
        n_short = len(self.short)
        if n_short < SHORT_MIN:
            return MIN_ATR
        rolling = sum(self.short) / n_short
        base = max(MIN_ATR, FLOOR_FRAC * rolling)
        if len(self.long) >= self.long_min:
            lt = sum(self.long) / len(self.long)
            if lt > 0 and rolling / lt < ATR_SHIFT_THRESH:
                base = max(base, lt * ATR_SHIFT_THRESH * FLOOR_FRAC)
        return base


def p_l1_chosen(ctx: dict, side: str, floor: AtrFloor) -> float | None:
    btc = ctx.get("btc_price") or 0
    strike = ctx.get("strike_price") or 0
    atr_raw = ctx.get("atr") or 0
    secs = ctx.get("seconds_remaining") or 0
    if btc <= 0 or strike <= 0 or secs <= 0 or atr_raw <= 0:
        return None
    f = floor.update_and_floor(atr_raw)
    atr_eff = max(atr_raw, f)
    ac = ctx.get("regime_autocorr")
    if ac is None:
        rs = (ctx.get("regime_state") or "").lower()
        ac = 0.20 if rs.startswith("trending") else (-0.20 if rs.startswith("mean") else 0.0)
    ac_c = max(-0.5, min(0.5, float(ac)))
    vol = (atr_eff / ATR_SIGMA_RATIO) * math.sqrt(max(secs / 60.0, 0.01)) * math.sqrt((1 + ac_c) / (1 - ac_c))
    z = (btc - strike) / vol
    t_scale = math.sqrt(STUDENT_T_DF / (STUDENT_T_DF - 2))
    pu = float(stdtr(STUDENT_T_DF, z * t_scale))
    pu = max(L1_CLIP, min(1 - L1_CLIP, pu))
    return pu if side == "Up" else 1.0 - pu


def day_t(diffs_by_day: dict[str, list[float]]):
    means = [sum(v) / len(v) for v in diffs_by_day.values()]
    nd = len(means)
    if nd < 2:
        return None, nd, None
    m = sum(means) / nd
    sd = pstdev(means)
    t = m / (sd / math.sqrt(nd)) if sd > 0 else float("inf")
    pos = sum(1 for x in means if x > 0)
    return t, nd, pos


def boot(rows_by_day: dict[str, list[float]]):
    days = sorted(rows_by_day)
    random.seed(42)
    stats = []
    for _ in range(1000):
        sample = random.choices(days, k=len(days))
        vals = [v for d in sample for v in rows_by_day[d]]
        stats.append(sum(vals) / len(vals))
    stats.sort()
    return (sum(stats) / len(stats), stats[int(0.10 * len(stats))], stats[int(0.90 * len(stats))])


def main() -> None:
    trades_all = load_records("outcomes")
    ghosts = load_records("ghost_outcomes")
    cfs_all = load_records("counterfactuals")

    trades, dup_out = dedupe_by_pid(trades_all)
    cfs, dup_cf = dedupe_by_pid(cfs_all)
    # exact-duplicate ghost check (no position_id)
    gkeys = [(g.get("market_id"), g.get("gate_name"), g.get("recorded_at")) for g in ghosts]
    ghost_dupes = len(gkeys) - len(set(gkeys))
    print(f"loaded: outcomes={len(trades_all)} (dupes dropped {dup_out}), "
          f"cfs={len(cfs_all)} (dupes dropped {dup_cf}), ghosts={len(ghosts)} "
          f"(exact-dup ghost keys: {ghost_dupes})")

    cf_res: dict[int, bool] = {}
    for c in cfs:
        cf = c.get("counterfactual") or {}
        rp = cf.get("resolution_price")
        if (c.get("actual") or {}).get("exit_reason") != "hold" and rp in (0.0, 1.0):
            cf_res[c["position_id"]] = rp == 1.0
    # sensitivity: cf winners from NON-deduped CFs
    cf_res_all: dict[int, bool] = {}
    for c in cfs_all:
        cf = c.get("counterfactual") or {}
        rp = cf.get("resolution_price")
        if (c.get("actual") or {}).get("exit_reason") != "hold" and rp in (0.0, 1.0):
            cf_res_all[c["position_id"]] = rp == 1.0

    # ---- pool A ------------------------------------------------------------
    poolA = []
    n_res = n_scalp_cf = n_excl_no_cf = n_missing = 0
    cf_vs_correct_mismatch = 0
    for t in sorted(trades, key=lambda r: r.get("timestamp") or ""):
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        p_raw = ctx.get("model_probability_raw")
        side = t.get("side")
        price = ctx.get("market_price_down" if side == "Down" else "market_price_up")
        if p_raw is None or price is None or side not in ("Up", "Down"):
            n_missing += 1
            continue
        if t.get("exit_reason") == "resolution":
            won = bool(t.get("correct"))
            n_res += 1
        elif t.get("position_id") in cf_res:
            won = cf_res[t["position_id"]]
            n_scalp_cf += 1
            if t.get("correct") is not None and bool(t.get("correct")) != won:
                cf_vs_correct_mismatch += 1
        else:
            n_excl_no_cf += 1
            continue
        poolA.append(dict(ts=t["timestamp"], day=et_day(t["timestamp"]), side=side,
                          p_raw=float(p_raw), price=float(price), won=won, ctx=ctx,
                          kind="trade"))
    n_excl_dedup_sensitive = sum(
        1 for t in trades
        if t.get("exit_reason") != "resolution"
        and t.get("position_id") not in cf_res
        and t.get("position_id") in cf_res_all)
    print(f"poolA: n={len(poolA)} resolution={n_res} scalp_via_cf={n_scalp_cf} "
          f"excluded_no_cf={n_excl_no_cf} missing_fields={n_missing}")
    print(f"  scalp CF-winner vs outcome.correct mismatches: {cf_vs_correct_mismatch}"
          f" | exclusions rescued by non-deduped CFs: {n_excl_dedup_sensitive}")

    # ---- pool B ------------------------------------------------------------
    ghost_rows = []
    g_unres = g_missing = 0
    for g in ghosts:
        if not g.get("resolved"):
            g_unres += 1
            continue
        ctx = (g.get("indicator_snapshot") or {}).get("trade_context") or {}
        p_raw = ctx.get("model_probability_raw")
        side = g.get("side")
        price = ctx.get("market_price_down" if side == "Down" else "market_price_up")
        if p_raw is None or price is None or side not in ("Up", "Down"):
            g_missing += 1
            continue
        ghost_rows.append(dict(ts=g["timestamp"], day=et_day(g["timestamp"]), side=side,
                               p_raw=float(p_raw), price=float(price),
                               won=bool(g.get("ghost_correct")), ctx=ctx, kind="ghost"))
    poolB = sorted(poolA + ghost_rows, key=lambda r: r["ts"])
    print(f"poolB: n={len(poolB)} ghosts_included={len(ghost_rows)} "
          f"(ghost unresolved={g_unres}, ghost missing fields={g_missing})")

    # ---- p_l1 per pool (buffers advance over that pool's chronology) -------
    for pool, label in ((poolA, "A"), (poolB, "B")):
        fl = AtrFloor()
        n_ok = 0
        for r in sorted(pool, key=lambda r: r["ts"]):
            p = p_l1_chosen(r["ctx"], r["side"], fl)
            r[f"p_l1"] = p
            if p is not None:
                n_ok += 1
        print(f"p_l1 derivable pool {label}: {n_ok}/{len(pool)}")

    days = sorted({r["day"] for r in poolA})
    print(f"n_days={len(days)} range={days[0]}..{days[-1]}")

    def diff(r, pred):
        p = r["p_raw"] if pred == "raw" else r["p_l1"]
        if p is None:
            return None
        y = 1.0 if r["won"] else 0.0
        return (r["price"] - y) ** 2 - (p - y) ** 2

    def stats(pool, pred):
        by_day = defaultdict(list)
        for r in pool:
            d = diff(r, pred)
            if d is not None:
                by_day[r["day"]].append(d)
        all_d = [v for vs in by_day.values() for v in vs]
        t, nd, pos = day_t(by_day)
        return dict(n=len(all_d), mean=sum(all_d) / len(all_d) if all_d else None,
                    t=t, n_days=nd, days_pos=pos, by_day=by_day)

    print("\n== overall (diff = brier_mkt - brier_model; + = model better) ==")
    print(f"{'pool/pred':>14} {'n':>5} {'mean_diff':>10} {'t_day':>7} {'n_days':>6} {'days+':>5}")
    overall = {}
    for pool, pl in ((poolA, "A"), (poolB, "B")):
        for pred in ("raw", "l1"):
            s = stats(pool, pred)
            overall[(pl, pred)] = s
            print(f"{pl + '/' + pred:>14} {s['n']:>5} {s['mean']:>+10.5f} {s['t']:>7.2f} "
                  f"{s['n_days']:>6} {s['days_pos']:>5}")

    print("\n== bootstrap pool A (seed 42, 1000 day resamples) ==")
    for pred in ("raw", "l1"):
        m, p10, p90 = boot(overall[("A", pred)]["by_day"])
        print(f"  {pred}: mean={m:+.5f} p10={p10:+.5f} p90={p90:+.5f}")

    # ---- depth coverage ----------------------------------------------------
    for pool, pl in ((poolA, "A"), (poolB, "B")):
        cov = sum(1 for r in pool if r["ctx"].get("depth_usd_top20") is not None)
        print(f"depth_usd_top20 coverage pool {pl}: {cov}/{len(pool)} = {100*cov/len(pool):.1f}%")

    # ---- segments (pool A, both predictors) --------------------------------
    atrs = sorted(r["ctx"].get("atr") or 0 for r in poolA)
    t1, t2 = atrs[len(atrs) // 3], atrs[2 * len(atrs) // 3]
    print(f"\natr terciles: T1<={t1:.1f} < T2 <= {t2:.1f} < T3")

    def seg_def(r):
        ctx = r["ctx"]
        out = []
        rs = ctx.get("regime_state")
        if rs:
            out.append(f"regime_state={rs}")
        a = ctx.get("atr") or 0
        out.append("atr_T1" if a <= t1 else ("atr_T2" if a <= t2 else "atr_T3"))
        ac = ctx.get("regime_autocorr")
        if ac is not None:
            out.append("ac_trend(>+0.15)" if ac > 0.15 else ("ac_revert(<-0.15)" if ac < -0.15 else "ac_neutral"))
        out.append(f"side={r['side']}")
        ph = ctx.get("entry_phase")
        if ph:
            out.append(f"phase={ph}")
        out.append(f"is_flip={ctx.get('is_flip')}")
        secs = ctx.get("seconds_remaining") or 0
        out.append("secs>=240" if secs >= 240 else ("secs180-240" if secs >= 180 else "secs<180"))
        p = r["price"]
        out.append("price<0.45" if p < 0.45 else ("price0.45-0.55" if p <= 0.55 else "price>0.55"))
        edge = r["p_raw"] - p
        out.append("edge<0.07" if edge < 0.07 else ("edge0.07-0.12" if edge < 0.12 else "edge>=0.12"))
        return out

    segs = defaultdict(list)
    for r in poolA:
        for s in seg_def(r):
            segs[s].append(r)

    print(f"\n== pool A segments x 2 predictors (pass bar: n>=30, t>2, days+>=3) ==")
    print(f"{'segment':>24} {'pred':>4} {'n':>5} {'mean_diff':>10} {'t':>7} {'nd':>3} {'d+':>3} pass")
    survivors = []
    rows_out = []
    for name in sorted(segs):
        for pred in ("raw", "l1"):
            s = stats(segs[name], pred)
            if s["n"] == 0 or s["t"] is None:
                continue
            ok = s["n"] >= 30 and s["t"] > 2 and s["days_pos"] >= 3
            if ok:
                survivors.append((name, pred, s))
            rows_out.append((name, pred, s, ok))
            print(f"{name:>24} {pred:>4} {s['n']:>5} {s['mean']:>+10.5f} {s['t']:>7.2f} "
                  f"{s['n_days']:>3} {s['days_pos']:>3} {'PASS' if ok else ''}")
    print(f"\nsurvivors: {[(n, p) for n, p, _ in survivors] or 'NONE'}")

    # sensitivity: long-term warmup 200 instead of production 50 (their caveat wording)
    fl200 = AtrFloor(long_min=200)
    diffs200 = defaultdict(list)
    for r in sorted(poolA, key=lambda r: r["ts"]):
        p = p_l1_chosen(r["ctx"], r["side"], fl200)
        if p is None:
            continue
        y = 1.0 if r["won"] else 0.0
        diffs200[r["day"]].append((r["price"] - y) ** 2 - (p - y) ** 2)
    t200, nd200, _ = day_t(diffs200)
    alld = [v for vs in diffs200.values() for v in vs]
    print(f"sensitivity poolA l1 with long_min=200: mean={sum(alld)/len(alld):+.5f} t={t200:.2f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
