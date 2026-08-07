"""Realized-ledger readers for the nightly sniper health job (main._sniper_health_job).

Everything here reads trade_history/window_labels — signal-agnostic, so the
reads carried unchanged across the 08-07 TWAP cutover. The SIM-side replay
lives in scripts/analyze_twap_lock.py; the BINDING deployment gate is
live_health_read over the paper-shadow fills since late_window.validation_epoch
(sniper_shadow_status.py prints it).
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import sys

ROOT = Path(__file__).resolve().parent.parent
# Standalone runs from any cwd still need polybot.* importable (sprt/paths).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LIVE_DB = ROOT / "polybot" / "db" / "polybot_live.db"   # real fills for the live kill-rule read
PAPER_DB = ROOT / "polybot" / "db" / "polybot_paper.db" # paper-shadow fills (the binding gate in paper mode)
ET = ZoneInfo("America/New_York")  # DST-correct; a fixed UTC-4 mis-buckets EST days
TWAP_SWITCH_TS = 1786060800        # 2026-08-07 00:00 UTC — the resolution-rule cutover;
                                   # the straddle pair (old-rule final vs new-rule strike)
                                   # differs by $10.61 BY DESIGN and must never alarm


def et_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")


def tstat(xs: list[float]):
    n = len(xs)
    if n < 2:
        return (statistics.mean(xs) if xs else float("nan"), float("nan"), n)
    m = statistics.mean(xs)
    sd = statistics.stdev(xs)
    se = sd / math.sqrt(n)
    return (m, (m / se if se > 0 else float("nan")), n)


def block_bootstrap_p10(daily: list[float], iters: int = 2000) -> float:
    """Resample whole days with replacement; p10 of the resampled day-means.
    Seeded stdlib RNG on purpose: a raw LCG's low bits cycle with period 8, so
    at n_days=8 p10 degenerated to exactly the mean — the leg checked nothing."""
    if len(daily) < 2:
        return float("nan")
    n = len(daily)
    rng = random.Random(12345)
    means = []
    for _ in range(iters):
        means.append(sum(daily[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[int(0.10 * len(means))]


def live_health_read(db_path=None, since_iso=None):
    """Post-live kill-rule metrics from REALIZED fills (trade_history).

    Defaults to polybot_live.db; pass db_path=PAPER_DB + since_iso=<validation
    epoch> for the BINDING paper-shadow gate (pre-epoch fills ran different
    code/config and are excluded). Unit matches the kill bar so the reads
    compare directly: EQUAL-WEIGHT per-fill net $/share, ET-day-clustered.

    Per-fill net = pnl / shares_held, and pnl is ALREADY net of all fees
    (size = shares*entry + entry_fee, pnl = revenue - size; scalp exits net
    the exit fee into revenue) — subtracting the stored `fees` column again
    DOUBLE-COUNTS the fee, ~1.3c/sh too pessimistic. shares_held is the
    audited fill count. Every trade_history row is a sniper fire (base
    entries are always suppressed).

    kill_rule_tripped: trailing-4-day mean < +0.02 once >= 4 ET days, OR
    trailing-8-day t < 2.0 once >= 8; None before 4 days. Alert-only — the
    caller never flips config (kill bars are operator authority)."""
    db = Path(db_path) if db_path else LIVE_DB
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        # Join on position_id — a bare t.id = p.id pairing only holds while both
        # AUTOINCREMENT sequences run in lockstep. id is the un-migrated fallback.
        has_pid = any(r[1] == "position_id"
                      for r in con.execute("PRAGMA table_info(trade_history)"))
        join_key = "COALESCE(t.position_id, t.id)" if has_pid else "t.id"
        q = ("SELECT t.pnl AS pnl, t.exit_timestamp AS ts, "
             "p.shares_held AS shares, p.indicator_snapshot AS snap "
             "FROM trade_history t "
             f"JOIN positions p ON {join_key} = p.id "
             "WHERE t.exit_timestamp IS NOT NULL AND p.shares_held > 0")
        args = ()
        if since_iso:
            q += " AND t.exit_timestamp >= ?"
            args = (since_iso,)
        rows = con.execute(q, args).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    per_day = defaultdict(list)     # ET day -> list of (net_per_sh, win, pnl$)
    per_leg = defaultdict(list)     # signal_leg -> list of (net_per_sh, win)
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue
        nps = r["pnl"] / r["shares"]        # pnl already nets all fees — never subtract `fees` again
        per_day[et_day(ts)].append((nps, 1.0 if (r["pnl"] or 0) > 0 else 0.0, r["pnl"] or 0.0))
        try:
            leg = (json.loads(r["snap"] or "{}").get("trade_context", {})
                   or {}).get("signal_leg") or "unstamped"
        except (ValueError, json.JSONDecodeError):
            leg = "unstamped"
        per_leg[leg].append((nps, 1.0 if (r["pnl"] or 0) > 0 else 0.0))
    if not per_day:
        return None
    fills = [x for v in per_day.values() for x in v]
    series = [(day, statistics.mean(n for n, _, _ in v)) for day, v in sorted(per_day.items())]
    # per-day rollup for the manual shadow table — one source of truth for both reads
    day_detail = [(day, len(v), statistics.mean(w for _, w, _ in v),
                   statistics.mean(n for n, _, _ in v), sum(p for _, _, p in v))
                  for day, v in sorted(per_day.items())]
    daily = [m for _, m in series]
    m, t, _ = tstat(daily)
    trailing4 = statistics.mean(daily[-4:]) if len(daily) >= 4 else None
    trailing8_t = tstat(daily[-8:])[1] if len(daily) >= 8 else None
    if len(daily) < 4:
        tripped = None                                        # too few live days to judge
    else:
        tripped = (trailing4 < 0.02) or (trailing8_t is not None and trailing8_t < 2.0)
    legs = {leg: dict(n_fills=len(v),
                      net_per_sh=statistics.mean(n for n, _ in v),
                      win_rate=statistics.mean(w for _, w in v))
            for leg, v in sorted(per_leg.items())}
    return dict(label=f"{db.stem}(trade_history{' since ' + since_iso if since_iso else ''})",
                n_fills=len(fills), n_days=len(daily),
                win_rate=statistics.mean(w for _, w, _ in fills), avg_fill=float("nan"),
                mean_net_day=m, t_day=t, p10=block_bootstrap_p10(daily),
                net_per_sh=statistics.mean(n for n, _, _ in fills),
                net_sum=sum(n for n, _, _ in fills),
                days_pos=sum(1 for d in daily if d > 0), series=series, day_detail=day_detail,
                trailing4_mean=trailing4, trailing8_t=trailing8_t,
                kill_rule_tripped=tripped, legs=legs)


def _realized_fill_contexts(db_path, since_iso):
    """(et_day, net_per_share_$, pnl_$, size_$, trade_context) per realized
    fill — shared loader for the SPRT / regime-shadow reads. Same join + net
    convention as live_health_read (pnl already net of all fees)."""
    db = Path(db_path) if db_path else LIVE_DB
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        has_pid = any(r[1] == "position_id"
                      for r in con.execute("PRAGMA table_info(trade_history)"))
        join_key = "COALESCE(t.position_id, t.id)" if has_pid else "t.id"
        q = ("SELECT t.pnl AS pnl, t.size AS size, t.exit_timestamp AS ts, "
             "p.shares_held AS shares, p.indicator_snapshot AS snap "
             "FROM trade_history t "
             f"JOIN positions p ON {join_key} = p.id "
             "WHERE t.exit_timestamp IS NOT NULL AND p.shares_held > 0")
        args = ()
        if since_iso:
            q += " AND t.exit_timestamp >= ?"
            args = (since_iso,)
        rows = con.execute(q, args).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
            ctx = json.loads(r["snap"] or "{}").get("trade_context", {}) or {}
        except (ValueError, AttributeError, json.JSONDecodeError):
            continue
        out.append((et_day(ts), r["pnl"] / r["shares"], r["pnl"] or 0.0,
                    r["size"] or 0.0, ctx))
    return out


# ── Burst-alive SPRT (pre-registered; constants are design-frozen) ────────────
# Tests whether tick-rate-burst days out-earn calm days on realized fills.
# HOT ⇔ n_ticks_1s / (n_ticks_30s/30) ≥ 2.0 at the fill's decision tick; unit =
# per-ET-day (mean HOT net − mean COLD net) in ¢/sh, days with ≥ 2 fills on
# EACH arm. H1 μ₁ = +6¢/sh. σ freezes WRITE-ONCE from the first 6 qualifying
# days (memory/state/sprt_burst.json); those days never score — estimating and
# scoring on the same days would bias the test. Deleting the state file
# restarts it.
BURST_SPRT_MU1 = 6.0
BURST_SPRT_ALPHA = 0.05
BURST_SPRT_BETA = 0.23
BURST_SPRT_SIGMA_DAYS = 6
BURST_MIN_ARM_FILLS = 2
BURST_HOT_RATIO = 2.0


def _burst_arm(ctx: dict):
    """'HOT' / 'COLD' from the stamped tick counters; None when the feed was
    cold at fire time — the fill scores on neither arm."""
    n1, n30 = ctx.get("n_ticks_1s"), ctx.get("n_ticks_30s")
    if n1 is None or n30 is None or not n30:
        return None
    return "HOT" if (n1 / (n30 / 30.0)) >= BURST_HOT_RATIO else "COLD"


def burst_sprt_read(db_path=None, since_iso=None, state_path=None):
    """Nightly burst-alive SPRT state from the realized ledger. Alert-only —
    never touches sizing or entries: accept-H1 graduates burst into the
    regime-Kelly framework, accept-H0 parks it."""
    from polybot.core.sprt import run_sprt
    from polybot.paths import SPRT_BURST_PATH
    sp = Path(state_path) if state_path else SPRT_BURST_PATH
    per_day = defaultdict(lambda: {"HOT": [], "COLD": []})
    for day, nps, _pnl, _size, ctx in _realized_fill_contexts(db_path, since_iso):
        arm = _burst_arm(ctx)
        if arm is not None:
            per_day[day][arm].append(nps * 100.0)          # ¢/sh
    qualifying = [
        (day, statistics.mean(v["HOT"]) - statistics.mean(v["COLD"]))
        for day, v in sorted(per_day.items())
        if len(v["HOT"]) >= BURST_MIN_ARM_FILLS and len(v["COLD"]) >= BURST_MIN_ARM_FILLS
    ]
    state = None
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except (json.JSONDecodeError, OSError):
            state = None
    if state is None:
        if len(qualifying) < BURST_SPRT_SIGMA_DAYS:
            return dict(state="accruing_sigma", n_qualifying=len(qualifying),
                        need=BURST_SPRT_SIGMA_DAYS, frozen_sigma=None,
                        lam=None, n_scored=0, day_diffs=[d for _, d in qualifying])
        est = qualifying[:BURST_SPRT_SIGMA_DAYS]
        sigma = statistics.stdev([d for _, d in est])
        state = {"frozen_sigma": round(sigma, 4),
                 "sigma_days": [day for day, _ in est],
                 "mu1": BURST_SPRT_MU1,
                 "frozen_at": datetime.now(timezone.utc).isoformat()}
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(state, indent=2))      # write-once freeze
        except OSError:
            pass
    sigma_days = set(state.get("sigma_days", []))
    scored = [(day, d) for day, d in qualifying if day not in sigma_days]
    r = run_sprt([d for _, d in scored], state.get("mu1", BURST_SPRT_MU1),
                 float(state.get("frozen_sigma") or 0.0),
                 BURST_SPRT_ALPHA, BURST_SPRT_BETA)
    return dict(state=r.state, lam=r.lam, upper=r.upper, lower=r.lower,
                n_qualifying=len(qualifying), n_scored=r.n_days,
                frozen_sigma=state.get("frozen_sigma"),
                day_diffs=[d for _, d in scored])


# ── Regime-Kelly shadow counterfactual ─────────────────────────────────────────
def regime_shadow_read(db_path=None, since_iso=None):
    """Per-ET-day counterfactual D = regime-sized $P&L − flat $P&L over fills
    carrying the regime shadow stamps (size_flat/size_regime). Report-only:
    the D-level SPRT may not START until the burst SPRT accepts H1.
    size_regime == 0 = the regime arm skipped the fill (sub-$1) — earns nothing."""
    per_day = defaultdict(lambda: [0.0, 0])                 # day -> [D$, n_stamped]
    for day, _nps, pnl, _size, ctx in _realized_fill_contexts(db_path, since_iso):
        sf, sr = ctx.get("size_flat"), ctx.get("size_regime")
        if sf is None or sr is None or not sf:
            continue
        per_day[day][0] += (pnl / sf) * sr - pnl
        per_day[day][1] += 1
    scored = [(day, v[0], v[1]) for day, v in sorted(per_day.items()) if v[1] >= 3]
    return dict(n_days=len(scored),
                total_d=sum(d for _, d, _ in scored),
                day_detail=scored)


# ── Resolution-mechanism watch ─────────────────────────────────────────────────
def resolution_snapshot_read(db_path=None, hours: float = 26.0):
    """Is the resolution rule still the one the sniper is built on?

    Invariant: a window's official final_price and the NEXT window's
    price_to_beat are the SAME value — the 30s-TWAP stream's report at their
    shared boundary — so they match bit-exact. Systematic divergence means
    Polymarket changed the resolution rule again: kill the sniper
    (sniper_enabled: false) and re-verify the mechanism by hand. Checks
    windows labeled in the trailing ``hours``; alert-only.
    """
    import time as _t
    db = Path(db_path) if db_path else LIVE_DB
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT window_id, final_price, price_to_beat, labeled_at "
            "FROM window_labels WHERE labeled_at >= ?",
            (_t.time() - (hours + 1.0) * 3600.0,)).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    cutoff = _t.time() - hours * 3600.0
    by_ts: dict[int, tuple] = {}
    for wid, fp, ptb, lab in rows:
        try:
            ts = int(str(wid).rsplit("-", 1)[-1])
        except ValueError:
            continue
        by_ts[ts] = (fp, ptb, lab)
    checked = matched = 0
    worst = 0.0
    mism = []
    for ts, (fp, _ptb, lab) in sorted(by_ts.items()):
        if ts < TWAP_SWITCH_TS:
            continue   # pre-cutover windows chain on the OLD rule — never compare
        nxt = by_ts.get(ts + 300)
        if nxt is None or fp is None or nxt[1] is None or (lab or 0) < cutoff:
            continue
        checked += 1
        if abs(fp - nxt[1]) < 0.005:
            matched += 1
        else:
            d = abs(fp - nxt[1])
            worst = max(worst, d)
            if len(mism) < 3:
                mism.append(dict(window_ts=ts, final=fp, next_ptb=nxt[1],
                                 diff=round(d, 2)))
    return dict(checked=checked, matched=matched, worst=round(worst, 2),
                mismatches=mism)


# ── Scar scan (nightly learning loop — polybot/core/scar_scan.py) ─────────────
def scar_scan_read(db_path=None, since_iso=None, enforce=None,
                   registry_path=None, vetoes_path=None, mode=None):
    """Scar scan: discovery + per-gate OOS SPRT over the current mode's
    realized ledger, plus enforced-veto resolution. Alert-only; registry
    persists to memory/state/scar_gates.json. `mode` stamps registrations and
    pauses foreign-mode gates on a mode flip — never splice modes into one
    frozen-σ test."""
    from polybot.core.scar_scan import scan, resolve_vetoes
    from polybot.paths import SCAR_GATES_PATH, SCAR_VETOES_PATH
    db = Path(db_path) if db_path else LIVE_DB
    reg = Path(registry_path) if registry_path else SCAR_GATES_PATH
    vet = Path(vetoes_path) if vetoes_path else SCAR_VETOES_PATH
    rep = scan(db, since_iso, reg, enforce or [], mode)
    rep["vetoes"] = resolve_vetoes(vet, db)
    return rep
