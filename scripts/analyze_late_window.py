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
from datetime import datetime
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

    kill_rule_tripped: any lock_dip loss (every fire is max-tier, so a loss IS a
    breach of the never-breach bound — mechanism failure, one is enough), OR
    trailing-4-day mean DOLLARS < 0 once >= 4 ET days; None before that.
    Maker-ladder rungs are priced for occasional loss (break-even = price paid)
    and only feed the dollars rule.
    Alert-only — the caller never flips config (kill bars are operator authority)."""
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
    # DOLLARS are the unit both legs share. A c/sh rule cannot judge post-close:
    # it buys a $1.00 payout at 0.992, so 0.8c/sh is its ceiling, and a +2c/sh
    # threshold would condemn a leg returning 25%/day.
    usd_daily = [sum(p for _, _, p in v) for _, v in sorted(per_day.items())]
    usd_per_day = statistics.mean(usd_daily)
    trailing4_usd = statistics.mean(usd_daily[-4:]) if len(usd_daily) >= 4 else None
    fills_trailing4 = sum(len(v) for _, v in sorted(per_day.items())[-4:])
    # A lock_dip loss means the max-tier lock named the wrong side — mechanism
    # failure, not variance (it happened once, 08-12 at k=1.1s, and cost the
    # whole stake). One is enough to halt.
    breach_losses = sum(1 for n, w in per_leg.get("lock_dip", []) if w == 0.0)
    if breach_losses:
        tripped = True
    elif len(usd_daily) < 4 or fills_trailing4 < 5:
        # Sparse fills cannot judge a dollars rule: one -$4.50 rung loss after
        # three quiet days reads as trailing-negative on a leg that is up on
        # the week (measured 08-18 on the engine-true series). Persistent
        # bleeding still trips: 5+ fills net-negative over 4 days is a
        # verdict; 1 fill is an anecdote — keep accruing.
        tripped = None
    else:
        tripped = trailing4_usd < 0.0
    legs = {leg: dict(n_fills=len(v),
                      net_per_sh=statistics.mean(n for n, _ in v),
                      win_rate=statistics.mean(w for _, w in v),
                      n_losses=sum(1 for _, w in v if w == 0.0))
            for leg, v in sorted(per_leg.items())}
    return dict(label=f"{db.stem}(trade_history{' since ' + since_iso if since_iso else ''})",
                n_fills=len(fills), n_days=len(daily),
                win_rate=statistics.mean(w for _, w, _ in fills),
                mean_net_day=m, t_day=t, p10=block_bootstrap_p10(daily),
                net_per_sh=statistics.mean(n for n, _, _ in fills),
                net_sum=sum(n for n, _, _ in fills),
                days_pos=sum(1 for d in daily if d > 0), series=series, day_detail=day_detail,
                trailing4_mean=trailing4, trailing8_t=trailing8_t,
                usd_per_day=usd_per_day, usd_p10=block_bootstrap_p10(usd_daily),
                trailing4_usd=trailing4_usd, breach_losses=breach_losses,
                kill_rule_tripped=tripped, legs=legs)


# ── Resolution-mechanism watch ─────────────────────────────────────────────────
def mechanism_read(boundaries: dict, db_path=None):
    """Served resolution values vs OUR recorded stream boundaries, bit-exact.

    This is the check the chain invariant cannot do: final==next-strike stays
    intact when Polymarket swaps the whole resolution source (both served
    values move together — exactly what happened on 08-14 when the 30s stream
    became the 60s stream and the watch stayed green for four days). Here the
    served value must equal the value WE captured from the subscribed topic;
    any systematic gap means the bot is trading a rule it no longer computes.

    boundaries: {window_ts: captured_value} — trusted captures only
    (ChainlinkFeed.boundary_snapshot). Compares each against the label's
    price_to_beat and the previous window's final_price."""
    if not boundaries:
        return None
    db = Path(db_path) if db_path else LIVE_DB
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT window_id, final_price, price_to_beat FROM window_labels "
            "WHERE labeled_at >= ?", (min(boundaries) - 3600,)).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    checked = exact = 0
    worst = 0.0
    worst_ts = None
    for wid, fp, ptb in rows:
        try:
            ts = int(str(wid).rsplit("-", 1)[-1])
        except ValueError:
            continue
        for served, b in ((ptb, ts), (fp, ts + 300)):
            cap = boundaries.get(b)
            if served is None or cap is None:
                continue
            checked += 1
            d = abs(served - cap)
            if d < 0.005:
                exact += 1
            elif d > worst:
                worst, worst_ts = d, b
    if checked == 0:
        return None
    return dict(checked=checked, exact=exact, worst=round(worst, 2),
                worst_ts=worst_ts)


def queue_depth_read(days: float = 7.0, db_path=None):
    """Trailing sweep-consumed depth per deep level — the live check on the
    paper fill rule's AT_PRICE_QUEUE_SH constant (135 sh, book-watch 08-17).

    Estimator: volume printed AT a level immediately before the tape trades
    strictly through it ~= the resting size that was consumed (a LOWER bound;
    the book-watch resting estimate runs higher because size cancels before
    sweeps). The constant is deliberately conservative — the UNSAFE drift is
    real queues growing PAST it (paper would over-credit at-price fills), so
    the alarm tolerance is pooled p75 > the constant."""
    import gzip as _gz
    import json as _json
    import time as _t
    from collections import defaultdict as _dd
    rec_dir = ROOT / "polybot" / "memory" / "recordings"
    db = Path(db_path) if db_path else LIVE_DB
    toks = set()
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for tu, td in con.execute(
                    "SELECT token_up, token_down FROM window_labels "
                    "WHERE labeled_at >= ?", (_t.time() - days * 86400,)):
                if tu:
                    toks.add(tu)
                if td:
                    toks.add(td)
        except sqlite3.OperationalError:
            return None
        finally:
            con.close()
    if not toks:
        return None
    levels = [0.80, 0.65, 0.50, 0.35, 0.20]
    consumed = []
    cur = {}
    cutoff = _t.time() - days * 86400
    for f in sorted(rec_dir.glob("tape_*.jsonl*")):
        day = f.name.split("_")[1][:10]
        try:
            if datetime.fromisoformat(day).timestamp() < cutoff - 86400:
                continue
        except ValueError:
            continue
        opener = (lambda p: _gz.open(p, "rt")) if f.suffix == ".gz" \
            else (lambda p: open(p, encoding="utf-8"))
        try:
            with opener(f) as fh:
                for line in fh:
                    r = _json.loads(line)
                    if r.get("token") not in toks:
                        continue
                    try:
                        ts, px, sz = float(r["ts"]), float(r["price"]), float(r["size"])
                    except (TypeError, ValueError):
                        continue
                    for L in levels:
                        key = (r["token"], L)
                        st = cur.get(key)
                        if st and ts - st[0] > 60.0:
                            del cur[key]
                            st = None
                        if abs(px - L) <= 1e-9:
                            if st is None:
                                cur[key] = [ts, sz]
                            else:
                                st[1] += sz
                        elif px < L - 1e-9 and st is not None:
                            consumed.append(st[1])
                            del cur[key]
        except (OSError, EOFError):
            continue
    if len(consumed) < 50:
        return None
    consumed.sort()
    q = lambda f: round(consumed[min(int(f * len(consumed)), len(consumed) - 1)], 1)
    return dict(n=len(consumed), med=q(0.5), p75=q(0.75), days=days)


def resolution_snapshot_read(db_path=None, hours: float = 26.0):
    """Is the resolution rule still the one the sniper is built on?

    Invariant: a window's official final_price and the NEXT window's
    price_to_beat are the SAME value — the 30s-TWAP stream's report at their
    shared boundary — so they match bit-exact. Systematic divergence means
    Polymarket changed the resolution rule again: kill the sniper
    (trading_enabled: false) and re-verify the mechanism by hand. Checks
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
    gaps = []
    for ts, (fp, ptb, lab) in sorted(by_ts.items()):
        if ts < TWAP_SWITCH_TS:
            continue   # pre-cutover windows chain on the OLD rule — never compare
        if fp is not None and ptb is not None and (lab or 0) >= cutoff:
            gaps.append(abs(fp - ptb))
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
    # Regime readout: |final - strike| distribution over the trailing day.
    # deep_proj's weather. 60s-rule era: gap p50 runs ~$13 market-wide; the
    # photo-finish band is $1 (the same percentile the 30s era's $2 sat at —
    # a 60s average compresses gaps; re-derived 08-18 on 1,186 windows).
    gaps.sort()
    regime = None
    if len(gaps) >= 24:
        q = lambda p: round(gaps[int(p * (len(gaps) - 1))], 2)
        regime = dict(n=len(gaps), gap_p25=q(0.25), gap_p50=q(0.50),
                      gap_p75=q(0.75),
                      photo_finish_pct=round(
                          100.0 * sum(1 for g in gaps if g < 1.0) / len(gaps), 1))
    return dict(checked=checked, matched=matched, worst=round(worst, 2),
                mismatches=mism, regime=regime)


