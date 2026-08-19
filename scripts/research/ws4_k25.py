"""WS4: is the k>25 deep flow cheap optionality, or do sweeps outrun cancels?

1. Sweep kinematics: winner-side print clusters starting <0.90 at k in
   [25,58] — time to first print at/below each rung price.
2. Flip race: bridged displacement crossing zero at k in [6,58] after having
   cleared 0.5 x p99.5 — engine-true cancel lands at (first tick where signed
   < floor) + cancel RTT; sweep = first print at/below each rung on the
   previously-favored side. P(rung filled before cancel | flip) per rung.
3. Counterfactual: engine-true ladder [6,58] vs [6,25] on the WS1
   walk-forward splits (OOS tables), needs 1.0 and 0.5.

Bar (charter): propose [6,58] only if it beats [6,25] on EW AND dollars
across both splits, with no rung whose flip-race loss probability exceeds
its price margin.
"""
import gzip
import json
import sys
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
import importlib.util

SP = Path(__file__).parent
DATA = SP / "data"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)
spec2 = importlib.util.spec_from_file_location("oos", SP / "ws1_oos.py")
oos = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(oos)

RUNGS = [0.80, 0.65, 0.50, 0.35, 0.20]
CANCEL_LAT = 0.054
CANCEL_LAT_P99 = 0.20        # sensitivity; box-measured p50 0.054, p99 unmeasured
RULE_TS = 1786665600


def main():
    c = lr.load_corpus()
    wins = {w["ep"]: w for w in c["wins"]}
    prints = c["prints"]

    # ---- 1. sweep kinematics -------------------------------------------------
    print("=== sweep kinematics: winner-side clusters first-printing <0.90 at k in [25,58] ===")
    deltas = {rp: [] for rp in RUNGS}
    reached = {rp: 0 for rp in RUNGS}
    n_clusters = 0
    for ep, wd in wins.items():
        close = ep + 300
        wtok = wd["token_up"] if wd["up"] else wd["token_down"]
        ps = [(ts, px) for ts, px, _sz in prints.get(wtok, [])
              if close - 58 <= ts <= close + 60]
        start = next((ts for ts, px in ps
                      if px < 0.90 and 25 <= close - ts <= 58), None)
        if start is None:
            continue
        n_clusters += 1
        for rp in RUNGS:
            hit = next((ts for ts, px in ps if ts >= start and px <= rp + 1e-9), None)
            if hit is not None:
                reached[rp] += 1
                deltas[rp].append(hit - start)
    print(f"{n_clusters} clusters")
    for rp in RUNGS:
        xs = sorted(deltas[rp])
        if xs:
            q = lambda f: xs[min(int(f * len(xs)), len(xs) - 1)]
            print(f"  to {rp:.2f}: reached {reached[rp]}/{n_clusters}  "
                  f"dt q10/50/90: {q(.1):6.2f}/{q(.5):6.2f}/{q(.9):6.2f}s")

    # ---- 2. flip race ---------------------------------------------------------
    print("\n=== flip race: bridged sign flip at k in [6,58] after clearing 0.5xp995 ===")
    from polybot.core.signal_engine import TWAP_MARGIN_P995, twap_margin
    race = {rp: [0, 0] for rp in RUNGS}     # [flips-where-rung-swept-before-cancel, flips]
    n_flips = 0
    for ep, wd in wins.items():
        close = ep + 300
        t0 = close - lr.HORIZON
        strike = wd["strike"]
        if not strike or not wd["final"]:
            continue
        l = sorted(wd["l"])
        l_rx = [(rx, p) for rx, _ts, p in l]
        trecs = sorted(wd.get("t") or [])
        bz = wd["bz"]
        if not bz and c["kl_ts"]:
            i0 = bisect_right(c["kl_ts"], ep + 195)
            i1 = bisect_right(c["kl_ts"], ep + 306)
            bz = [(S + 1 + c["bz_lag"], S + 1.0, px)
                  for S, px in zip(c["kl_ts"][i0:i1], c["kl_px"][i0:i1])]
        cleared_side = None
        last_clear = None
        flip = None            # (t_flip_tick, side_that_had_cleared)
        for i, (rx, p) in enumerate(l_rx):
            k = close - rx
            if k > 58 or k < 6:
                continue
            pr = lr.proj_at(l, l_rx, bz, rx, t0)
            if pr is None:
                continue
            disp = pr - strike
            m = twap_margin(TWAP_MARGIN_P995, k)
            side = "Up" if disp >= 0 else "Down"
            if abs(disp) >= 0.5 * m:
                if cleared_side and side != cleared_side:
                    flip = (rx, cleared_side)
                    break
                cleared_side = side
                last_clear = rx
            elif cleared_side and side != cleared_side:
                flip = (rx, cleared_side)
                break
        if flip is None:
            continue
        n_flips += 1
        t_flip, old_side = flip
        cancel_t = t_flip + CANCEL_LAT
        tok = wd["token_up"] if old_side == "Up" else wd["token_down"]
        ps = [(ts, px) for ts, px, _sz in prints.get(tok, []) if ts >= (last_clear or t_flip)]
        for rp in RUNGS:
            hit = next((ts for ts, px in ps if px < rp - 1e-9 or abs(px - rp) <= 1e-9), None)
            race[rp][1] += 1
            if hit is not None and hit <= cancel_t:
                race[rp][0] += 1
    print(f"{n_flips} flips")
    for rp in RUNGS:
        s, n = race[rp]
        print(f"  rung {rp:.2f}: swept-before-cancel {s}/{n} "
              f"({100 * s / max(1, n):.1f}%)  price margin allows {100 * rp:.0f}% "
              f"loss-fills -> {'OK' if (s / max(1, n)) <= rp else 'EXCEEDS'}")

    # ---- 3. counterfactual [6,58] vs [6,25] on WS1 splits ---------------------
    print("\n=== [6,58] vs [6,25], OOS tables (walk-forward splits) ===")
    eps_by_day = {}
    for ep in wins:
        eps_by_day.setdefault(oos.day_of(ep), set()).add(ep)
    days = sorted(eps_by_day)
    splits = [("A fit 14-15 / score 16-18", days[:2], days[2:]),
              ("B fit 16-18 / score 14-15", days[2:], days[:2])]
    for label, fit_days, sc_days in splits:
        tab = oos.fit_p995(fit_days)
        sc = set().union(*(eps_by_day[d] for d in sc_days))
        print(f"--- {label} ---")
        for need in (1.0, 0.5):
            for kmax in (25.0, 58.0):
                res = lr.run(need=need, k_max=kmax, table=tab, eps=sc)
                fills = [r for r in res if r["filled"] > 0]
                pnl = sum(r["pnl"] for r in fills)
                ew = (pnl / sum(r["filled"] for r in fills)) if fills else float("nan")
                losses = sum(1 for r in fills if not r["win"])
                print(f"  need {need} k<={kmax:2.0f}: arm {len(res):4d} "
                      f"fill {len(fills):2d} loss {losses} pnl {pnl:+8.2f}$ "
                      f"EW {ew * 100 if ew == ew else float('nan'):+6.1f}c/sh")


if __name__ == "__main__":
    main()
