"""Adversarial verification of p5-sharpe-criterion.

Independently re-derives every headline number:
  - dedupe counts (outcomes / counterfactuals / ghosts)
  - realized $ pnl + day-clustered bootstrap (trailing-200 and full pool)
  - criterion: weighted replay Sharpe on trailing 200 (production iso + identity)
  - L1-only candidate (L2-L6 zeroed) vs baseline on three pools, z + day-t

Replay numbers come from the PRODUCTION AgentScheduler._kelly_bankroll_returns
(called via a stub subclass), NOT from the analysis script under test.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import json
import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
MEM = ROOT / "polybot" / "memory"
ET = ZoneInfo("America/New_York")

from polybot.agents.scheduler import AgentScheduler  # noqa: E402
from polybot.agents.weight_optimizer import _jk_se  # noqa: E402
from polybot.agents.pipeline_analytics import (  # noqa: E402
    RECENCY_DECAY_PER_DAY, sharpe as plain_sharpe, weighted_sharpe_from_returns,
)
from polybot.core.calibrator import IsotonicCalibrator  # noqa: E402
import yaml  # noqa: E402


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


def parse_ts(s) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def exit_ts(o: dict) -> float:
    return parse_ts(o.get("exit_timestamp", o.get("timestamp", "")) or "")


def rec_ts(o: dict) -> float:
    """Best timestamp for dedupe-keep-latest."""
    return max(exit_ts(o), parse_ts(o.get("timestamp", "")))


def et_day(o: dict) -> str:
    t = parse_ts(o.get("timestamp", ""))
    if t == 0.0:
        return (o.get("timestamp") or "")[:10]
    return datetime.fromtimestamp(t, timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def dedupe_by_pid(records: list[dict]) -> tuple[list[dict], int, int]:
    best: dict = {}
    order: list = []
    nopid: list[dict] = []
    for r in records:
        pid = r.get("position_id")
        if pid is None:
            nopid.append(r)
            continue
        if pid not in best:
            best[pid] = r
            order.append(pid)
        elif rec_ts(r) >= rec_ts(best[pid]):
            best[pid] = r
    unique = [best[p] for p in order] + nopid
    return unique, len(records) - len(unique), len(nopid)


outcomes_raw = load_records("outcomes")
ghosts_raw = load_records("ghost_outcomes")
cfs_raw = load_records("counterfactuals")

outcomes, out_dropped, out_nopid = dedupe_by_pid(outcomes_raw)
cfs, cf_dropped, cf_nopid = dedupe_by_pid(cfs_raw)

# ghosts have no position_id; check exact-duplicate records two ways
ghost_json_dupes = len(ghosts_raw) - len({json.dumps(g, sort_keys=True) for g in ghosts_raw})
ghost_key_dupes = len(ghosts_raw) - len(
    {(g.get("timestamp"), g.get("market_id"), g.get("gate_name"), g.get("side")) for g in ghosts_raw}
)

print("=" * 78)
print("DEDUPE / LOAD COUNTS")
print(f"  outcomes:        raw={len(outcomes_raw)}  unique={len(outcomes)}  dropped={out_dropped}  no_pid={out_nopid}")
print(f"  counterfactuals: raw={len(cfs_raw)}  unique={len(cfs)}  dropped={cf_dropped}  no_pid={cf_nopid}")
print(f"  ghosts:          raw={len(ghosts_raw)}  exact_json_dupes={ghost_json_dupes}  key_dupes={ghost_key_dupes}")
print(f"  ghosts resolved: {sum(1 for g in ghosts_raw if g.get('resolved'))}")

# ---------------------------------------------------------------- stub scheduler
class SchedStub(AgentScheduler):
    def __init__(self, config: dict):
        self._config = config


cfg_yaml = yaml.safe_load((ROOT / "polybot" / "config" / "settings.yaml").read_text())
sig = cfg_yaml["signal"]
stub = SchedStub({"execution": {"backtest_realism_factor":
                                cfg_yaml["execution"]["backtest_realism_factor"]}})

BASE_KW = dict(
    recommended_weights=dict(sig["weights"]),
    momentum_weight=float(sig["momentum_weight"]),
    atr_sigma_ratio=float(sig["atr_sigma_ratio"]),
    student_t_df=int(sig["student_t_df"]),
    min_edge=float(sig["min_edge"]),
    kelly_fraction=float(cfg_yaml["math"]["kelly_fraction"]),
    min_kelly=float(sig["min_kelly"]),
    min_prob=float(sig["min_model_probability"]),
    regime_weight=float(sig["regime_weight"]),
    flow_weight=float(sig["flow_weight"]),
    spot_flow_weight=float(sig["spot_flow_weight"]),
    prev_margin_weight=float(sig["prev_margin_weight"]),
    logit_scale=float(sig["logit_scale"]),
    min_atr=float(sig["min_atr"]),
    regime_momentum_threshold=float(sig["regime_momentum_threshold"]),
    final_logit_clamp=float(sig["final_logit_clamp"]),
    l5_regime_damp_cap=float(sig["l5_regime_damp_cap"]),
    atr_regime_shift_threshold=float(sig["atr_regime_shift_threshold"]),
    derived_weights={k: float(v) for k, v in (sig.get("derived") or {}).items()},
)
L1_KW = dict(BASE_KW)
L1_KW.update(
    momentum_weight=0.0, regime_weight=0.0, flow_weight=0.0,
    spot_flow_weight=0.0, prev_margin_weight=0.0,
    derived_weights={k: 0.0 for k in BASE_KW["derived_weights"]},
)

prod_cal = IsotonicCalibrator()
prod_cal.load(MEM / "calibration" / "isotonic_params.json")
ident_cal = IsotonicCalibrator()
print(f"  production calibrator: knots={prod_cal.n_knots} identity={prod_cal.is_identity}")
print(f"  config echo: atr_sigma_ratio={BASE_KW['atr_sigma_ratio']} df={BASE_KW['student_t_df']} "
      f"min_edge={BASE_KW['min_edge']} min_kelly={BASE_KW['min_kelly']} min_prob={BASE_KW['min_prob']} "
      f"momentum_w={BASE_KW['momentum_weight']} kelly_frac={BASE_KW['kelly_fraction']} "
      f"realism={cfg_yaml['execution']['backtest_realism_factor']} derived={BASE_KW['derived_weights']}")

# ---------------------------------------------------------------- pools
real_sorted = sorted(outcomes, key=exit_ts)
t200 = real_sorted[-200:]

ghost_outcomes = []
ghost_skipped = 0
for g in ghosts_raw:
    norm = stub._ghost_to_outcome(g)
    if norm is None:
        ghost_skipped += 1
    else:
        ghost_outcomes.append(norm)
combined = sorted(real_sorted + ghost_outcomes, key=exit_ts)
print(f"  ghosts mapped to outcome-shape: {len(ghost_outcomes)}  skipped: {ghost_skipped}")
print(f"  pools: trailing200={len(t200)}  full_real={len(real_sorted)}  real+ghosts={len(combined)}")

# ---------------------------------------------------------------- realized pnl
def day_bootstrap(records, value=lambda r: float(r.get("pnl") or 0.0)):
    by_day = defaultdict(float)
    for r in records:
        by_day[et_day(r)] += value(r)
    days = sorted(by_day)
    random.seed(42)
    stats = []
    for _ in range(1000):
        pick = random.choices(days, k=len(days))
        stats.append(sum(by_day[d] for d in pick))
    stats.sort()

    def pct(p):
        k = (len(stats) - 1) * p
        f, c = math.floor(k), math.ceil(k)
        if f == c:
            return stats[int(k)]
        return stats[f] * (c - k) + stats[c] * (k - f)

    return statistics.mean(stats), pct(0.10), pct(0.90), days, by_day


def recency_weights(pool, now_ts):
    ws = []
    for o in pool:
        s = o.get("exit_timestamp", o.get("timestamp", ""))
        try:
            t = parse_ts(s) if s else now_ts
            if t == 0.0:
                t = now_ts
            days_ago = max(0.0, (now_ts - t) / 86400.0)
        except Exception:
            days_ago = 0.0
        ws.append(RECENCY_DECAY_PER_DAY ** days_ago)
    return ws


n_pnl_missing = sum(1 for o in real_sorted if o.get("pnl") is None)
pnl200 = sum(float(o.get("pnl") or 0.0) for o in t200)
pnl_all = sum(float(o.get("pnl") or 0.0) for o in real_sorted)
m200, p10_200, p90_200, days200, _ = day_bootstrap(t200)
mall, p10_all, p90_all, days_all, by_day_all = day_bootstrap(real_sorted)

now0 = datetime.now(timezone.utc).timestamp()
g200 = [float(o.get("gain_pct") or 0.0) for o in t200]
w200 = recency_weights(t200, now0)
print("=" * 78)
print("REALIZED P&L (pnl as stored, net of fees)")
print(f"  pnl records missing pnl: {n_pnl_missing}")
print(f"  trailing-200: pnl=${pnl200:,.2f}  n_days={len(days200)} ({days200[0]}..{days200[-1]})")
print(f"    bootstrap(day-clustered, seed42, 1000): mean={m200:,.2f} p10={p10_200:,.2f} p90={p90_200:,.2f}")
print(f"    gain_pct sharpe: weighted={weighted_sharpe_from_returns(g200, w200):+.4f}  "
      f"unweighted={plain_sharpe(g200):+.4f}")
print(f"  full pool ({len(real_sorted)}): pnl=${pnl_all:,.2f}  n_days={len(days_all)}")
print(f"    bootstrap: mean={mall:,.2f} p10={p10_all:,.2f} p90={p90_all:,.2f}")
print(f"    per-day pnl: " + "  ".join(f"{d[5:]}:{by_day_all[d]:+.0f}" for d in days_all))

# ---------------------------------------------------------------- replay machinery
def replay(pool, kwargs, cal):
    now_ts = datetime.now(timezone.utc).timestamp()
    rets, ws = stub._kelly_bankroll_returns(outcomes=pool, calibrator=cal, **kwargs)
    # recover entered-record day labels by matching recency weights in order
    myw = recency_weights(pool, now_ts)
    days, j = [], 0
    for w in ws:
        while j < len(pool):
            if abs(myw[j] - w) <= 1e-7 * max(w, 1e-12):
                days.append(et_day(pool[j]))
                j += 1
                break
            j += 1
        else:
            raise RuntimeError("recency-weight match failed — day labels unrecoverable")
    return rets, ws, days


def day_t(pool, b, c):
    """Day-clustered t on per-day (candidate - baseline) summed replay returns."""
    all_days = sorted({et_day(o) for o in pool})
    bs = {d: 0.0 for d in all_days}
    cs = {d: 0.0 for d in all_days}
    for r, d in zip(b[0], b[2]):
        bs[d] += r
    for r, d in zip(c[0], c[2]):
        cs[d] += r
    diffs = [cs[d] - bs[d] for d in all_days]
    m, sd = statistics.mean(diffs), statistics.pstdev(diffs)
    t = m / (sd / math.sqrt(len(diffs))) if sd > 0 else float("nan")
    # sensitivity: only days where either arm entered
    nz = [x for x, d in zip(diffs, all_days) if bs[d] != 0.0 or cs[d] != 0.0]
    if len(nz) >= 2:
        m2, sd2 = statistics.mean(nz), statistics.pstdev(nz)
        t2 = m2 / (sd2 / math.sqrt(len(nz))) if sd2 > 0 else float("nan")
    else:
        t2, nz = float("nan"), nz
    return t, len(all_days), t2, len(nz)


def arm_stats(rw):
    rets, ws, _ = rw
    return weighted_sharpe_from_returns(rets, ws), plain_sharpe(rets), len(rets), sum(rets)


pools = {"i_trailing200": t200, "ii_full_real": real_sorted, "iii_real+ghosts": combined}

print("=" * 78)
print("CRITERION — baseline-config replay on trailing 200 (production iso then identity)")
for cal, label in ((prod_cal, "production"), (ident_cal, "identity")):
    rw = replay(t200, BASE_KW, cal)
    s_w, s_u, n, tot = arm_stats(rw)
    print(f"  [{label:>10}] entered={n}/{len(t200)}  sharpe_w={s_w:+.4f}  sharpe_unw={s_u:+.4f}  sum_ret={tot:+.4f}")

print("=" * 78)
print("L1-ONLY CANDIDATE (L2-L6 zeroed) vs BASELINE")
hdr = f"  {'pool':<16}{'cal':<11}{'base_sh':>9}{'cand_sh':>9}{'delta':>9}{'z':>8}{'n_b':>6}{'n_c':>6}{'t_day':>8}{'nd':>4}{'t_nz':>8}{'nz':>4}"
print(hdr)
for pname, pool in pools.items():
    for cal, clabel in ((prod_cal, "prod"), (ident_cal, "identity")):
        b = replay(pool, BASE_KW, cal)
        c = replay(pool, L1_KW, cal)
        bs_w, _, nb, _ = arm_stats(b)
        cs_w, _, nc, _ = arm_stats(c)
        delta = cs_w - bs_w
        se = _jk_se(bs_w, nc, c[0])
        z = delta / se if se > 0 else float("nan")
        t, nd, t2, nnz = day_t(pool, b, c)
        print(f"  {pname:<16}{clabel:<11}{bs_w:>+9.4f}{cs_w:>+9.4f}{delta:>+9.4f}{z:>+8.3f}{nb:>6}{nc:>6}{t:>+8.2f}{nd:>4}{t2:>+8.2f}{nnz:>4}")

print("=" * 78)
print("DIAGNOSIS — reproduce claimed numbers with the ANALYSIS SCRIPT'S baseline")
print("(their make_replay omits derived_weights -> registry default 0.0, dropping")
print(" the LIVE settings.yaml L6 flow_disagreement=0.005 from the baseline arm)")
THEIR_BASE = dict(BASE_KW)
THEIR_BASE["derived_weights"] = {k: 0.0 for k in BASE_KW["derived_weights"]}
for cal, label in ((prod_cal, "production"), (ident_cal, "identity")):
    rw = replay(t200, THEIR_BASE, cal)
    s_w, s_u, n, tot = arm_stats(rw)
    print(f"  CRITERION [{label:>10}] entered={n}/200  sharpe_w={s_w:+.4f}  sharpe_unw={s_u:+.4f}")
print(hdr)
for pname, pool in pools.items():
    for cal, clabel in ((prod_cal, "prod"), (ident_cal, "identity")):
        b = replay(pool, THEIR_BASE, cal)
        c = replay(pool, L1_KW, cal)
        bs_w, _, nb, _ = arm_stats(b)
        cs_w, _, nc, _ = arm_stats(c)
        delta = cs_w - bs_w
        se = _jk_se(bs_w, nc, c[0])
        z = delta / se if se > 0 else float("nan")
        t, nd, t2, nnz = day_t(pool, b, c)
        print(f"  {pname:<16}{clabel:<11}{bs_w:>+9.4f}{cs_w:>+9.4f}{delta:>+9.4f}{z:>+8.3f}{nb:>6}{nc:>6}{t:>+8.2f}{nd:>4}{t2:>+8.2f}{nnz:>4}")

print("=" * 78)
print("BOOTSTRAP RNG-STREAM SENSITIVITY (their convention: random.choice loop +")
print(" index percentile stats[int(p*N)]; re-seeded 42 per bootstrap call)")


def day_boot_their_way(records):
    by_day = defaultdict(float)
    for r in records:
        if r.get("pnl") is not None:
            by_day[et_day(r)] += r["pnl"]
    days = sorted(by_day)
    random.seed(42)
    stats = []
    for _ in range(1000):
        sampled = [random.choice(days) for _ in days]
        stats.append(sum(by_day[d] for d in sampled))
    stats.sort()
    return statistics.mean(stats), stats[int(0.10 * 1000)], stats[int(0.90 * 1000)]


for label, recs in (("trailing-200", t200), ("full pool", real_sorted)):
    m, p10, p90 = day_boot_their_way(recs)
    print(f"  {label:<14} mean={m:,.2f}  p10={p10:,.2f}  p90={p90:,.2f}")
