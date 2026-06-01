#!/usr/bin/env python
"""Offline analysis of passive microstructure telemetry — run after the frozen
multi-day paper baseline to test the three surviving edge candidates.

    python analyze_microstructure.py            # all logged days
    python analyze_microstructure.py 20260530    # one ET day

The forecasting edge is already disproven (model log-loss 1.05 vs market 0.67),
so this tool does NOT look for a better forecast. It tests the only three places a
real edge could live, each with a PRE-REGISTERED threshold so a null can't be
rationalised away:

  A. Quote staleness / latency arb — are CLOB quotes stale (old vs spot) and
     fillable while spot moves?  PURSUE if a meaningful share of spot-move moments
     show book staleness > 0.5s with a live quote; KILL if quotes are ~always fresh.
  B. Resolution lag (last 60s) — is the Chainlink-near-certain winning side offered
     at a fillable discount?  PURSUE if discounts >= 5pp appear and are fillable;
     KILL if the winning side is ~never discounted near expiry.
  C. Fill toxicity — is post-fill mid-drift systematically against us (read from
     outcome edge_decay)?  If yes, taker strategies are dead and the maker pivot is
     indicated.

These are go/no-go reads at ~0.5-1s resolution — the right scale for a non-HFT
retail taker. Numbers are evidence, not proof; treat a thin sample as "inconclusive,
keep collecting", not "edge".
"""
from __future__ import annotations

import glob
import json
import sys
import statistics as st
from pathlib import Path

# Windows cp1252 consoles choke on the em-dashes/box chars below; match main.py.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from polybot.paths import MICROSTRUCTURE_DIR, OUTCOMES_DIR

# Pre-registered thresholds (decided before seeing the data — do not move them after).
STALE_S = 0.5          # A: a quote older than this during a move is "exploitably stale"
STALE_SHARE_PURSUE = 0.20   # A: pursue if >=20% of move-moments are stale+fillable
LAG_KILL_S = 0.15      # A: median staleness below this => no edge at retail latency
DISCOUNT_PP = 0.05     # B: winning-side discount this large counts
NEARCERT_DIST = 30.0   # B: |chainlink - strike| ($) above which the side is "near-certain"
ENDGAME_S = 60.0       # B window


def _pctiles(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return {}
    def p(q):
        i = min(len(xs) - 1, int(q * len(xs)))
        return round(xs[i], 4)
    return {"n": len(xs), "p50": p(0.5), "p90": p(0.9), "p99": p(0.99),
            "max": round(xs[-1], 4)}


def load_rows(day: str | None):
    pat = f"micro_{day}.jsonl" if day else "micro_*.jsonl"
    rows = []
    for fp in sorted(glob.glob(str(MICROSTRUCTURE_DIR / pat))):
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


def analyze_A(rows):
    """Quote staleness during spot moves."""
    by_mkt: dict[str, list] = {}
    for r in rows:
        by_mkt.setdefault(r.get("mid", ""), []).append(r)
    move_moments = 0
    stale_fillable = 0
    staleness_all = []          # ts - bkts (per side, every row with a live quote)
    staleness_on_move = []      # same, restricted to rows just after a spot move
    reprice_lags = []
    for mid, rs in by_mkt.items():
        rs.sort(key=lambda r: r.get("ts", 0))
        for i, r in enumerate(rs):
            ts = r.get("ts", 0)
            for bk, bid, ask in (("bkts_up", "bid_up", "ask_up"), ("bkts_dn", "bid_dn", "ask_dn")):
                bkts = r.get(bk)
                if bkts and bkts > 0:
                    s = ts - bkts
                    if r.get(bid) or r.get(ask):
                        staleness_all.append(s)
            if i == 0:
                continue
            prev = rs[i - 1]
            cb, pcb = r.get("cb"), prev.get("cb")
            if not (cb and pcb and pcb > 0):
                continue
            moved = abs(cb - pcb) / pcb >= 0.0003   # ~3 bps, enough to shift a 5-min binary
            if not moved:
                continue
            move_moments += 1
            # Staleness of the quote you could hit at the moment of the move.
            this_stale = []
            fillable_stale = False
            for bk, bid, ask in (("bkts_up", "bid_up", "ask_up"), ("bkts_dn", "bid_dn", "ask_dn")):
                bkts = r.get(bk)
                if bkts and bkts > 0:
                    s = ts - bkts
                    this_stale.append(s)
                    if s > STALE_S and (r.get(bid) or r.get(ask)):
                        fillable_stale = True
            staleness_on_move.extend(this_stale)
            if fillable_stale:
                stale_fillable += 1
            # Reprice lag: how long until a book ts advances after this move.
            for j in range(i, min(i + 30, len(rs))):
                adv = False
                for bk in ("bkts_up", "bkts_dn"):
                    if rs[j].get(bk) and r.get(bk) and rs[j][bk] > r[bk]:
                        adv = True
                if adv:
                    reprice_lags.append(rs[j].get("ts", 0) - ts)
                    break

    print("\n=== EXPERIMENT A — quote staleness / latency arb ===")
    print(f"spot-move moments observed: {move_moments}")
    print(f"book staleness (ts - book_update), all live quotes: {_pctiles(staleness_all)}")
    print(f"book staleness at spot-move moments:                {_pctiles(staleness_on_move)}")
    print(f"reprice lag after a spot move (s):                  {_pctiles(reprice_lags)}")
    share = (stale_fillable / move_moments) if move_moments else 0.0
    print(f"share of moves with a STALE (>{STALE_S}s) + FILLABLE quote: {share:.1%}")
    med = _pctiles(staleness_on_move).get("p50", 0.0)
    if move_moments < 50:
        print("VERDICT: INCONCLUSIVE — <50 move moments; keep collecting.")
    elif share >= STALE_SHARE_PURSUE:
        print(f"VERDICT: PURSUE — {share:.0%} of moves leave a fillable stale quote (>= {STALE_SHARE_PURSUE:.0%}).")
    elif med < LAG_KILL_S:
        print(f"VERDICT: KILL — median move-moment staleness {med}s < {LAG_KILL_S}s; book reprices ~instantly.")
    else:
        print("VERDICT: WEAK — some staleness but below the pursue bar; marginal at best.")


def analyze_B(rows):
    """Resolution-lag mispricing in the final ENDGAME_S seconds."""
    cand = 0
    fillable_disc = 0
    discounts = []
    for r in rows:
        secs = r.get("secs")
        cl, strike = r.get("cl"), r.get("strike")
        if secs is None or secs > ENDGAME_S or not cl or not strike:
            continue
        dist = cl - strike
        if abs(dist) < NEARCERT_DIST:
            continue
        cand += 1
        # Winning side per Chainlink; its BUY price is that side's ask.
        if dist > 0:
            ask = r.get("ask_up"); bid = r.get("bid_up")
        else:
            ask = r.get("ask_dn"); bid = r.get("bid_dn")
        if not ask or ask <= 0:
            continue
        disc = 1.0 - ask          # how far below $1 you can buy the near-certain side
        discounts.append(disc)
        if disc >= DISCOUNT_PP and bid:   # fillable: a real ask, and a bid exists to exit/confirm liquidity
            fillable_disc += 1

    print("\n=== EXPERIMENT B — resolution lag (last 60s) ===")
    print(f"near-certain endgame snapshots (|cl-strike| > ${NEARCERT_DIST:.0f}, secs<= {ENDGAME_S:.0f}): {cand}")
    print(f"winning-side discount (1 - ask): {_pctiles(discounts)}")
    share = (fillable_disc / cand) if cand else 0.0
    print(f"share offered a FILLABLE discount >= {DISCOUNT_PP*100:.0f}pp: {share:.1%}")
    if cand < 30:
        print("VERDICT: INCONCLUSIVE — <30 near-certain endgame snapshots; keep collecting.")
    elif share >= 0.15:
        print(f"VERDICT: PURSUE — near-certain side is discounted & fillable {share:.0%} of the time.")
    else:
        print("VERDICT: KILL — winning side is ~never fillably discounted near expiry.")


def analyze_C():
    """Fill toxicity from outcome edge_decay (post-fill side-signed mid drift)."""
    files = glob.glob(str(OUTCOMES_DIR / "*.json"))
    horizons = {"5s": [], "10s": [], "15s": [], "30s": [], "60s": []}
    for fp in files:
        try:
            recs = json.load(open(fp))
        except Exception:
            continue
        for o in (recs if isinstance(recs, list) else [recs]):
            if not isinstance(o, dict):
                continue
            ed = (o.get("edge_decay") or {}).get("deltas") or {}
            for k in horizons:
                v = ed.get(k)
                if isinstance(v, (int, float)):
                    horizons[k].append(v)
    print("\n=== EXPERIMENT C — fill toxicity (post-fill mid drift) ===")
    any_data = False
    for k, xs in horizons.items():
        if not xs:
            continue
        any_data = True
        neg = sum(1 for x in xs if x < 0) / len(xs)
        print(f"  {k:>3}: n={len(xs):4d}  mean drift={st.mean(xs):+.4f}  share negative={neg:.0%}")
    if not any_data:
        print("  no edge_decay data yet.")
        return
    m15 = horizons["15s"]
    if m15 and len(m15) >= 30 and st.mean(m15) < -0.01:
        print("VERDICT: TOXIC — fills are systematically followed by adverse drift. "
              "Taker edge unlikely; consider the maker pivot (post liquidity, earn spread).")
    elif m15 and len(m15) >= 30:
        print("VERDICT: OK — no strong adverse post-fill drift at 15s.")
    else:
        print("VERDICT: INCONCLUSIVE — <30 fills with decay data.")


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else None
    rows = load_rows(day)
    print(f"loaded {len(rows)} microstructure rows from "
          f"{MICROSTRUCTURE_DIR}{' day ' + day if day else ' (all days)'}")
    if rows:
        flat = sum(1 for r in rows if r.get("phase") == "flat")
        hold = sum(1 for r in rows if r.get("phase") == "hold")
        mkts = len({r.get("mid") for r in rows})
        print(f"  phases: flat={flat} hold={hold} | distinct markets={mkts}")
    analyze_A(rows)
    analyze_B(rows)
    analyze_C()
    print("\nReminder: thresholds above were pre-registered. A thin sample = keep "
          "collecting, not 'edge'. If A, B, and C all KILL after 2 weeks, the honest "
          "conclusion is no taker edge — pivot to making liquidity or stand down.")


if __name__ == "__main__":
    main()
