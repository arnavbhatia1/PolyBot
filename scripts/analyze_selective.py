#!/usr/bin/env python
"""H8 — selective participation: is there ANY sub-slice of 5-min BTC that is
profitable net of the REAL Polymarket fee?

We can't forecast direction (the model loses the horse race), but maybe some
*conditions* (time of day, regime, phase, ...) clear the fee hurdle while others
bleed. If so, the cheapest edge is "only trade the good slice."

Every realized trade is re-costed at the true taker coefficient (default 0.07 —
recorded `fees` used 0.018, so we scale: net = pnl - fees*(rate/0.018 - 1)).

Significance is reported BOTH ways and the DAY-CLUSTERED one is the honest one:
trades within a day/window are correlated, so 1,800 trades over ~7 days is ~7
independent observations, not 1,800. A slice is only interesting if it survives
day-clustering AND a first-half/second-half stability split. With ~40 slices
tested, expect a few false positives by chance — treat a lone positive slice as a
hypothesis to confirm forward, never as a found edge.

Usage:
  python analyze_selective.py                # all outcomes, fee 0.07
  python analyze_selective.py --fee 0.018    # compare at the old (wrong) fee
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
from polybot.paths import OUTCOMES_DIR

ET = ZoneInfo("America/New_York")
RECORDED_RATE = 0.018          # the coefficient the stored `fees` were computed with
MIN_SLICE_N = 80               # ignore slices thinner than this
MIN_DAY_COUNT = 3              # need >= this many distinct days for a clustered read


def load():
    recs = []
    for fp in sorted(glob.glob(str(OUTCOMES_DIR / "*.json"))):
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        for r in (d if isinstance(d, list) else [d]):
            if isinstance(r, dict) and "pnl" in r and r.get("size"):
                recs.append(r)
    return recs


def net_gain(r, rate):
    """Net gain_pct re-costed to `rate`. Scales the recorded fee linearly (same
    shares/price, fee is linear in the coefficient)."""
    fee = r.get("fees") or 0.0
    extra = fee * (rate / RECORDED_RATE - 1.0)
    return (r["pnl"] - extra) / r["size"]


def tc(r):
    return (r.get("indicator_snapshot") or {}).get("trade_context") or {}


def et_dt(r):
    try:
        dt = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET)
    except Exception:
        return None


def day_clustered(rows, rate):
    """Return (mean_per_trade, day_mean, day_se, n_days). day_se is the honest SE."""
    by_day = defaultdict(list)
    for r in rows:
        d = et_dt(r)
        if d:
            by_day[d.strftime("%Y-%m-%d")].append(net_gain(r, rate))
    day_means = [st.mean(v) for v in by_day.values() if v]
    pt = st.mean([net_gain(r, rate) for r in rows]) if rows else 0.0
    if len(day_means) >= 2:
        dm = st.mean(day_means)
        se = st.pstdev(day_means) / (len(day_means) ** 0.5)
    else:
        dm, se = pt, float("nan")
    return pt, dm, se, len(by_day)


def table(title, rows, keyfn, rate):
    buckets = defaultdict(list)
    for r in rows:
        k = keyfn(r)
        if k is not None:
            buckets[k].append(r)
    print(f"\n=== {title} ===")
    print(f"  {'slice':<22}{'n':>6}{'win%':>7}{'net/trade':>11}{'day-mean':>10}{'day-SE':>9}{'t_day':>7}")
    out = []
    for k in sorted(buckets, key=lambda x: str(x)):
        rs = buckets[k]
        if len(rs) < MIN_SLICE_N:
            continue
        pt, dm, se, nd = day_clustered(rs, rate)
        wr = 100 * sum(1 for r in rs if net_gain(r, rate) > 0) / len(rs)
        t = (dm / se) if (se and se == se and se > 0) else float("nan")
        flag = "  <<<" if (dm > 0 and t == t and t >= 2.0 and nd >= MIN_DAY_COUNT) else ""
        print(f"  {str(k):<22}{len(rs):>6}{wr:>6.1f}%{pt:>+11.4f}{dm:>+10.4f}{se:>9.4f}{t:>7.2f}{flag}")
        out.append((k, len(rs), pt, dm, se, t, nd))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee", type=float, default=0.07, help="taker coefficient (default 0.07)")
    args = ap.parse_args()
    rate = args.fee
    recs = load()
    if not recs:
        print("no outcomes."); return
    days = sorted({et_dt(r).strftime("%Y-%m-%d") for r in recs if et_dt(r)})
    print(f"loaded {len(recs)} realized outcomes over {len(days)} ET days "
          f"({days[0]} .. {days[-1]}), re-costed at fee={rate}")
    pt, dm, se, nd = day_clustered(recs, rate)
    t = dm / se if se and se == se and se > 0 else float("nan")
    print(f"OVERALL: net/trade {pt:+.4f} | day-mean {dm:+.4f} +/- {se:.4f} (t_day={t:.2f}, {nd} days)")
    print(f"  (>0 needs day-t >= ~2 to be real; {nd} days is a thin base — read as directional)")

    def b_prob(r):
        p = tc(r).get("model_probability")
        if p is None: return None
        return "p>=0.70" if p >= 0.70 else ("p0.60-0.70" if p >= 0.60 else "p0.56-0.60")
    def b_edge(r):
        e = tc(r).get("edge")
        if e is None: return None
        return "edge>=0.08" if e >= 0.08 else ("edge0.04-0.08" if e >= 0.04 else "edge<0.04")
    def b_regime(r):
        a = tc(r).get("regime_autocorr")
        if a is None: return None
        a = abs(a)
        return "|ac|>=0.20" if a >= 0.20 else ("|ac|0.08-0.20" if a >= 0.08 else "|ac|<0.08")
    def b_vol(r):
        a = tc(r).get("atr_rolling_20")
        if not a: return None
        return "atr>=90" if a >= 90 else ("atr60-90" if a >= 60 else "atr<60")
    def b_hour(r):
        d = et_dt(r)
        if not d: return None
        h = d.hour
        return f"ET {h//6*6:02d}-{h//6*6+6:02d}"
    def b_flowagree(r):
        sf = tc(r).get("spot_flow_signal"); side = r.get("side")
        if sf is None or side is None: return None
        agree = (sf > 0 and side == "Up") or (sf < 0 and side == "Down")
        return "flow_agrees" if agree else "flow_disagrees"

    table("Weekday vs weekend", recs,
          lambda r: ("weekend" if et_dt(r) and et_dt(r).weekday() >= 5 else "weekday") if et_dt(r) else None, rate)
    table("Day of week", recs, lambda r: et_dt(r).strftime("%a") if et_dt(r) else None, rate)
    table("Session (ET hour)", recs, b_hour, rate)
    table("Side", recs, lambda r: r.get("side"), rate)
    table("Model probability", recs, b_prob, rate)
    table("Edge bucket", recs, b_edge, rate)
    table("Regime |autocorr|", recs, b_regime, rate)
    table("Volatility (ATR20)", recs, b_vol, rate)
    table("Entry phase", recs, lambda r: tc(r).get("entry_phase"), rate)
    table("Flip vs not", recs, lambda r: ("flip" if tc(r).get("is_flip") else "non_flip") if tc(r) else None, rate)
    table("Exit reason", recs, lambda r: r.get("exit_reason"), rate)
    table("Spot-flow agreement", recs, b_flowagree, rate)

    print("\nVERDICT GUIDE: a slice marked <<< has day-clustered t>=2 over >=3 days — the only kind")
    print("worth a forward confirmation. No <<< = nothing survives honest significance; the overall")
    print("edge (if any) is too thin / too regime-dependent to trade selectively yet. Re-run after")
    print("the next weekend to add independent days before trusting any positive slice.")


if __name__ == "__main__":
    main()
