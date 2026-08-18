"""WS2: regime stack recalibration for the 60s rule.

1. |final-strike| and photo-finish distributions per era; port the HOSTILE
   thresholds by the percentile logic the old ones encoded (photo band $2 and
   p50 floor $8 were positions in the 30s-era gap distribution).
2. Coin-flip arms at the staged floor: arms in windows whose final gap lands
   inside p99.5(k_arm) — the resolution treats them as inside projection
   noise — and their engine-true P&L.
3. Kill-rule trip simulation on the engine-true daily P&L series, with and
   without a trailing-HOSTILE suppression gate, current rule vs a min-fills
   variant.
"""
import gzip
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
import importlib.util

SP = Path(__file__).parent
DATA = SP / "data"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from polybot.core.signal_engine import TWAP_MARGIN_P995, twap_margin  # noqa: E402

TWAP_SWITCH = 1786060800
RULE_TS = 1786665600

spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)


def day_of(ep):
    return datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d")


def q(xs, f):
    xs = sorted(xs)
    return xs[min(int(f * len(xs)), len(xs) - 1)] if xs else float("nan")


def pct_below(xs, v):
    xs = sorted(xs)
    from bisect import bisect_right as br
    return br(xs, v) / len(xs) if xs else float("nan")


def main():
    gaps = {}
    for name in ("polybot_paper.db", "polybot_live.db"):
        p = DATA / name
        if not p.exists():
            continue
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        for wid, fp, ptb in db.execute(
                "SELECT window_id, final_price, price_to_beat FROM window_labels "
                "WHERE window_id LIKE 'btc-updown-5m-%'"):
            try:
                ep = int(wid.rsplit("-", 1)[1])
            except ValueError:
                continue
            if ep >= TWAP_SWITCH and fp and ptb:
                gaps.setdefault(ep, abs(fp - ptb))
        db.close()

    era30 = [g for ep, g in gaps.items() if ep < RULE_TS]
    era60 = [g for ep, g in gaps.items() if ep >= RULE_TS]
    print(f"gaps: 30s-era n={len(era30)}, 60s-era n={len(era60)}")
    print(f"30s era: p25 {q(era30, .25):.2f} p50 {q(era30, .5):.2f} p75 {q(era30, .75):.2f}  "
          f"share<\\$2: {100 * pct_below(era30, 2.0):.1f}%")
    print(f"60s era: p25 {q(era60, .25):.2f} p50 {q(era60, .5):.2f} p75 {q(era60, .75):.2f}  "
          f"share<\\$2: {100 * pct_below(era60, 2.0):.1f}%")

    # --- port the thresholds by percentile position -------------------------
    photo_pct_30 = pct_below(era30, 2.0)          # where $2 sat in the 30s dist
    new_photo_band = q(era60, photo_pct_30)
    day_p50_30 = {}
    day_p50_60 = {}
    day_photo = {}
    for ep, g in gaps.items():
        d = day_of(ep)
        (day_p50_30 if ep < RULE_TS else day_p50_60).setdefault(d, []).append(g)
    p50s_30 = sorted(q(v, .5) for v in day_p50_30.values())
    # $8 as a fraction of the 30s-era day-p50 distribution
    frac = pct_below(p50s_30, 8.0)
    p50s_60 = sorted(q(v, .5) for v in day_p50_60.values())
    new_p50_floor = q(p50s_60, frac) if p50s_60 else float("nan")
    print(f"\nold photo band $2 = {100 * photo_pct_30:.1f}th pct of 30s gaps "
          f"-> 60s-era same-pct band = ${new_photo_band:.2f}")
    print(f"old p50 floor $8 sat at {100 * frac:.0f}th pct of 30s-era day-p50s "
          f"{[round(x, 1) for x in p50s_30]}")
    print(f"60s-era day-p50s: { {d: round(q(v, .5), 2) for d, v in sorted(day_p50_60.items())} }")
    print(f"-> ported p50 floor = ${new_p50_floor:.2f} (same percentile)")

    # massacre days (08-14..15) under the 60s rule + new thresholds
    print("\nper-60s-day regime read (gap p50 / photo% at old $2 band / at new band):")
    for d, v in sorted(day_p50_60.items()):
        ph_old = 100 * pct_below(v, 2.0)
        ph_new = 100 * pct_below(v, new_photo_band)
        old_flag = q(v, .5) < 8.0 or ph_old > 15.0
        new_flag = q(v, .5) < new_p50_floor or ph_new > 15.0
        print(f"  {d}: p50 ${q(v, .5):6.2f}  photo(old ${2:.0f}) {ph_old:4.1f}%  "
              f"photo(new ${new_photo_band:.2f}) {ph_new:4.1f}%  "
              f"OLD->{'HOSTILE' if old_flag else 'paying '}  NEW->{'HOSTILE' if new_flag else 'paying '}")

    # --- coin-flip arms + engine-true P&L at the staged floor ---------------
    print("\ncoin-flip arms (final gap < p99.5 at place_k):")
    for need in (0.5, 1.0):
        res = lr.run(need=need)          # shipped tables, full 60s corpus
        coin = [r for r in res
                if r["gap"] < twap_margin(TWAP_MARGIN_P995, r["place_k"])]
        cf = [r for r in coin if r["filled"] > 0]
        pnl = sum(r["pnl"] for r in cf)
        losses = [r for r in cf if not r["win"]]
        print(f"  need {need}: arms {len(res)}, coin-flip arms {len(coin)} "
              f"({100 * len(coin) / max(1, len(res)):.1f}%), filled {len(cf)}, "
              f"pnl {pnl:+.2f}$, losses {len(losses)} "
              f"{[(day_of(r['ep']), round(r['pnl'], 2)) for r in losses]}")
        # daily P&L series for the kill-rule sim (staged floor only)
        if need == 1.0:
            daily = {}
            fills_per_day = {}
            for r in res:
                d = day_of(r["ep"])
                daily[d] = daily.get(d, 0.0) + r["pnl"]
                if r["filled"] > 0:
                    fills_per_day[d] = fills_per_day.get(d, 0) + 1
            days = sorted(set(day_of(r["ep"]) for r in res))
            series = [(d, daily.get(d, 0.0), fills_per_day.get(d, 0)) for d in days]
            print(f"\n  engine-true daily $ series (need 1.0): {series}")
            # current rule: trailing-4-day mean < 0 once >=4 days
            vals = [x for _, x, _ in series]
            fl = [f for _, _, f in series]
            for i in range(3, len(vals)):
                t4 = sum(vals[i - 3:i + 1]) / 4
                nf = sum(fl[i - 3:i + 1])
                trip_now = t4 < 0
                trip_minfill = t4 < 0 and nf >= 5
                print(f"  day {series[i][0]}: trailing4 mean ${t4:+.2f} "
                      f"fills {nf} -> current rule {'TRIP' if trip_now else 'ok'}, "
                      f"min-5-fills variant {'TRIP' if trip_minfill else 'ok'}")


if __name__ == "__main__":
    main()
