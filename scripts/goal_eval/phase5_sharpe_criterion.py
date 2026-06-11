"""Phase 5 — goal 'Done when' criterion + L1-only simplification candidate.

PART A: per-trade fold-replay Sharpe exactly as the adoption gate computes it
        (AgentScheduler._kelly_bankroll_returns + weighted_sharpe_from_returns)
        on the trailing 200 closed trades, with the PRODUCTION isotonic
        calibrator (identity as sensitivity). Plus realized-gain context and
        day-clustered bootstrap of realized $ pnl (trailing 200 + full pool).

PART B: "L1-only" candidate = L2-L6 weights zeroed (regime, flow, spot_flow,
        prev_margin, momentum; L6 already 0.0). Baseline vs candidate via the
        same replay machinery on (i) trailing 200, (ii) full real pool,
        (iii) full real + resolved ghosts (sched._ghost_to_outcome). Adoption
        z = delta / _jk_se(baseline_sharpe, n_cand, cand_returns), plus
        day-clustered t on per-day Kelly-return delta.

Run: python scripts/goal_eval/phase5_sharpe_criterion.py
"""
from __future__ import annotations

import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
MEM = ROOT / "polybot" / "memory"
ET = ZoneInfo("America/New_York")

from polybot.config.loader import load_config                      # noqa: E402
from polybot.agents.scheduler import AgentScheduler                 # noqa: E402
from polybot.agents.pipeline_analytics import (                     # noqa: E402
    weighted_sharpe_from_returns, sharpe, RECENCY_DECAY_PER_DAY)
from polybot.agents.weight_optimizer import _jk_se, ADOPTION_Z_FLOOR  # noqa: E402
from polybot.core.calibrator import IsotonicCalibrator               # noqa: E402

N_BOOT = 1000
TRAIL_N = 200


# ---------------------------------------------------------------- loading

def load_records(dirname: str) -> list[dict]:
    """Glob *.json under memory/<dirname>; flatten rollup arrays."""
    out = []
    for f in sorted((MEM / dirname).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def rec_latest_ts(r: dict):
    cands = [parse_ts(r.get("exit_timestamp")), parse_ts(r.get("timestamp"))]
    cands = [c for c in cands if c is not None]
    return max(cands) if cands else None


def dedupe_by_pid(records: list[dict], label: str) -> tuple[list[dict], int]:
    best: dict[int, tuple] = {}
    no_pid = 0
    for r in records:
        pid = r.get("position_id")
        if pid is None:
            no_pid += 1
            continue
        t = rec_latest_ts(r)
        cur = best.get(pid)
        if cur is None or (t is not None and (cur[0] is None or t > cur[0])):
            best[pid] = (t, r)
    kept = [v[1] for v in best.values()]
    dropped = len(records) - no_pid - len(kept)
    print(f"  {label:<16} loaded={len(records):>5}  no_pid={no_pid}  "
          f"dupes_dropped={dropped}  kept={len(kept)}")
    return kept, dropped


def sort_key(o: dict) -> float:
    """Scheduler convention: parse exit_timestamp (fallback timestamp) to epoch;
    failed parses sort to the front."""
    t = parse_ts(o.get("exit_timestamp") or o.get("timestamp"))
    return t.timestamp() if t else 0.0


def et_day(r: dict) -> str:
    t = parse_ts(r.get("timestamp"))
    return t.astimezone(ET).strftime("%Y-%m-%d") if t else "????-??-??"


# ------------------------------------------------- replay-side field check
# Mirrors the skip conditions at the top of _kelly_bankroll_returns' loop
# (rows the replay drops for missing/invalid fields, BEFORE any gate runs).

def field_valid(o: dict) -> bool:
    snap = o.get("indicator_snapshot", {})
    if not snap:
        return False
    ctx = snap.get("trade_context", {})
    stored_raw = ctx.get("model_probability_raw") or ctx.get("model_probability") or 0.0
    if not isinstance(stored_raw, (int, float)) or stored_raw <= 0 or stored_raw >= 1:
        return False
    side = (o.get("side") or "").lower()
    if side not in ("up", "down"):
        return False
    mp = (ctx.get("market_price_up", 0) if side == "up"
          else ctx.get("market_price_down", 0)) or 0
    if mp <= 0 or mp >= 1:
        return False
    btc = ctx.get("btc_price") or 0
    strike = ctx.get("strike_price") or 0
    atr_raw = ctx.get("atr") or 0
    secs = ctx.get("seconds_remaining") or 0
    if btc <= 0 or strike <= 0 or secs <= 0 or atr_raw <= 0:
        return False
    return True


# ---------------------------------------------------------------- bootstrap

def day_boot_pnl(records: list[dict], label: str) -> dict:
    """Day-clustered bootstrap of total realized $ pnl. seed 42, 1000 iters."""
    by_day = defaultdict(list)
    for r in records:
        if r.get("pnl") is not None:
            by_day[et_day(r)].append(r["pnl"])
    days = sorted(by_day)
    total = sum(p for v in by_day.values() for p in v)
    random.seed(42)
    stats = []
    for _ in range(N_BOOT):
        sampled = [random.choice(days) for _ in days]
        stats.append(sum(sum(by_day[d]) for d in sampled))
    stats.sort()
    mean = sum(stats) / len(stats)
    p10 = stats[int(0.10 * N_BOOT)]
    p90 = stats[int(0.90 * N_BOOT)]
    print(f"  {label:<28} n={sum(len(v) for v in by_day.values()):>5} n_days={len(days):>3} "
          f"actual=${total:>+9.2f}  boot mean=${mean:>+9.2f}  p10=${p10:>+9.2f}  p90=${p90:>+9.2f}")
    return {"n_days": len(days), "actual": total, "mean": mean, "p10": p10, "p90": p90}


# ---------------------------------------------------------------- replay

def make_replay(sched, cfg):
    sig = cfg["signal"]
    l4 = dict(sig["weights"])

    def replay(pool, calibrator, **overrides):
        kwargs = dict(
            outcomes=pool,
            recommended_weights=l4,
            momentum_weight=sig["momentum_weight"],
            atr_sigma_ratio=sig["atr_sigma_ratio"],
            student_t_df=sig["student_t_df"],
            min_edge=sig["min_edge"],
            calibrator=calibrator,
            kelly_fraction=cfg["math"]["kelly_fraction"],
            min_kelly=sig["min_kelly"],
            min_prob=sig["min_model_probability"],
        )
        kwargs.update(overrides)
        return sched._kelly_bankroll_returns(**kwargs)

    return replay


L1_ONLY = dict(
    momentum_weight=0.0,
    regime_weight=0.0,
    flow_weight=0.0,
    spot_flow_weight=0.0,
    prev_margin_weight=0.0,
)


def per_day_sums(replay, pool, calibrator, full_returns, **overrides) -> dict[str, float]:
    """Exact per-ET-day attribution of replay returns via prefix replays.

    The replay loop is strictly forward-causal (rolling ATR deques only), so
    returns of a prefix pool == prefix of full-pool returns. One prefix replay
    per contiguous same-day run; runs of the same day merged into one sum.
    """
    days = [et_day(r) for r in pool]
    runs = []  # (end_exclusive, day)
    start = 0
    for i in range(1, len(pool) + 1):
        if i == len(pool) or days[i] != days[start]:
            runs.append((i, days[start]))
            start = i
    day_sums: dict[str, float] = defaultdict(float)
    prev_count = 0
    for end, day in runs:
        prefix_returns, _ = replay(pool[:end], calibrator, **overrides)
        c = len(prefix_returns)
        # sanity: prefix property must hold exactly
        for a, b in zip(prefix_returns, full_returns[:c]):
            assert abs(a - b) < 1e-12, "prefix-replay mismatch — attribution invalid"
        day_sums[day] += sum(full_returns[prev_count:c])
        prev_count = c
    assert prev_count == len(full_returns)
    return dict(day_sums)


def day_t(deltas: list[float]) -> float:
    if len(deltas) < 2:
        return float("nan")
    sd = st.pstdev(deltas)
    if sd == 0:
        return float("nan")
    return st.mean(deltas) / (sd / math.sqrt(len(deltas)))


# ---------------------------------------------------------------- main

def main() -> None:
    print("=" * 78)
    print("LOAD (once)")
    print("=" * 78)
    raw_outcomes = load_records("outcomes")
    raw_ghosts = load_records("ghost_outcomes")
    raw_cfs = load_records("counterfactuals")

    outcomes, out_dropped = dedupe_by_pid(raw_outcomes, "outcomes")
    cfs, cf_dropped = dedupe_by_pid(raw_cfs, "counterfactuals")
    # ghosts carry no position_id — dedupe on (market_id, gate_name, recorded_at)
    seen, ghosts = set(), []
    for g in raw_ghosts:
        k = (g.get("market_id"), g.get("gate_name"), g.get("recorded_at"))
        if k in seen:
            continue
        seen.add(k)
        ghosts.append(g)
    print(f"  {'ghosts':<16} loaded={len(raw_ghosts):>5}  "
          f"dupes_dropped={len(raw_ghosts) - len(ghosts)}  kept={len(ghosts)}")

    outcomes.sort(key=sort_key)
    days_all = sorted({et_day(r) for r in outcomes})
    print(f"  pool spans ET days {days_all[0]} .. {days_all[-1]} ({len(days_all)} days)")

    cfg = load_config()
    sched = AgentScheduler(outcome_reviewer=None, bias_detector=None,
                           ta_evolver=None, weight_optimizer=None, config=cfg)
    sig = cfg["signal"]
    print(f"  config: atr_sigma_ratio={sig['atr_sigma_ratio']} df={sig['student_t_df']} "
          f"min_edge={sig['min_edge']} min_kelly={sig['min_kelly']} "
          f"min_prob={sig['min_model_probability']} momentum_weight={sig['momentum_weight']} "
          f"kelly_fraction={cfg['math']['kelly_fraction']} "
          f"realism={cfg['execution'].get('backtest_realism_factor')}")

    cal_prod = IsotonicCalibrator()
    cal_prod.load(MEM / "calibration" / "isotonic_params.json")
    cal_ident = IsotonicCalibrator()
    print(f"  production calibrator: identity={cal_prod.is_identity} knots={cal_prod.n_knots}")

    replay = make_replay(sched, cfg)

    # ---------------------------------------------------------- PART A
    print()
    print("=" * 78)
    print(f"PART A — CRITERION: fold-replay weighted Sharpe on trailing {TRAIL_N} trades")
    print("=" * 78)
    pool200 = outcomes[-TRAIL_N:]
    n_valid = sum(field_valid(o) for o in pool200)
    print(f"  trailing pool: {len(pool200)} trades "
          f"({et_day(pool200[0])} .. {et_day(pool200[-1])})")
    print(f"  rows passing replay field checks (n_replayed): {n_valid}/{len(pool200)} "
          f"(skipped for missing fields: {len(pool200) - n_valid})")

    ret_p, w_p = replay(pool200, cal_prod)
    sw_prod = weighted_sharpe_from_returns(ret_p, w_p)
    su_prod = sharpe(ret_p)
    ret_i, w_i = replay(pool200, cal_ident)
    sw_ident = weighted_sharpe_from_returns(ret_i, w_i)
    print(f"\n  [production isotonic]  n_entered={len(ret_p)}  "
          f"weighted_sharpe={sw_prod:+.4f}  unweighted_sharpe={su_prod:+.4f}  "
          f"sum_returns={sum(ret_p):+.4f}")
    print(f"  [identity sensitivity] n_entered={len(ret_i)}  "
          f"weighted_sharpe={sw_ident:+.4f}  unweighted_sharpe={sharpe(ret_i):+.4f}  "
          f"sum_returns={sum(ret_i):+.4f}")
    passes = sw_prod > 0.5
    print(f"\n  VERDICT (criterion: weighted fold-replay Sharpe > 0.5): "
          f"{sw_prod:+.4f} -> {'PASS' if passes else 'FAIL'}")

    # realized-gain context — NOT the criterion
    now_ts = datetime.now(timezone.utc).timestamp()
    gains, gw = [], []
    for o in pool200:
        g = o.get("gain_pct")
        if g is None:
            continue
        t = parse_ts(o.get("exit_timestamp") or o.get("timestamp"))
        days_ago = max(0.0, (now_ts - t.timestamp()) / 86400.0) if t else 0.0
        gains.append(g)
        gw.append(RECENCY_DECAY_PER_DAY ** days_ago)
    realized_sw = weighted_sharpe_from_returns(gains, gw)
    realized_su = sharpe(gains)
    pnl200 = sum(o.get("pnl") or 0.0 for o in pool200)
    print(f"\n  CONTEXT (NOT the criterion) — trailing {TRAIL_N} REALIZED gain_pct: "
          f"n={len(gains)}  weighted_sharpe={realized_sw:+.4f}  "
          f"unweighted_sharpe={realized_su:+.4f}  $pnl={pnl200:+.2f}")

    print(f"\n  Day-clustered bootstrap of realized $ pnl (seed 42, {N_BOOT} iters):")
    boot200 = day_boot_pnl(pool200, f"trailing {TRAIL_N}")
    bootfull = day_boot_pnl(outcomes, "full 14-day pool")
    print(f"  goal bar p10 > 0:  trailing{TRAIL_N}: "
          f"{'PASS' if boot200['p10'] > 0 else 'FAIL'} (p10={boot200['p10']:+.2f})   "
          f"full pool: {'PASS' if bootfull['p10'] > 0 else 'FAIL'} (p10={bootfull['p10']:+.2f})")

    # ---------------------------------------------------------- PART B
    print()
    print("=" * 78)
    print("PART B — L1-ONLY CANDIDATE (regime/flow/spot_flow/prev_margin/momentum = 0)")
    print("=" * 78)

    # pool (iii): real + resolved ghosts via the gate's own normalizer
    ghost_outcomes, ghost_skipped = [], 0
    for g in ghosts:
        norm = sched._ghost_to_outcome(g)
        if norm is None:
            ghost_skipped += 1
        else:
            ghost_outcomes.append(norm)
    combined = sorted(outcomes + ghost_outcomes, key=sort_key)
    print(f"  ghosts mapped via _ghost_to_outcome: kept={len(ghost_outcomes)} "
          f"skipped(None)={ghost_skipped} of {len(ghosts)}")

    pools = [
        (f"(i) trailing {TRAIL_N}", pool200),
        ("(ii) full real pool", outcomes),
        ("(iii) real + ghosts", combined),
    ]
    l1_results = {}
    for cal_label, cal in [("production isotonic", cal_prod), ("identity", cal_ident)]:
        print(f"\n  --- calibrator: {cal_label} ---")
        print(f"  {'pool':<22}{'arm':<10}{'n_ent':>6}{'w_sharpe':>10}{'sum_ret':>10}"
              f"{'d_sharpe':>10}{'z':>8}{'t_day':>8}{'n_days':>7}")
        for pool_label, pool in pools:
            rb, wb = replay(pool, cal)
            rc, wc = replay(pool, cal, **L1_ONLY)
            sb = weighted_sharpe_from_returns(rb, wb)
            sc = weighted_sharpe_from_returns(rc, wc)
            delta = sc - sb
            se = _jk_se(sb, len(rc), rc)
            z = delta / se if se > 0 else float("nan")
            db = per_day_sums(replay, pool, cal, rb)
            dc = per_day_sums(replay, pool, cal, rc, **L1_ONLY)
            all_days = sorted(set(db) | set(dc))
            deltas = [dc.get(d, 0.0) - db.get(d, 0.0) for d in all_days]
            t = day_t(deltas)
            print(f"  {pool_label:<22}{'baseline':<10}{len(rb):>6}{sb:>+10.4f}{sum(rb):>+10.4f}")
            print(f"  {'':<22}{'L1-only':<10}{len(rc):>6}{sc:>+10.4f}{sum(rc):>+10.4f}"
                  f"{delta:>+10.4f}{z:>+8.3f}{t:>+8.2f}{len(all_days):>7}")
            l1_results[(cal_label, pool_label)] = dict(
                n_base=len(rb), n_cand=len(rc), base=sb, cand=sc,
                delta=delta, z=z, t_day=t, n_days=len(all_days),
                sum_base=sum(rb), sum_cand=sum(rc),
                day_base=db, day_cand=dc)

    # per-day detail for the gate-convention pool (iii), production calibrator
    key = ("production isotonic", "(iii) real + ghosts")
    r = l1_results[key]
    print("\n  Per-day Kelly-return sums — pool (iii), production calibrator:")
    print(f"  {'ET day':<12}{'baseline':>10}{'L1-only':>10}{'delta':>10}")
    for d in sorted(set(r["day_base"]) | set(r["day_cand"])):
        b = r["day_base"].get(d, 0.0)
        c = r["day_cand"].get(d, 0.0)
        print(f"  {d:<12}{b:>+10.4f}{c:>+10.4f}{c - b:>+10.4f}")

    z3 = r["z"]
    if z3 >= ADOPTION_Z_FLOOR:
        verdict = "MEASURABLY IMPROVES (z >= 0.3, adoption-gate standard)"
    elif z3 <= -ADOPTION_Z_FLOOR:
        verdict = "MEASURABLY HURTS (z <= -0.3)"
    else:
        verdict = "INDISTINGUISHABLE (|z| < 0.3)"
    print(f"\n  VERDICT (pool iii, production cal): delta_sharpe={r['delta']:+.4f}  "
          f"z={z3:+.3f}  t_day={r['t_day']:+.2f} ({r['n_days']} days) -> {verdict}")

    # ------------------------------------------------------- key numbers
    print()
    print("=" * 78)
    print("KEY NUMBERS (machine-readable)")
    print("=" * 78)
    kn = {
        "criterion": {
            "sharpe_weighted": round(sw_prod, 4),
            "sharpe_unweighted": round(su_prod, 4),
            "n_replayed": n_valid,
            "n_entered": len(ret_p),
            "passes_gt_0_5": passes,
            "sharpe_weighted_identity_cal": round(sw_ident, 4),
        },
        "realized_context": {
            "trailing200_sharpe": round(realized_sw, 4),
            "trailing200_sharpe_unweighted": round(realized_su, 4),
            "trailing200_pnl_usd": round(pnl200, 2),
            "boot_p10_trailing200": round(boot200["p10"], 2),
            "fullpool_pnl_usd": round(bootfull["actual"], 2),
            "boot_p10_fullpool": round(bootfull["p10"], 2),
        },
        "l1_only": {
            "baseline_sharpe": round(r["base"], 4),
            "candidate_sharpe": round(r["cand"], 4),
            "delta": round(r["delta"], 4),
            "z": round(r["z"], 3),
            "n_base": r["n_base"],
            "n_cand": r["n_cand"],
            "t_day": round(r["t_day"], 2),
            "n_days": r["n_days"],
            "verdict": verdict,
        },
        "dedupe": {"outcomes_dropped": out_dropped, "cfs_dropped": cf_dropped,
                   "ghosts_dropped": len(raw_ghosts) - len(ghosts)},
    }
    print(json.dumps(kn, indent=2))


if __name__ == "__main__":
    main()
