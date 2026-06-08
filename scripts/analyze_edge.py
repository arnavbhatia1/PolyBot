#!/usr/bin/env python
"""KEEP/KILL — is there a real edge in 5-min BTC Up/Down at the true fee?

Reads realized outcomes (restamp-aware: pnl/fees are net of `fee_restamped`, 0.07
for restamped records) and the counterfactual tracker, and answers three questions
the freeze was set up to settle:

  1. MODEL EDGE (unconfounded by scalp timing): held to resolution, does the chosen
     side win often enough to beat the price we paid, net of fee? Uses `correct` +
     `entry_price` — the realized exit (scalp vs hold) does not enter, so the
     bot's endogenous "hold winners / scalp losers" selection can't flatter it.
  2. SCALP LEAK: across the counterfactual tracker, did the bot's actual exits beat
     their alternative? Split by what the bot actually did (scalp vs hold).
  3. WHERE: which session / vol / prob / side slices carry the edge, day-clustered.

Significance is DAY-CLUSTERED (trades within a day are correlated, so N days ~= N
independent obs, not N trades). A slice is only worth forward-confirming if its
day-clustered t >= 2 over >= 3 days (flagged <<<). With a thin day base, read
everything as directional.

Usage:
  python analyze_edge.py                 # fee 0.07
  python analyze_edge.py --fee 0.072     # sensitivity to a higher coefficient
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polybot.paths import OUTCOMES_DIR, COUNTERFACTUALS_DIR
from polybot.execution.base import DEFAULT_FEE_RATE

ET = ZoneInfo("America/New_York")
RECORDED_RATE = 0.018
MIN_SLICE_N = 60
MIN_DAY_COUNT = 3


def load(d_dir):
    recs = []
    for fp in sorted(glob.glob(str(d_dir / "*.json"))):
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        for r in (d if isinstance(d, list) else [d]):
            if isinstance(r, dict):
                recs.append(r)
    return recs


def realized_gain(r, rate):
    """Realized gain_pct re-costed to `rate`, restamp-aware."""
    base = r.get("fee_restamped") or DEFAULT_FEE_RATE   # unflagged records are native at the live fee, not 0.018
    fee = r.get("fees") or 0.0
    extra = fee * (rate / base - 1.0)
    return (r["pnl"] - extra) / r["size"]


def hold_gain(r, rate):
    """Counterfactual gain_pct if the entry had been HELD to resolution — pure
    binary payoff at the entry price, entry fee only (resolution isn't a taker
    trade). Independent of the bot's actual exit. fee/size = rate*(1-p)."""
    p = r.get("entry_price")
    if not p or p <= 0 or p >= 1:
        return None
    ef = rate * (1.0 - p)
    if r.get("correct"):
        return (1.0 - p) / p - ef
    return -1.0 - ef


def tc(r):
    return (r.get("indicator_snapshot") or {}).get("trade_context") or {}


def dt_of(r):
    try:
        d = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def day_clustered(vals_by_day):
    """vals_by_day: {day: [gain,...]}. Returns (pt_mean, day_mean, day_se, n_days)."""
    day_means = [st.mean(v) for v in vals_by_day.values() if v]
    allv = [g for v in vals_by_day.values() for g in v]
    pt = st.mean(allv) if allv else 0.0
    if len(day_means) >= 2:
        dm = st.mean(day_means)
        se = st.pstdev(day_means) / (len(day_means) ** 0.5)
    else:
        dm, se = pt, float("nan")
    return pt, dm, se, len(vals_by_day)


def slice_table(title, recs, keyfn, gainfn, rate):
    buckets = defaultdict(lambda: defaultdict(list))   # key -> day -> [gain]
    wins = defaultdict(lambda: [0, 0])                 # key -> [wins, n]
    for r in recs:
        k = keyfn(r)
        g = gainfn(r, rate)
        d = dt_of(r)
        if k is None or g is None or d is None:
            continue
        day = d.astimezone(ET).strftime("%Y-%m-%d")
        buckets[k][day].append(g)
        wins[k][1] += 1
        if g > 0:
            wins[k][0] += 1
    print(f"\n=== {title} ===")
    print(f"  {'slice':<20}{'n':>6}{'pos%':>7}{'mean/trade':>12}{'day-mean':>11}{'day-SE':>9}{'t_day':>7}")
    for k in sorted(buckets, key=lambda x: str(x)):
        n = wins[k][1]
        if n < MIN_SLICE_N:
            continue
        pt, dm, se, nd = day_clustered(buckets[k])
        t = (dm / se) if (se and se == se and se > 0) else float("nan")
        wr = 100 * wins[k][0] / n
        good = dm > 0 and t == t and t >= 2.0 and nd >= MIN_DAY_COUNT
        flag = "  <<<" if good else ("   ~+" if (dm > 0 and nd >= MIN_DAY_COUNT) else "")
        print(f"  {str(k):<20}{n:>6}{wr:>6.1f}%{pt:>+12.4f}{dm:>+11.4f}{se:>9.4f}{t:>7.2f}{flag}")


def overall(label, recs, gainfn, rate):
    by_day = defaultdict(list)
    for r in recs:
        g = gainfn(r, rate)
        d = dt_of(r)
        if g is None or d is None:
            continue
        by_day[d.astimezone(ET).strftime("%Y-%m-%d")].append(g)
    pt, dm, se, nd = day_clustered(by_day)
    t = (dm / se) if (se and se == se and se > 0) else float("nan")
    lo = dm - 2 * se if se == se else float("nan")
    print(f"  {label:<26} mean/trade {pt:+.4f} | day-mean {dm:+.4f} +/- {se:.4f} "
          f"(t_day={t:+.2f}, ~95% lo={lo:+.4f}, {nd} days)")


# ---- slice key functions ----
def b_session_utc(r):
    d = dt_of(r)
    if not d:
        return None
    h = d.astimezone(timezone.utc).hour
    if h < 8:
        return "1:dead 00-08U"
    if h < 13:
        return "2:london 08-13U"
    if h < 21:
        return "3:prime 13-21U"
    return "4:usclose 21-24U"


def b_prob(r):
    p = tc(r).get("model_probability") or tc(r).get("model_probability_raw")
    if p is None:
        return None
    return "p>=0.70" if p >= 0.70 else ("p0.62-0.70" if p >= 0.62 else "p0.56-0.62")


def b_vol(r):
    a = tc(r).get("atr_rolling_20")
    if not a:
        return None
    return "atr>=90" if a >= 90 else ("atr60-90" if a >= 60 else "atr<60")


def b_edge(r):
    e = tc(r).get("edge")
    if e is None:
        return None
    return "edge>=0.10" if e >= 0.10 else ("edge0.06-0.10" if e >= 0.06 else "edge<0.06")


def b_ac(r):
    a = tc(r).get("regime_autocorr")
    if a is None:
        return None
    a = abs(a)
    return "|ac|>=0.20" if a >= 0.20 else ("|ac|0.08-0.20" if a >= 0.08 else "|ac|<0.08")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee", type=float, default=0.07)
    args = ap.parse_args()
    rate = args.fee

    out = [r for r in load(OUTCOMES_DIR) if "pnl" in r and r.get("size")]
    days = sorted({dt_of(r).astimezone(ET).strftime("%Y-%m-%d") for r in out if dt_of(r)})
    stamped = sum(1 for r in out if r.get("fee_restamped") == 0.07)
    print(f"loaded {len(out)} realized outcomes over {len(days)} ET days "
          f"({days[0]} .. {days[-1]}), fee={rate}, restamped={stamped}/{len(out)}")

    print("\n##### Q0. DOLLAR P&L — what the bankroll actually did (size-weighted truth) #####")
    dollars = defaultdict(float)
    for r in out:
        g = realized_gain(r, rate)
        d = dt_of(r)
        if g is None or d is None:
            continue
        dollars[d.astimezone(ET).strftime("%Y-%m-%d")] += g * r["size"]
    daily = [dollars[k] for k in sorted(dollars)]
    tot = sum(daily)
    md = st.mean(daily) if daily else 0.0
    se = st.pstdev(daily) / (len(daily) ** 0.5) if len(daily) >= 2 else float("nan")
    tdd = md / se if se == se and se > 0 else float("nan")
    best = max(daily) if daily else 0.0
    print(f"  total ${tot:+.2f} over {len(out)} trades | ${md:+.2f}/day +/- {se:.2f} (t_day={tdd:+.2f}, {len(daily)} days)")
    if tot:
        print(f"  best single day ${best:+.2f} = {100*best/tot:.0f}% of total (variance/concentration check)")

    print("\n##### Q1. MODEL EDGE — held to resolution (unconfounded by scalp timing) #####")
    overall("REALIZED (as-traded):", out, realized_gain, rate)
    overall("HOLD-to-resolution (cf):", out, hold_gain, rate)
    hit = 100 * st.mean([1 if r.get("correct") else 0 for r in out])
    avg_p = st.mean([r["entry_price"] for r in out if r.get("entry_price")])
    print(f"  directional hit rate (correct, hold) {hit:.1f}%  |  avg entry price {avg_p:.3f}  "
          f"|  break-even hit ~{100*avg_p:.1f}%")

    print("\n  -- Calibration / favorite-longshot: does hit rate track the price paid? --")
    print(f"  {'entry price':<16}{'n':>6}{'hit%':>8}{'impliedP%':>11}{'hold mean/trade':>17}")
    pb = defaultdict(list)
    for r in out:
        p = r.get("entry_price")
        if not p:
            continue
        k = "0.45-0.55" if p < 0.55 else ("0.55-0.65" if p < 0.65 else ("0.65-0.75" if p < 0.75 else "0.75+"))
        pb[k].append(r)
    for k in sorted(pb):
        rs = pb[k]
        h = 100 * st.mean([1 if r.get("correct") else 0 for r in rs])
        ip = 100 * st.mean([r["entry_price"] for r in rs])
        hg = st.mean([g for g in (hold_gain(r, rate) for r in rs) if g is not None])
        print(f"  {k:<16}{len(rs):>6}{h:>7.1f}%{ip:>10.1f}%{hg:>+17.4f}")

    print("\n##### Q2. SCALP LEAK — did the bot's actual exits beat the alternative? #####")
    cf = load(COUNTERFACTUALS_DIR)
    by_actual = defaultdict(lambda: {"b": [], "opt": 0, "n": 0})
    for c in cf:
        a = c.get("actual") or {}
        cc = c.get("counterfactual") or {}
        er = a.get("exit_reason")
        if er is None or a.get("pnl") is None or cc.get("pnl") is None:
            continue
        benefit = a["pnl"] - cc["pnl"]   # >0 => the bot's actual exit beat its tracked alternative
        b = by_actual[er]
        b["b"].append(benefit)
        b["n"] += 1
        if c.get("hold_was_optimal") or c.get("scalp_was_optimal"):
            b["opt"] += 1
    print(f"  {'bot actually did':<22}{'n':>6}{'sum benefit$':>14}{'mean benefit$':>15}{'choice_optimal%':>16}")
    for er in sorted(by_actual):
        b = by_actual[er]
        print(f"  {er:<22}{b['n']:>6}{sum(b['b']):>+14.2f}{st.mean(b['b']):>+15.4f}"
              f"{100*b['opt']/b['n']:>15.1f}%")
    print("  (benefit$ = actual.pnl - counterfactual.pnl; >0 means the bot's exit beat its")
    print("   alternative. For scalp, the alternative is hold-to-resolution.)")

    print("\n##### Q3. WHERE is the model edge (HOLD-to-resolution, day-clustered) #####")
    slice_table("Session (UTC)", out, b_session_utc, hold_gain, rate)
    slice_table("Volatility (ATR20)", out, b_vol, hold_gain, rate)
    slice_table("Model probability", out, b_prob, hold_gain, rate)
    slice_table("Side", out, lambda r: r.get("side"), hold_gain, rate)
    slice_table("Edge bucket", out, b_edge, hold_gain, rate)
    slice_table("Regime |autocorr|", out, b_ac, hold_gain, rate)
    slice_table("Weekday vs weekend", out,
                lambda r: ("weekend" if dt_of(r) and dt_of(r).astimezone(ET).weekday() >= 5
                           else "weekday") if dt_of(r) else None, hold_gain, rate)

    print("\n##### Q3b. Same slices, REALIZED as-traded (edge AFTER the scalp policy) #####")
    slice_table("Session (UTC) realized", out, b_session_utc, realized_gain, rate)
    slice_table("Volatility realized", out, b_vol, realized_gain, rate)
    slice_table("Model prob realized", out, b_prob, realized_gain, rate)

    print("\nLEGEND: <<< day-clustered t>=2 over >=3 days (forward-confirm candidate).")
    print("        ~+ positive day-mean but not yet significant (directional).")


if __name__ == "__main__":
    main()
