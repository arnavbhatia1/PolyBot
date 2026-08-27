"""Engine-true deep_proj ladder replay under the corrected 60s rule, 08-14+.

Faithful to execution/maker_bid.py + main.py's placement hook:
  - decision ticks = raw Chainlink reports (rx clock)
  - place first tick with k in [6,25], |disp_bridged60| >= 2.0 x p995_60(k),
    coverage guard, stall-veto replica, one ladder per window
  - rungs [0.80/0.65/0.50/0.35/0.20] x 20% of budget ($22.50 at $150 bankroll)
  - survival: cancel when bridged disp drops under the floor / flips / proj None
  - post-close: hold 60s ONLY while label winner == our side (winner known at
    close + 1.7s, the boundary-report p50); unverified windows fail closed
  - paper fills from the print tape: strictly-below fills the rung in FULL;
    at-price prints credit only volume beyond 135 sh; prints only count between
    placement+0.056s and cancel+0.054s (measured GTC latencies)
  - P&L: hold to resolution, maker pays no fee

Also reports per-day regime metrics (trailing-day |final-strike| p50 + photo
share) and per-window sign quality, old engine (realized paper ledger) vs new.
"""
import gzip
import json
import math
import sys
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
REC = Path(__file__).resolve().parents[2] / "polybot" / "memory" / "recordings"
RULE_TS = 1786665600
HORIZON = 60.0
P995 = [(2.0, 1.0), (4.0, 1.0), (6.0, 1.5), (8.0, 2.0), (10.0, 3.5),
        (12.0, 3.5), (15.0, 5.0), (20.0, 6.0), (25.0, 8.0), (29.0, 10.5),
        (35.0, 13.0), (40.0, 18.0), (45.0, 26.5), (50.0, 30.5), (55.0, 36.5),
        (58.0, 38.0)]
NEED = 2.0
K_PLACE = (6.0, 25.0)
RUNGS = [0.80, 0.65, 0.50, 0.35, 0.20]
BUDGET = 150.0 * 0.15
AT_PRICE_QUEUE_SH = 135.0
MIN_SHARES = 5.0
PLACE_LAT = 0.056
CANCEL_LAT = 0.054
POST_CLOSE_HOLD = 60.0
WINNER_KNOWN_DELAY = 1.7
SPOT_STALE_S = 3.0
RAW_GAP_MAX = 10.0
FROZEN_S = 20.0
FROZEN_RAW_MOVE = 2.0
DAYS = [f"2026-08-{d:02d}" for d in range(14, 28)]
EXT_RUNGS = [0.95, 0.90, 0.85, 0.80, 0.65, 0.50, 0.35, 0.20]


def r1_tables(path=None):
    """R1 re-fit knots from r1_tables.json as {P995, MAX, frozen_P995} tuple lists."""
    d = json.load(open(path or DATA / "vps-0821" / "r1_tables.json"))
    return dict(P995=[tuple(x) for x in d["P995"]], MAX=[tuple(x) for x in d["MAX"]],
                frozen_P995=[tuple(x) for x in d["frozen"]["P995"]])


def margin(k, kn=None):
    kn = kn or P995
    if k <= kn[0][0]:
        return kn[0][1]
    for (x0, y0), (x1, y1) in zip(kn, kn[1:]):
        if k <= x1:
            return y0 + (y1 - y0) * (k - x0) / (x1 - x0)
    return kn[-1][1]


def running_avg(recs, start, end):
    seed = None
    pts = []
    for rx, p in recs:
        if rx <= start:
            seed = p
        elif rx <= end:
            pts.append((rx, p))
    if seed is None:
        if not pts or pts[0][0] > start + 2.0:
            return None
        seed = pts[0][1]
    acc, prev_t, prev_p = 0.0, start, seed
    for rx, p in pts:
        acc += prev_p * (rx - prev_t)
        prev_t, prev_p = rx, p
    acc += prev_p * (end - prev_t)
    return acc / (end - start) if end > start else prev_p


def covered(recs, start, end):
    prev = start
    for rx, _p in recs:
        if rx <= start:
            continue
        if rx > end:
            break
        if rx - prev > RAW_GAP_MAX:
            return False
        prev = rx
    return end - prev <= RAW_GAP_MAX


def bridge_delta(ring, raw_ts, t):
    live = [e for e in ring if e[0] <= t]
    if not live:
        return None
    newest_rx, newest_ts, newest_px = live[-1]
    live = [e for e in live if e[1] >= newest_ts - 10.0]
    if newest_ts <= raw_ts:
        return 0.0
    anchor = None
    for _rx, ts, px in live:
        if ts <= raw_ts:
            anchor = px
        else:
            break
    if anchor is None:
        return 0.0
    return newest_px - anchor


def twap_frozen_at(trecs, l_rx, t):
    vals = [(rx, p) for rx, _ts, p in trecs if rx <= t]
    if not vals:
        return False
    v = vals[-1][1]
    since = vals[-1][0]
    for rx, p in reversed(vals):
        if p == v:
            since = rx
        else:
            break
    if t - since < FROZEN_S:
        return False
    spanned = [p for rx, p in l_rx if since <= rx <= t]
    return len(spanned) >= 2 and (max(spanned) - min(spanned)) >= FROZEN_RAW_MOVE


def load_klines():
    rows = {}
    for name in ("binance_1s.csv", "binance_1s_late.csv"):
        p = DATA / name
        if not p.exists():
            continue
        with open(p) as f:
            next(f)
            for line in f:
                a, b = line.rstrip("\n").split(",")
                rows[int(a)] = float(b)
    ts = sorted(rows)
    return ts, [rows[t] for t in ts]


def proj_at(l, l_rx, bz_ring, t, t0):
    """Bridged-60 projection at tick t (engine-faithful; None = cannot decide)."""
    i = bisect_right(l_rx, (t, float("inf"))) - 1
    if i < 0:
        return None
    rx_s, p_s = l_rx[i]
    if t - rx_s > SPOT_STALE_S:
        return None
    if not covered(l_rx, t0, t):
        return None
    A = running_avg(l_rx, t0, t)
    if A is None:
        return None
    w = (t - t0) / HORIZON
    d = bridge_delta(bz_ring, l[i][1], t) if bz_ring else None
    return w * A + (1 - w) * (p_s + (d or 0.0))


_CACHE = {}


def load_corpus():
    if _CACHE:
        return _CACHE
    wins = []
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wd = json.loads(line)
            if wd["ep"] >= RULE_TS:
                wins.append(wd)
    kl_ts, kl_px = load_klines()
    lags = sorted(rx - ts for w in wins for rx, ts, _p in w["bz"] if rx and ts)
    bz_lag = lags[len(lags) // 2] if lags else 0.45
    tok_map = {}
    for wd in wins:
        tok_map[wd["token_up"]] = wd["ep"]
        tok_map[wd["token_down"]] = wd["ep"]
    prints = {}
    for day in DAYS:
        p = REC / f"tape_{day}.jsonl.gz"
        if not p.exists():
            p = REC / f"tape_{day}.jsonl"
        if not p.exists():
            continue
        opener = (lambda q: gzip.open(q, "rt")) if p.suffix == ".gz" \
            else (lambda q: open(q, encoding="utf-8"))
        with opener(p) as f:
            for line in f:
                r = json.loads(line)
                if r["token"] not in tok_map:
                    continue
                try:
                    prints.setdefault(r["token"], []).append(
                        (float(r["ts"]), float(r["price"]), float(r["size"])))
                except (TypeError, ValueError):
                    pass
    for v in prints.values():
        v.sort()
    _CACHE.update(wins=wins, kl_ts=kl_ts, kl_px=kl_px, bz_lag=bz_lag,
                  prints=prints)
    return _CACHE


def run(need=2.0, k_min=6.0, k_max=25.0, anti=False, verbose=False,
        table=None, eps=None, rungs=None, budget=None):
    """Engine-true replay; returns list of per-window results.

    table: p99.5 knots to arm against (default = the frozen P995).
    eps:   restrict scoring to this set of window epochs (walk-forward).
    rungs: ladder prices (default RUNGS); budget split equally per rung.
    budget: ladder dollars (default BUDGET); rungs under MIN_SHARES skip."""
    tab = table or P995
    rungs = rungs or RUNGS
    budget = budget or BUDGET
    frac = 1.0 / len(rungs)
    c = load_corpus()
    results = []
    for wd in sorted(c["wins"], key=lambda w: w["ep"]):
        ep = wd["ep"]
        if eps is not None and ep not in eps:
            continue
        close = ep + 300
        t0 = close - HORIZON
        strike = wd["strike"]
        final = wd["final"]
        if not strike or not final:
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
        winner = "Up" if wd["up"] else "Down"

        place_t = side = None
        place_mult = None
        for rx, _p in l_rx:
            k = close - rx
            if k > k_max or k < k_min:
                continue
            if twap_frozen_at(trecs, l_rx, rx):
                continue
            pr = proj_at(l, l_rx, bz, rx, t0)
            if pr is None:
                continue
            disp = pr - strike
            m = margin(k, tab)
            if abs(disp) >= need * m:
                place_t = rx
                place_mult = abs(disp) / m if m > 0 else None
                side = "Up" if disp >= 0 else "Down"
                if anti:
                    side = "Down" if side == "Up" else "Up"
                break
        if place_t is None:
            continue

        cancel_t = cancel_why = None
        for rx, _p in l_rx:
            if rx <= place_t or rx > close:
                continue
            k = close - rx
            pr = proj_at(l, l_rx, bz, rx, t0)
            if pr is None:
                cancel_t, cancel_why = rx, "cold"
                break
            signed = (pr - strike) if side == "Up" else (strike - pr)
            if anti:
                signed = -signed
            if signed < need * margin(max(k, 0.01), tab):
                cancel_t, cancel_why = rx, ("flip" if signed < 0 else "floor")
                break
        if cancel_t is None:
            if winner != side:
                cancel_t, cancel_why = close + WINNER_KNOWN_DELAY, "wrong-winner"
            else:
                cancel_t, cancel_why = close + POST_CLOSE_HOLD, "hold-expiry"

        tok = wd["token_up"] if side == "Up" else wd["token_down"]
        shares = {px: round(budget * frac / px, 2) for px in rungs}
        shares = {px: s for px, s in shares.items() if s >= MIN_SHARES}
        filled = {px: 0.0 for px in shares}
        at_vol = {px: 0.0 for px in shares}
        fill_px = {}
        for ts, px, sz in c["prints"].get(tok, []):
            if ts < place_t + PLACE_LAT or ts > cancel_t + CANCEL_LAT:
                continue
            for rp in shares:
                if px < rp - 1e-9:
                    if filled[rp] < shares[rp]:
                        filled[rp] = shares[rp]
                        fill_px[rp] = True
                elif abs(px - rp) <= 1e-9:
                    at_vol[rp] += sz
                    credit = min(shares[rp], max(0.0, at_vol[rp] - AT_PRICE_QUEUE_SH))
                    if credit > filled[rp]:
                        filled[rp] = credit
                        fill_px[rp] = True
        tot = sum(filled.values())
        row = dict(ep=ep, side=side, winner=winner, place_k=close - place_t,
                   why=cancel_why, gap=abs(final - strike),
                   place_mult=round(place_mult, 3) if place_mult else None,
                   placed=sorted(shares, reverse=True),
                   rungs={rp: filled[rp] for rp in filled if filled[rp] > 0})
        if tot <= 0:
            row.update(win=None, pnl=0.0, filled=0.0)
        else:
            notional = sum(filled[rp] * rp for rp in filled)
            vwap = notional / tot
            win = side == winner
            row.update(win=win, filled=tot, vwap=vwap,
                       pnl=(1.0 - vwap) * tot if win else -vwap * tot)
        results.append(row)
    return results


def summarize(results, label):
    fills = [r for r in results if r["filled"] > 0]
    pnl = sum(r["pnl"] for r in fills)
    wins_n = sum(1 for r in fills if r["win"])
    sign_ok = sum(1 for r in results if r["side"] == r["winner"])
    # per-rung-price fill economics
    rung_stat = {}
    for r in fills:
        for rp, sh in r["rungs"].items():
            s = rung_stat.setdefault(rp, [0, 0, 0.0])
            s[0] += 1
            s[1] += 1 if r["win"] else 0
            s[2] += sh * ((1 - rp) if r["win"] else -rp)
    rs = " ".join(f"{rp}:{s[1]}/{s[0]}({s[2]:+.0f}$)"
                  for rp, s in sorted(rung_stat.items(), reverse=True))
    print(f"{label:24s} armed {len(results):4d}  filled {len(fills):3d}  "
          f"wins {wins_n:3d}  pnl {pnl:+8.2f}$  sign {sign_ok}/{len(results)}"
          f"  rungs[{rs}]")
    return pnl, len(fills), wins_n


def rung_stats(results, rungs):
    """Per-rung economics: placements, fills, flip-cancel fills, wins, $."""
    st = {rp: dict(placements=0, fills=0, flip=0, floor=0, wins=0, sh=0.0,
                   dollars=0.0) for rp in rungs}
    for r in results:
        for rp in r.get("placed", []):
            st[rp]["placements"] += 1
        for rp, sh in r["rungs"].items():
            s = st[rp]
            s["fills"] += 1
            if r["why"] == "flip":
                s["flip"] += 1
            elif r["why"] == "floor":
                s["floor"] += 1
            if r["win"]:
                s["wins"] += 1
            s["sh"] += sh
            s["dollars"] += sh * ((1 - rp) if r["win"] else -rp)
    return st


def day_split(results):
    """Dollars per ET day (August => EDT, fixed UTC-4)."""
    days = {}
    for r in results:
        d = datetime.fromtimestamp(r["ep"] - 4 * 3600,
                                   tz=timezone.utc).strftime("%m-%d")
        days[d] = round(days.get(d, 0.0) + r["pnl"], 2)
    return dict(sorted(days.items()))


def print_run(name, res, rungs):
    summarize(res, name)
    st = rung_stats(res, rungs)
    for rp in rungs:
        s = st[rp]
        wpct = 100.0 * s["wins"] / s["fills"] if s["fills"] else float("nan")
        cps = 100.0 * s["dollars"] / s["sh"] if s["sh"] else float("nan")
        print(f"  rung {rp:.2f}: placed {s['placements']:4d}  fills {s['fills']:3d}"
              f"  flip {s['flip']:2d}  floor {s['floor']:2d}"
              f"  win% {wpct:5.1f} (be {100 * rp + 5:.0f})"
              f"  sh {s['sh']:8.1f}  c/sh {cps:+7.2f}  $ {s['dollars']:+8.2f}")
    ds = day_split(res)
    print(f"  by-day $: {json.dumps(ds)}")
    h1 = sum(v for d, v in ds.items() if d <= "08-17")
    h2 = sum(v for d, v in ds.items() if d >= "08-18")
    print(f"  halves $: 08-14..17 {h1:+.2f} | 08-18..21 {h2:+.2f}")
    return st


def h1b_main():
    """H1B extended-rung measurement: 6 runs, all need 1.0 k[6,25]."""
    c = load_corpus()
    print(f"{len(c['wins'])} 60s-rule windows, {len(c['kl_ts'])} klines, "
          f"bz median lag {c['bz_lag']:.2f}s")
    runs = {
        "base_b22": dict(need=1.0),
        "base_b60": dict(need=1.0, budget=60.0),
        "ext_b22": dict(need=1.0, rungs=EXT_RUNGS),
        "ext_b60": dict(need=1.0, rungs=EXT_RUNGS, budget=60.0),
        "anti_ext_b22": dict(need=1.0, rungs=EXT_RUNGS, anti=True),
        "anti_ext_b60": dict(need=1.0, rungs=EXT_RUNGS, budget=60.0, anti=True),
    }
    out = {}
    for name, kw in runs.items():
        res = run(**kw)
        out[name] = dict(params={k: v for k, v in kw.items()}, results=res)
        print_run(name, res, kw.get("rungs", RUNGS))
    p = DATA / "vps-0821" / "h1b_results.json"
    json.dump(out, open(p, "w"))
    print(f"saved {p}")


def main():
    c = load_corpus()
    print(f"{len(c['wins'])} 60s-rule windows, {len(c['kl_ts'])} klines")
    print("\n=== grid: needs x k_place_max (engine-true, 08-14..17) ===")
    for need in (2.0, 1.5, 1.0, 0.5):
        for k_max in (25.0, 40.0, 55.0):
            summarize(run(need=need, k_max=k_max), f"need {need} k<= {k_max:.0f}")
    print("\n=== ANTI-side controls ===")
    for need in (1.0, 0.5):
        for k_max in (40.0, 55.0):
            summarize(run(need=need, k_max=k_max, anti=True),
                      f"ANTI need {need} k<= {k_max:.0f}")


# ---------------------------------------------------------------------------
# Candidate A — cushion dip-buyer (WALLETS.md): both-sides deep rungs, no gate.
# ---------------------------------------------------------------------------
CAND_RUNGS = [0.35, 0.30, 0.25, 0.20, 0.15, 0.10]
CAND_RUNGS_BAND = [0.35, 0.30, 0.25]
CAND_BUDGET = 60.0
REF_LOOKBACK = 120.0


def market_ref(prints, t):
    """Last print price on a token before t (within REF_LOOKBACK); None if none."""
    i = bisect_right(prints, (t, float("inf"), float("inf"))) - 1
    if i < 0 or t - prints[i][0] > REF_LOOKBACK:
        return None
    return prints[i][1]


def signed_mult_at(l, l_rx, bz, strike, side, t, close):
    """Projection displacement toward `side` in p99.5 units at tick t.

    k <= 60: bridged-60 projection (engine sign). k > 60: the averaging window
    has not opened, so the sign is spot-vs-strike (tagged 'spot'). None = cold."""
    k = close - t
    if k > HORIZON:
        i = bisect_right(l_rx, (t, float("inf"))) - 1
        if i < 0 or t - l_rx[i][0] > SPOT_STALE_S:
            return None, "na"
        d = l_rx[i][1] - strike
        signed = d if side == "Up" else -d
        return signed / margin(k), "spot"
    pr = proj_at(l, l_rx, bz, t, close - HORIZON)
    if pr is None:
        return None, "na"
    d = pr - strike
    signed = d if side == "Up" else -d
    return signed / margin(max(k, 0.01)), "proj"


def run_candidate_a(rungs=None, budget=CAND_BUDGET, k_max=25.0, k_min=6.0,
                    post_close="close", eps=None, rest="below_ref"):
    """Both-sides cushion ladder; returns one row per window (both sides).

    Placement is wall-clock at close - k_max (no signal to tick on; the raw
    stream in win_streams only covers k <= ~80 so a tick clock cannot reach
    k=120). Budget splits equally over ALL resting rungs (both sides);
    rungs under MIN_SHARES are starved (reported). Fills: same print-through
    rule and GTC latencies as run(). post_close: 'close' cancels both sides
    at the close; 'engine' mirrors deep_proj (loser side cancelled at close +
    WINNER_KNOWN_DELAY, winner side held POST_CLOSE_HOLD). Each fill carries
    the signed projection multiple toward its side at fill time (need units)
    so the sign-gated overlay can be read off the same fills.

    rest: 'below_ref' rests a rung only where it is a passive bid — strictly
    below the token's last print before placement (fallback 1 - the other
    token's last print; no reference = nothing rests on that side). A bid
    above the market is a crossing taker order, not a cushion, and the
    print-through rule would mis-credit it at the rung price. 'all' rests
    every rung unconditionally (the raw record of why the rule is needed)."""
    rungs = rungs or CAND_RUNGS
    c = load_corpus()
    n_r = 2 * len(rungs)
    want = {px: round(budget / n_r / px, 2) for px in rungs}
    shares_tpl = {px: s for px, s in want.items() if s >= MIN_SHARES}
    starved = sorted(px for px in rungs if px not in shares_tpl)
    results = []
    for wd in sorted(c["wins"], key=lambda w: w["ep"]):
        ep = wd["ep"]
        if eps is not None and ep not in eps:
            continue
        close = ep + 300
        strike, final = wd["strike"], wd["final"]
        if not strike or not final:
            continue
        l = sorted(wd["l"])
        l_rx = [(rx, p) for rx, _ts, p in l]
        bz = wd["bz"]
        if not bz and c["kl_ts"]:
            i0 = bisect_right(c["kl_ts"], ep + 195)
            i1 = bisect_right(c["kl_ts"], ep + 306)
            bz = [(S + 1 + c["bz_lag"], S + 1.0, px)
                  for S, px in zip(c["kl_ts"][i0:i1], c["kl_px"][i0:i1])]
        winner = "Up" if wd["up"] else "Down"
        place_t = close - k_max
        refs = {}
        for side in ("Up", "Down"):
            tok = wd["token_up"] if side == "Up" else wd["token_down"]
            refs[side] = market_ref(c["prints"].get(tok, []), place_t)
        for side, other in (("Up", "Down"), ("Down", "Up")):
            if refs[side] is None and refs[other] is not None:
                refs[side] = 1.0 - refs[other]
        sides = {}
        pnl_w = 0.0
        for side in ("Up", "Down"):
            ref = refs[side]
            if rest == "all":
                rested = dict(shares_tpl)
            elif ref is None:
                rested = {}
            else:
                rested = {px: sh for px, sh in shares_tpl.items() if px < ref - 1e-9}
            if post_close == "engine":
                cancel_t = close + (POST_CLOSE_HOLD if side == winner
                                    else WINNER_KNOWN_DELAY)
            else:
                cancel_t = close
            tok = wd["token_up"] if side == "Up" else wd["token_down"]
            filled = {px: 0.0 for px in rested}
            at_vol = {px: 0.0 for px in rested}
            fill_t = {}
            for ts, px, sz in c["prints"].get(tok, []):
                if ts < place_t + PLACE_LAT or ts > cancel_t + CANCEL_LAT:
                    continue
                for rp in rested:
                    if px < rp - 1e-9:
                        if filled[rp] < rested[rp]:
                            filled[rp] = rested[rp]
                            fill_t.setdefault(rp, ts)
                    elif abs(px - rp) <= 1e-9:
                        at_vol[rp] += sz
                        credit = min(rested[rp],
                                     max(0.0, at_vol[rp] - AT_PRICE_QUEUE_SH))
                        if credit > filled[rp]:
                            filled[rp] = credit
                            fill_t.setdefault(rp, ts)
            win = side == winner
            fills = {}
            pnl_s = 0.0
            for rp, sh in filled.items():
                if sh <= 0:
                    continue
                t = fill_t[rp]
                mult, src = signed_mult_at(l, l_rx, bz, strike, side, t, close)
                fills[rp] = dict(sh=sh, t=round(t - close, 3), k=round(close - t, 3),
                                 mult=None if mult is None else round(mult, 3),
                                 src=src, pnl=sh * ((1 - rp) if win else -rp))
                pnl_s += fills[rp]["pnl"]
            sides[side] = dict(win=win, fills=fills, pnl=pnl_s,
                               cancel_t=round(cancel_t - close, 3),
                               ref=None if ref is None else round(ref, 3),
                               placed=sorted(rested, reverse=True))
            pnl_w += pnl_s
        results.append(dict(ep=ep, winner=winner, gap=abs(final - strike),
                            place_k=k_max, placed=sorted(shares_tpl, reverse=True),
                            starved=starved, sides=sides, pnl=pnl_w,
                            filled=sum(f["sh"] for s in sides.values()
                                       for f in s["fills"].values())))
    return results


def cand_rung_stats(results, rungs, need=1.0):
    """Per-rung economics for Candidate A rows, with the sign overlay.

    fav/anti split every fill by the projection multiple toward the filled
    side at fill time: fav = mult >= need (deep_proj would rest this side),
    anti = mult < 0 (projection points the other way), weak = [0, need),
    na = cold. Each bucket carries its own fills / wins / dollars."""
    def bucket():
        return dict(fills=0, wins=0, sh=0.0, dollars=0.0)
    st = {rp: dict(placements=0, fills=0, wins=0, sh=0.0, dollars=0.0, ks=[],
                   fav=bucket(), weak=bucket(), anti=bucket(), na=bucket(),
                   Up=bucket(), Down=bucket()) for rp in rungs}
    for r in results:
        for side, s in r["sides"].items():
            for rp in s["placed"]:
                st[rp]["placements"] += 1
            for rp, f in s["fills"].items():
                b = st[rp]
                b["fills"] += 1
                b["wins"] += 1 if s["win"] else 0
                b["sh"] += f["sh"]
                b["dollars"] += f["pnl"]
                b["ks"].append(f["k"])
                m = f["mult"]
                key = ("na" if m is None else "fav" if m >= need
                       else "anti" if m < 0 else "weak")
                for kk in (key, side):
                    b[kk]["fills"] += 1
                    b[kk]["wins"] += 1 if s["win"] else 0
                    b[kk]["sh"] += f["sh"]
                    b[kk]["dollars"] += f["pnl"]
    for rp, b in st.items():
        ks = sorted(b["ks"])
        b["k_med"] = ks[len(ks) // 2] if ks else None
        b["k_p25"] = ks[len(ks) // 4] if ks else None
        b["k_p75"] = ks[(3 * len(ks)) // 4] if ks else None
        del b["ks"]
    return st


def print_cand(name, res, rungs, need=1.0):
    fills = [(r, s) for r in res for s in r["sides"].values() if s["fills"]]
    pnl = sum(r["pnl"] for r in res)
    both = sum(1 for r in res if all(s["fills"] for s in r["sides"].values()))
    rested = sum(1 for r in res for s in r["sides"].values() if s["placed"])
    noref = sum(1 for r in res for s in r["sides"].values() if s["ref"] is None)
    print(f"{name:28s} windows {len(res):4d}  sides-resting {rested:4d}  no-ref {noref:3d}  "
          f"side-fills {len(fills):4d}  both-sides {both:3d}  pnl {pnl:+9.2f}$  "
          f"starved {res[0]['starved'] if res else []}")
    st = cand_rung_stats(res, rungs, need)
    for rp in rungs:
        b = st[rp]
        wp = 100.0 * b["wins"] / b["fills"] if b["fills"] else float("nan")
        cps = 100.0 * b["dollars"] / b["sh"] if b["sh"] else float("nan")
        ov = " ".join(f"{k}:{b[k]['fills']}({b[k]['dollars']:+.0f}$)"
                      for k in ("fav", "weak", "anti", "na"))
        print(f"  rung {rp:.2f}: fills {b['fills']:4d}  win% {wp:5.1f} (be {100 * rp + 8:.0f})"
              f"  sh {b['sh']:8.1f}  c/sh {cps:+7.2f}  $ {b['dollars']:+8.2f}"
              f"  k_med {b['k_med']}  [{ov}]")
    print(f"  by-day $: {json.dumps(day_split(res))}")
    return st


def candidate_a_main():
    """R4 Candidate A: both-sides variants + the sign-gated comparison."""
    c = load_corpus()
    print(f"{len(c['wins'])} 60s-rule windows, {len(c['kl_ts'])} klines, "
          f"bz median lag {c['bz_lag']:.2f}s")
    out = {}
    p = DATA / "vps-0821" / "r4_results.json"
    cand = {
        "A6_k25": dict(rungs=CAND_RUNGS, k_max=25.0),
        "A6_k120": dict(rungs=CAND_RUNGS, k_max=120.0),
        "A3_k25": dict(rungs=CAND_RUNGS_BAND, k_max=25.0),
        "A3_k120": dict(rungs=CAND_RUNGS_BAND, k_max=120.0),
        "A6_k25_engine_pc": dict(rungs=CAND_RUNGS, k_max=25.0, post_close="engine"),
        "A6_k120_engine_pc": dict(rungs=CAND_RUNGS, k_max=120.0, post_close="engine"),
        "A6_k60": dict(rungs=CAND_RUNGS, k_max=60.0),
        "A6_k25_all": dict(rungs=CAND_RUNGS, k_max=25.0, rest="all"),
        "A6_k120_all": dict(rungs=CAND_RUNGS, k_max=120.0, rest="all"),
    }
    for name, kw in cand.items():
        res = run_candidate_a(budget=CAND_BUDGET, **kw)
        out[name] = dict(kind="candidate_a", params=kw, results=res)
        print_cand(name, res, kw["rungs"])
        json.dump(out, open(p, "w"))
    # sign-gated comparison: same rungs, projection side only, need 1.0.
    # budget matched per rung ($5/rung = $60 over 12 both-sides rungs).
    gated = {
        "S6_k25_b30": dict(need=1.0, rungs=CAND_RUNGS, budget=30.0, k_max=25.0),
        "S6_k25_b60": dict(need=1.0, rungs=CAND_RUNGS, budget=60.0, k_max=25.0),
        "S3_k25_b30": dict(need=1.0, rungs=CAND_RUNGS_BAND, budget=30.0, k_max=25.0),
        "S6_k120_b30": dict(need=1.0, rungs=CAND_RUNGS, budget=30.0, k_max=120.0),
        "ANTI6_k25_b30": dict(need=1.0, rungs=CAND_RUNGS, budget=30.0, k_max=25.0,
                              anti=True),
    }
    for name, kw in gated.items():
        res = run(**kw)
        out[name] = dict(kind="sign_gated", params=kw, results=res)
        print_run(name, res, kw["rungs"])
        json.dump(out, open(p, "w"))
    print(f"saved {p}")


MODES = {"h1b": h1b_main, "candidate_a": candidate_a_main}

if __name__ == "__main__":
    MODES.get(sys.argv[1] if len(sys.argv) > 1 else "", main)()

