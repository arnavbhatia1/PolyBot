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
DAYS = ["2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18"]


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
        table=None, eps=None):
    """Engine-true replay; returns list of per-window results.

    table: p99.5 knots to arm against (default = the frozen P995).
    eps:   restrict scoring to this set of window epochs (walk-forward)."""
    tab = table or P995
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
                cancel_t, cancel_why = rx, "floor"
                break
        if cancel_t is None:
            if winner != side:
                cancel_t, cancel_why = close + WINNER_KNOWN_DELAY, "wrong-winner"
            else:
                cancel_t, cancel_why = close + POST_CLOSE_HOLD, "hold-expiry"

        tok = wd["token_up"] if side == "Up" else wd["token_down"]
        shares = {px: round(BUDGET * 0.20 / px, 2) for px in RUNGS}
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


if __name__ == "__main__":
    main()
