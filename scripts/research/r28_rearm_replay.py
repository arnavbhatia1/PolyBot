"""r28 (09-09): ws2_ladder_replay.py + the production RE-ARM state machine (the harness behind docs/research/engine_restart_2026-09-08.md T2).

Copied 2026-09-09 from the repo (md5 fbce52f8899c8595dc02fd576792518c); the repo copy is untouched.
The original run()/summarize()/rung_stats()/day_split()/print_run()/candidate-A code is kept verbatim
below (regression anchor: run(need=0.6,k_max=25,R8,$200,P995) = 17 fills / 17 wins / +$1,882.72).

CHANGES vs the repo harness (complete list):
 1. DATA / REC made absolute to the repo (this copy lives in the scratchpad).
 2. run2(): production RE-ARM state machine (rearm=True). After a cancel with no booked position the
    ladder is idle again (maker_bid._retire sets active=None) and re-arms at the first raw tick with
    r >= min_need, k in [k_min, k_max], on whichever side the sign names; the re-arm scan resumes AT the
    cancel tick (main loop: _MAKER_MGR.maintain() precedes _evaluate_signal_and_enter, main.py:2246 vs
    2255+), so a flip can re-arm on the same tick. A booking (fills > 0 and notional >= MIN_NOTIONAL_USD,
    maker_bid._book) creates the DB position that main.py:1048-1055 refuses on -> no later arm.
    rearm=False = the ws2 one-arm convention. Fills of an unbooked arm (notional < $1) are dropped, as _book does.
 3. run2(): per-rung need schedule `needs` {px: need}, production semantics (maker_bid.ladder() /
    consider_placement() / maintain()): place when r >= min(needs); only rungs whose need <= r at the
    placement tick rest (no late joins); the cancel floor is min(needs) for every rung.
 4. run2(): pc_mode="capture" = maker_bid.certain_winner() replica from boundaries.json (ws1_reduce: first
    sixty-topic report with payload ts in [B, B+300); trusted iff ts - B <= STRIKE_TRUST_GAP_S=0.5 and a prior
    report exists). Post-close: winner unknown/untrusted by close+PC_VERIFY_GRACE_S=5 -> cancel "unverified";
    trusted captures naming the other side -> cancel at the final report's rx; else hold to close+60.
    pc_mode="label" = ws2's convention (label winner known at close+1.7 s).
 5. run2(): H2 reprice cancel (reprice=dict(thr, lat, win)) from the micro BBO tape (book-dynamics
    micro_b_dedup.parquet: distinct BBO price-state changes, receipt clock): while resting, at each BBO
    change of the complement token cancel if its ask rose >= thr vs the trailing `win` s (min of the ask in
    force at t-win and every ask in (t-win, t)); at each change of our token cancel if its ask fell >= thr
    vs the trailing max. Cancel takes effect at t + lat; fills stop at cancel + CANCEL_LAT. Trailing-only
    (rx <= t) information. reprice_latch=True: no re-arm in the window after a reprice cancel.
 6. run2(): `exclude` (set of eps, dead-book windows) skipped; each row carries n_arms, arms[], booked_arm.
 7. New helpers: load_bbo(), boundary_table(), capture_trusted(), post_close_capture(), reprice_trigger(),
    fill_arm(), et_day(), halves(), summarize2(), per_rung(), by_day(), day_boot(). Nothing else changed.
"""

import gzip
import json
import math
import sys
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("C:/Users/abhat/Personal/PolyBot")
SP = REPO / "scripts" / "research"
DATA = SP / "data"
REC = REPO / "polybot" / "memory" / "recordings"
SCRATCH = Path(__file__).resolve().parent
BOOKDYN = DATA  # H2 reads micro_b_dedup.parquet (BBO price-state changes) from data/; built by the 09-08 book-dynamics scout
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
DAYS = [f"2026-08-{d:02d}" for d in range(14, 32)] + [f"2026-09-{d:02d}" for d in range(1, 8)]
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



# ===========================================================================
# T2 extension (see module docstring for the change list)
# ===========================================================================
import numpy as np
from bisect import bisect_left

MIN_NOTIONAL_USD = 1.0          # maker_bid.MIN_NOTIONAL_USD: below this _book books nothing
PC_VERIFY_GRACE_S = 5.0         # maker_bid.PC_VERIFY_GRACE_S
STRIKE_TRUST_GAP_S = 0.5        # chainlink_feed.STRIKE_TRUST_GAP_S
R8 = [0.80, 0.65, 0.50, 0.35, 0.20, 0.15, 0.10, 0.05]
ET_SPLIT_DAY = "2026-08-26"     # first13 = ET days <= 08-25, rest = >= 08-26 (information-structure convention)

_BBO = {}
_BND = {}


def et_day(ep):
    return datetime.fromtimestamp(ep - 4 * 3600, tz=timezone.utc).strftime("%Y-%m-%d")


def halves(ep):
    return "H1" if et_day(ep) < ET_SPLIT_DAY else "H2"


def load_bbo():
    """{(ep, side): (ts ndarray, ask ndarray)} from book-dynamics micro_b_dedup.parquet; side 0=Up token, 1=Down."""
    if _BBO:
        return _BBO
    import pandas as pd
    df = pd.read_parquet(BOOKDYN / "micro_b_dedup.parquet", columns=["ep", "side", "ts", "ask"])
    df = df.sort_values(["ep", "side", "ts"], kind="stable")
    for (ep, side), g in df.groupby(["ep", "side"], sort=False):
        _BBO[(int(ep), int(side))] = (g.ts.values.astype(np.float64), g.ask.values.astype(np.float64))
    return _BBO


def boundary_table():
    """boundaries.json -> {B: (ts, rx, price, prev_ts)}; B = int(ts // 300) * 300 (first report in the bucket,
    first write wins) -- the same capture rule as ChainlinkFeed (chainlink_feed.py:415-420)."""
    if _BND:
        return _BND
    d = json.load(open(DATA / "boundaries.json"))
    for k, v in d.items():
        _BND[int(k)] = (v[0], v[1], v[2], v[3])
    return _BND


def capture_trusted(bnd, B):
    e = bnd.get(B)
    return (e is not None and e[3] is not None and (e[0] - B) <= STRIKE_TRUST_GAP_S)


def post_close_capture(bnd, ep, side):
    """certain_winner() replica. Returns (cancel_t, why, winner_from_captures_or_None)."""
    close = ep + 300
    o, cl = bnd.get(ep), bnd.get(close)
    if not (capture_trusted(bnd, ep) and capture_trusted(bnd, close)):
        return close + PC_VERIFY_GRACE_S, "unverified", None
    rx_c = cl[1] if cl[1] is not None else cl[0] + WINNER_KNOWN_DELAY
    if rx_c > close + PC_VERIFY_GRACE_S:
        return close + PC_VERIFY_GRACE_S, "unverified", None
    wcap = "Up" if cl[2] >= o[2] else "Down"
    if wcap != side:
        return max(rx_c, close), "wrong-winner", wcap
    return close + POST_CLOSE_HOLD, "hold-expiry", wcap


def _trailing_ref(ts, ask, e, win, want_min):
    """Reference over the trailing window for event index e: the ask in force at ts[e]-win plus every
    ask strictly inside (ts[e]-win, ts[e]). NaN (missing side) ignored. None if no numeric reference."""
    t = ts[e]
    i0 = bisect_right(ts, t - win) - 1
    if i0 < 0:
        i0 = 0
    seg = ask[i0:e]
    seg = seg[~np.isnan(seg)]
    if seg.size == 0:
        return None
    return float(seg.min() if want_min else seg.max())


def reprice_trigger(bbo, ep, side, t_start, t_end, cfg):
    """First (t, kind, move) in (t_start, t_end] at which the H2 rule fires, else None (trailing information only)."""
    thr, win = cfg["thr"], cfg.get("win", 1.0)
    own = 0 if side == "Up" else 1
    comp = 1 - own
    cands = []
    for s in (own, comp):
        arr = bbo.get((ep, s))
        if arr is None:
            continue
        ts, ask = arr
        lo, hi = bisect_right(ts, t_start), bisect_right(ts, t_end)
        for e in range(lo, hi):
            cur = ask[e]
            if np.isnan(cur):
                continue
            if s == comp:
                ref = _trailing_ref(ts, ask, e, win, want_min=True)
                if ref is not None and cur - ref >= thr - 1e-9:
                    cands.append((float(ts[e]), "comp_ask_up", round(cur - ref, 3)))
            else:
                ref = _trailing_ref(ts, ask, e, win, want_min=False)
                if ref is not None and ref - cur >= thr - 1e-9:
                    cands.append((float(ts[e]), "own_ask_down", round(ref - cur, 3)))
    if not cands:
        return None
    return min(cands)


def fill_arm(prints, place_t, cancel_t, placed):
    """Production paper fill rule (maker_bid.on_print / ws2 run()): strictly-below print fills the rung in full;
    at-price prints credit only volume beyond AT_PRICE_QUEUE_SH. Prints count in [place+PLACE_LAT, cancel+CANCEL_LAT]."""
    filled = {px: 0.0 for px in placed}
    at_vol = {px: 0.0 for px in placed}
    first_fill_t = None
    lo = bisect_left(prints, (place_t + PLACE_LAT, -1.0, -1.0))
    for idx in range(lo, len(prints)):
        ts, px, sz = prints[idx]
        if ts > cancel_t + CANCEL_LAT:
            break
        for rp in placed:
            if px < rp - 1e-9:
                if filled[rp] < placed[rp]:
                    filled[rp] = placed[rp]
                    first_fill_t = ts if first_fill_t is None else min(first_fill_t, ts)
            elif abs(px - rp) <= 1e-9:
                at_vol[rp] += sz
                credit = min(placed[rp], max(0.0, at_vol[rp] - AT_PRICE_QUEUE_SH))
                if credit > filled[rp]:
                    filled[rp] = credit
                    first_fill_t = ts if first_fill_t is None else min(first_fill_t, ts)
    return filled, first_fill_t


def run2(need=0.6, k_min=6.0, k_max=25.0, anti=False, table=None, eps=None, rungs=None,
         budget=None, needs=None, rearm=True, pc_mode="label", reprice=None,
         reprice_latch=True, exclude=None, max_arms=50, corpus=None):
    """Engine-true replay with the production re-arm state machine. One row per ARMED window.

    need/needs: uniform need, or {rung_px: need} (production per-rung semantics, change 3).
    rearm: True = production (re-arm after a zero-fill cancel); False = ws2 one-arm convention.
    pc_mode: "label" (ws2) | "capture" (certain_winner replica, change 4).
    reprice: None | dict(thr=0.05, lat=0.0, win=1.0) (change 5); reprice_latch: no re-arm after it.
    exclude: eps to skip. corpus: injected corpus dict (unit tests). Returns rows with ep, winner, n_arms,
    arms, booked_arm, side, why, filled, vwap, win, pnl, place_k, place_mult, rungs, placed
    (booking arm, else the last arm)."""
    tab = table or P995
    rungs = list(rungs or RUNGS)
    budget = budget or BUDGET
    frac = 1.0 / len(rungs)
    needs = dict(needs) if needs else {px: need for px in rungs}
    assert set(needs) == set(rungs), "needs must cover every rung"
    min_need = min(needs.values())
    c = corpus or load_corpus()
    bbo = load_bbo() if reprice else None
    bnd = boundary_table() if pc_mode == "capture" else None
    results = []
    for wd in sorted(c["wins"], key=lambda w: w["ep"]):
        ep = wd["ep"]
        if eps is not None and ep not in eps:
            continue
        if exclude and ep in exclude:
            continue
        close = ep + 300
        t0 = close - HORIZON
        strike, final = wd["strike"], wd["final"]
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
        shares_tpl = {px: round(budget * frac / px, 2) for px in rungs}
        shares_tpl = {px: s for px, s in shares_tpl.items() if s >= MIN_SHARES}

        arms = []
        booked = None
        n = len(l_rx)
        i = 0
        while i < n and booked is None and len(arms) < max_arms:
            # ---- placement scan (consider_placement gate, main.py:1035-1065) ----
            place = None
            while i < n:
                rx = l_rx[i][0]
                k = close - rx
                if k > k_max:
                    i += 1
                    continue
                if k < k_min:
                    break
                if twap_frozen_at(trecs, l_rx, rx):
                    i += 1
                    continue
                pr = proj_at(l, l_rx, bz, rx, t0)
                if pr is None:
                    i += 1
                    continue
                disp = pr - strike
                m = margin(k, tab)
                r = abs(disp) / m if m > 0 else 0.0
                if m > 0 and r >= min_need:
                    side = "Up" if disp >= 0 else "Down"
                    if anti:
                        side = "Down" if side == "Up" else "Up"
                    placed = {px: sh for px, sh in shares_tpl.items() if needs[px] <= r}
                    place = (rx, k, r, side, placed)
                    break
                i += 1
            if place is None:
                break
            place_t, place_k, place_mult, side, placed = place
            # ---- cancel scan (maintain(), ticks strictly after placement, up to the close) ----
            cancel_t = why = None
            j = i + 1
            while j < n:
                rx = l_rx[j][0]
                if rx > close:
                    break
                k = close - rx
                pr = proj_at(l, l_rx, bz, rx, t0)
                if pr is None:
                    cancel_t, why = rx, "cold"
                    break
                signed = (pr - strike) if side == "Up" else (strike - pr)
                if anti:
                    signed = -signed
                if signed < min_need * margin(max(k, 0.01), tab):
                    cancel_t, why = rx, ("flip" if signed < 0 else "floor")
                    break
                j += 1
            wcap = None
            if cancel_t is None:
                if pc_mode == "label":
                    if winner != side:
                        cancel_t, why = close + WINNER_KNOWN_DELAY, "wrong-winner"
                    else:
                        cancel_t, why = close + POST_CLOSE_HOLD, "hold-expiry"
                else:
                    cancel_t, why, wcap = post_close_capture(bnd, ep, side)
                j = n
            # ---- H2 reprice cancel ----
            rp_ev = None
            if reprice:
                rp_ev = reprice_trigger(bbo, ep, side, place_t, cancel_t, reprice)
                if rp_ev is not None and rp_ev[0] + reprice.get("lat", 0.0) < cancel_t:
                    cancel_t, why = rp_ev[0] + reprice.get("lat", 0.0), "reprice"
                    j = bisect_left(l_rx, (cancel_t, -1.0))
            # ---- fills ----
            tok = wd["token_up"] if side == "Up" else wd["token_down"]
            filled, first_fill_t = fill_arm(c["prints"].get(tok, []), place_t, cancel_t, placed)
            tot = sum(filled.values())
            notional = sum(filled[px] * px for px in filled)
            arm = dict(side=side, place_t=place_t, place_k=close - place_t, place_mult=round(place_mult, 3),
                       cancel_t=cancel_t, cancel_k=close - cancel_t, why=why,
                       placed=sorted(placed, reverse=True),
                       rungs={px: f for px, f in filled.items() if f > 0}, filled=tot, notional=notional,
                       first_fill_k=(close - first_fill_t) if first_fill_t is not None else None,
                       reprice_ev=rp_ev, winner_cap=wcap)
            arms.append(arm)
            if tot > 0 and notional >= MIN_NOTIONAL_USD:
                booked = len(arms) - 1
                break
            if not rearm:
                break
            if why == "reprice" and reprice_latch:
                break
            if why in ("wrong-winner", "hold-expiry", "unverified"):
                break
            i = j   # re-arm scan resumes at the cancel tick (maintain() precedes placement in the loop)
        if not arms:
            continue
        a = arms[booked] if booked is not None else arms[-1]
        row = dict(ep=ep, et_day=et_day(ep), half=halves(ep), winner=winner, n_arms=len(arms), arms=arms,
                   booked_arm=booked, side=a["side"], why=a["why"], gap=abs(final - strike),
                   place_k=a["place_k"], place_mult=a["place_mult"], placed=a["placed"], rungs=a["rungs"],
                   first_arm_side=arms[0]["side"], first_arm_k=arms[0]["place_k"], first_arm_why=arms[0]["why"],
                   sides_armed=sorted({x["side"] for x in arms}))
        if booked is None:
            row.update(win=None, pnl=0.0, filled=0.0, vwap=None,
                       dropped_fill=any(x["filled"] > 0 for x in arms))
        else:
            tot = a["filled"]
            vwap = a["notional"] / tot
            win = a["side"] == winner
            row.update(win=win, filled=tot, vwap=vwap, dropped_fill=False,
                       pnl=(1.0 - vwap) * tot if win else -vwap * tot)
        results.append(row)
    return results


def summarize2(results, label="", quiet=False):
    """Per-window tallies (one row per armed window; N fills = windows with a booked fill)."""
    fills = [r for r in results if r["filled"] > 0]
    out = dict(label=label, armed=len(results), fills=len(fills),
               wins=sum(1 for r in fills if r["win"]), losses=sum(1 for r in fills if not r["win"]),
               pnl=round(sum(r["pnl"] for r in fills), 2),
               loss_pnl=round(sum(r["pnl"] for r in fills if not r["win"]), 2),
               win_pnl=round(sum(r["pnl"] for r in fills if r["win"]), 2),
               multi_arm=sum(1 for r in results if r["n_arms"] > 1),
               arms_total=sum(r["n_arms"] for r in results),
               fills_on_rearm=sum(1 for r in fills if r["booked_arm"] and r["booked_arm"] > 0),
               pnl_on_rearm=round(sum(r["pnl"] for r in fills if r["booked_arm"] and r["booked_arm"] > 0), 2),
               losses_on_rearm=sum(1 for r in fills if r["booked_arm"] and r["booked_arm"] > 0 and not r["win"]),
               sign_ok=sum(1 for r in results if r["side"] == r["winner"]),
               sh=round(sum(r["filled"] for r in fills), 2))
    out["ew_c_sh"] = round(100.0 * out["pnl"] / out["sh"], 2) if out["sh"] else None
    for h in ("H1", "H2"):
        fh = [r for r in fills if r["half"] == h]
        out[h] = dict(armed=sum(1 for r in results if r["half"] == h), fills=len(fh),
                      wins=sum(1 for r in fh if r["win"]), losses=sum(1 for r in fh if not r["win"]),
                      pnl=round(sum(r["pnl"] for r in fh), 2),
                      loss_pnl=round(sum(r["pnl"] for r in fh if not r["win"]), 2))
    out["loss_eps"] = [r["ep"] for r in fills if not r["win"]]
    if not quiet:
        print(f"{label:34s} armed {out['armed']:4d} fills {out['fills']:3d} wins {out['wins']:3d} "
              f"losses {out['losses']} pnl {out['pnl']:+9.2f} (loss$ {out['loss_pnl']:+.2f}) "
              f"multi-arm {out['multi_arm']:3d} fills-on-rearm {out['fills_on_rearm']} (${out['pnl_on_rearm']:+.2f}) "
              f"| H1 {out['H1']['fills']}f {out['H1']['pnl']:+.2f} | H2 {out['H2']['fills']}f {out['H2']['pnl']:+.2f}",
              flush=True)
    return out


def per_rung(results, rungs):
    """Per-rung tallies counted once per WINDOW (a window's booking arm may fill several rungs)."""
    st = {px: dict(placed_windows=0, fill_windows=0, win_windows=0, loss_windows=0, sh=0.0, dollars=0.0)
          for px in rungs}
    for r in results:
        for px in r["placed"]:
            st[px]["placed_windows"] += 1
        for px, sh in r["rungs"].items():
            s = st[px]
            s["fill_windows"] += 1
            s["win_windows"] += 1 if r["win"] else 0
            s["loss_windows"] += 0 if r["win"] else 1
            s["sh"] += sh
            s["dollars"] += sh * ((1 - px) if r["win"] else -px)
    for px, s in st.items():
        s["sh"] = round(s["sh"], 2)
        s["dollars"] = round(s["dollars"], 2)
    return st


def by_day(results):
    d = {}
    for r in results:
        d[r["et_day"]] = round(d.get(r["et_day"], 0.0) + r["pnl"], 2)
    return dict(sorted(d.items()))


def day_boot(day_vals, B=5000, seed=7):
    """Day-block bootstrap of a per-ET-day quantity: resample whole days with replacement.
    Returns total, 95% CI, one-sided p(sum <= 0), p(sum >= 0), number of unique day blocks."""
    rng = np.random.default_rng(seed)
    v = np.array(list(day_vals.values()), dtype=float)
    n = len(v)
    if n == 0:
        return dict(n_days=0)
    idx = rng.integers(0, n, size=(B, n))
    sums = v[idx].sum(axis=1)
    return dict(n_days=n, B=B, total=round(float(v.sum()), 2),
                ci95=[round(float(np.percentile(sums, 2.5)), 2), round(float(np.percentile(sums, 97.5)), 2)],
                p_le0=round(float((sums <= 0).mean()), 4), p_ge0=round(float((sums >= 0).mean()), 4))


if __name__ == "__main__":
    MODES.get(sys.argv[1] if len(sys.argv) > 1 else "", main)()
