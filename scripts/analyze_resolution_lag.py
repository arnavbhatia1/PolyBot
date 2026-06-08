#!/usr/bin/env python
"""Experiment B follow-through — does the resolution-lag discount actually pay?

analyze_microstructure.py Experiment B only proves a discount EXISTS and is fillable
on the Chainlink-near-certain side in the last 60s. That is not an edge: a binary
bought at price p held to resolution only profits if it WINS more than p of the time
(net of fee). The last-60s discount may simply be the market correctly pricing
whipsaw — a $30 cushion at BTC ~$63k is only ~1 minute of volatility.

This joins micro snapshots to GROUND-TRUTH resolutions (outcomes + ghosts: a side's
resolution is `side` if it won, else the opposite) and asks: when you buy the
near-certain discounted side, what is the realized win rate and hold-to-resolution EV
(fee 0.07, entry fee only) — split by safety cushion |chainlink - strike| and by the
price paid. Day-clustered (snapshots within a day are correlated).

    python analyze_resolution_lag.py
"""
from __future__ import annotations

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
from polybot.paths import MICROSTRUCTURE_DIR, OUTCOMES_DIR, GHOSTS_DIR

ET = ZoneInfo("America/New_York")
FEE = 0.07
FLIP = {"Up": "Down", "Down": "Up"}
DISCOUNT_PP = 0.05
ENDGAME_S = 60.0


def _recs(d_dir):
    for fp in sorted(glob.glob(str(d_dir / "*.json"))):
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        for r in (d if isinstance(d, list) else [d]):
            if isinstance(r, dict):
                yield r


def resolution_map():
    """market_id -> winning side ('Up'/'Down'), from realized outcomes + ghosts."""
    res = {}
    for r in _recs(OUTCOMES_DIR):
        m, s, c = r.get("market_id"), r.get("side"), r.get("correct")
        if m and s in FLIP and c is not None:
            res[m] = s if c else FLIP[s]
    for r in _recs(GHOSTS_DIR):               # fills coverage for non-traded windows
        m, s, c = r.get("market_id"), r.get("side"), r.get("ghost_correct")
        if m and s in FLIP and c is not None and m not in res:
            res[m] = s if c else FLIP[s]
    return res


def load_micro():
    rows = []
    for fp in sorted(glob.glob(str(MICROSTRUCTURE_DIR / "micro_*.jsonl"))):
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows


def near_certain_buys(rows, res, cushion, require_disc):
    """One simulated buy per market: the EARLIEST (most time left, most conservative)
    last-60s snapshot where |cl-strike| > cushion, a winning-side ask exists, and a
    bid exists; if require_disc, also disc >= 5pp. Returns list of dicts."""
    best = {}   # market -> (secs, row, side, ask)
    for r in rows:
        secs = r.get("secs")
        cl, strike, m = r.get("cl"), r.get("strike"), r.get("mid")
        if secs is None or secs > ENDGAME_S or not cl or not strike or m not in res:
            continue
        dist = cl - strike
        if abs(dist) < cushion:
            continue
        side = "Up" if dist > 0 else "Down"
        ask = r.get("ask_up") if dist > 0 else r.get("ask_dn")
        bid = r.get("bid_up") if dist > 0 else r.get("bid_dn")
        if not ask or ask <= 0 or not bid:
            continue
        if require_disc and (1.0 - ask) < DISCOUNT_PP:
            continue
        if m not in best or secs > best[m][0]:   # earliest = max secs
            best[m] = (secs, r, side, ask)
    out = []
    for m, (secs, r, side, ask) in best.items():
        won = res[m] == side
        ef = FEE * (1.0 - ask)
        gain = ((1.0 - ask) / ask - ef) if won else (-1.0 - ef)
        out.append({"m": m, "ts": r["ts"], "secs": secs, "side": side, "ask": ask,
                    "cushion": abs(r["cl"] - r["strike"]), "won": won, "gain": gain})
    return out


def day_of(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def report(title, buys):
    if not buys:
        print(f"\n{title}: no matched buys."); return
    by_day = defaultdict(list)
    for b in buys:
        by_day[day_of(b["ts"])].append(b["gain"])
    dms = [st.mean(v) for v in by_day.values()]
    dm = st.mean(dms)
    se = st.pstdev(dms) / (len(dms) ** 0.5) if len(dms) >= 2 else float("nan")
    t = dm / se if se == se and se > 0 else float("nan")
    wr = 100 * sum(b["won"] for b in buys) / len(buys)
    px = st.mean(b["ask"] for b in buys)
    pt = st.mean(b["gain"] for b in buys)
    print(f"\n{title}")
    print(f"  n={len(buys)}  win={wr:.1f}%  avg price={px:.3f} (break-even win ~{100*px:.1f}%)  "
          f"EV/trade={pt:+.4f}")
    print(f"  day-clustered EV {dm:+.4f} +/- {se:.4f} (t_day={t:+.2f}, {len(by_day)} days)")


def main():
    rows = load_micro()
    res = resolution_map()
    covered = len({r.get("mid") for r in rows if r.get("mid") in res})
    total_mkts = len({r.get("mid") for r in rows})
    print(f"micro rows={len(rows)}  markets={total_mkts}  with ground-truth resolution={covered}")

    print("\n##### Calibration: near-certain ($>30 cushion) winning side — win rate vs price #####")
    buys_all = near_certain_buys(rows, res, cushion=30.0, require_disc=False)
    pb = defaultdict(list)
    for b in buys_all:
        a = b["ask"]
        k = "0.80-0.90" if a < 0.90 else ("0.90-0.95" if a < 0.95 else ("0.95-0.98" if a < 0.98 else "0.98+"))
        pb[k].append(b)
    print(f"  {'price band':<12}{'n':>6}{'win%':>8}{'impliedP%':>11}{'gap_pp':>8}{'EV/trade':>11}")
    for k in sorted(pb):
        bs = pb[k]
        wr = 100 * sum(x["won"] for x in bs) / len(bs)
        ip = 100 * st.mean(x["ask"] for x in bs)
        ev = st.mean(x["gain"] for x in bs)
        print(f"  {k:<12}{len(bs):>6}{wr:>7.1f}%{ip:>10.1f}%{wr-ip:>+8.1f}{ev:>+11.4f}")

    print("\n##### Tradeable rule: buy near-certain side at >=5pp discount, by safety cushion #####")
    for lo, hi in [(30, 60), (60, 100), (100, 150), (150, 1e9)]:
        bs = [b for b in near_certain_buys(rows, res, cushion=lo, require_disc=True)
              if b["cushion"] < hi]
        lab = f"cushion ${lo}-{hi if hi < 1e9 else '+'}"
        report(lab, bs)

    print("\n##### Tradeable rule: ALL cushions >=$30, >=5pp discount #####")
    report("all near-certain discounted", near_certain_buys(rows, res, cushion=30.0, require_disc=True))

    print("\nEDGE TEST: a price band edges only if win% > impliedP% (gap_pp > 0) and EV/trade > 0")
    print("with day-t >= ~2. A positive discount with win% ~= price = the market pricing whipsaw")
    print("correctly (fair), not an edge.")


if __name__ == "__main__":
    main()
