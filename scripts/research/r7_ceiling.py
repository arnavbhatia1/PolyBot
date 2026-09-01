"""R7 (08-31 charter §2.2): ceiling + supply trend for the winner-side deep-bid seat.

Part 1 — ORACLE CEILING. Engine-true fill physics (ws2 conventions: strictly-below
print-through, 135-sh at-price credit, GTC latencies), oracle sign (winner known),
rest from the first raw tick with k in [6, 25] to close + the engine's 60s
post-close hold, no cancels. This is "every projection-side sweep captured at
every rung": no ladder variant under the same fill physics beats it. Two fill
conventions: 'paper' = deployed rule (each rung credits independently — what the
paper validator would score); 'volume' = price-priority, volume-conserving (each
sold share fills at most one rung — the physical ceiling). Budget swept to show
finite-flow decay.

Part 2 — SUPPLY TREND (§2.1 sweep question). Per-day winner-token taker-SELL
flow in the resting span [close-25, close+60]: shares, ceded value sum(sz*(1-px)),
band splits, sweep-window counts; plus the complement-cross bound (loser-token
taker-BUY at q >= 0.20 == winner-side bid flow at 1-q via the mint adapter,
h3: unconfirmed, upper bound). Splits: 08-14..20 / 08-21..27 / 08-28..30.
"""
import gzip
import importlib.util
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
OUT = DATA / "vps-0831"

spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)

RUNGS = lr.RUNGS
K_MIN, K_MAX = 6.0, 25.0
BUDGETS = [15.0, 30.0, 60.0, 90.0, 150.0, 300.0, 500.0, 1000.0, 2000.0]


def et_day(ep):
    return datetime.fromtimestamp(ep - 4 * 3600, tz=timezone.utc).strftime("%m-%d")


def oracle_run(budget, rule="volume", rungs=None, k_max=K_MAX, k_min=K_MIN):
    """One row per window that has a placement tick; always on the winner side."""
    rungs = rungs or RUNGS
    frac = 1.0 / len(rungs)
    c = lr.load_corpus()
    rows = []
    for wd in sorted(c["wins"], key=lambda w: w["ep"]):
        ep = wd["ep"]
        close = ep + 300
        strike, final = wd["strike"], wd["final"]
        if not strike or not final:
            continue
        l_rx = [(rx, p) for rx, _ts, p in sorted(wd["l"])]
        place_t = None
        for rx, _p in l_rx:
            k = close - rx
            if k_min <= k <= k_max:
                place_t = rx
                break
        if place_t is None:
            continue
        winner = "Up" if wd["up"] else "Down"
        cancel_t = close + lr.POST_CLOSE_HOLD
        tok = wd["token_up"] if winner == "Up" else wd["token_down"]
        shares = {px: round(budget * frac / px, 2) for px in rungs}
        shares = {px: s for px, s in shares.items() if s >= lr.MIN_SHARES}
        filled = {px: 0.0 for px in shares}
        at_vol = {px: 0.0 for px in shares}
        desc = sorted(shares, reverse=True)
        for ts, px, sz in c["prints"].get(tok, []):
            if ts < place_t + lr.PLACE_LAT or ts > cancel_t + lr.CANCEL_LAT:
                continue
            if rule == "paper":
                for rp in shares:
                    if px < rp - 1e-9:
                        filled[rp] = shares[rp]
                    elif abs(px - rp) <= 1e-9:
                        at_vol[rp] += sz
                        credit = min(shares[rp], max(0.0, at_vol[rp] - lr.AT_PRICE_QUEUE_SH))
                        filled[rp] = max(filled[rp], credit)
            else:  # volume-conserving, price priority: highest eligible rung first
                remaining = sz
                for rp in desc:
                    if remaining <= 0:
                        break
                    if px < rp - 1e-9:
                        take = min(remaining, shares[rp] - filled[rp])
                        filled[rp] += take
                        remaining -= take
                    elif abs(px - rp) <= 1e-9:
                        at_vol[rp] += remaining
                        credit = min(shares[rp], max(0.0, at_vol[rp] - lr.AT_PRICE_QUEUE_SH))
                        if credit > filled[rp]:
                            remaining -= credit - filled[rp]
                            filled[rp] = credit
        tot = sum(filled.values())
        pnl = sum(filled[rp] * (1 - rp) for rp in filled)   # oracle: always wins
        rows.append(dict(ep=ep, day=et_day(ep), filled=tot, pnl=pnl,
                         rungs={rp: filled[rp] for rp in filled if filled[rp] > 0}))
    return rows


def part1():
    c = lr.load_corpus()
    days = sorted({et_day(w["ep"]) for w in c["wins"]})
    n_days = len(days)
    print(f"=== PART 1: oracle ceiling ({len(c['wins'])} windows, {n_days} ET days "
          f"{days[0]}..{days[-1]}) ===")
    print(f"{'budget':>7} {'rule':>7} {'windows-filled':>14} {'fills/day':>9} "
          f"{'$total':>10} {'$/day':>8} {'$/day/$100bgt':>13}")
    curves = {}
    for rule in ("volume", "paper"):
        for b in BUDGETS:
            rows = oracle_run(b, rule=rule)
            f = [r for r in rows if r["filled"] > 0]
            pnl = sum(r["pnl"] for r in f)
            curves.setdefault(rule, []).append(dict(budget=b, windows=len(f),
                                                    pnl=round(pnl, 2)))
            print(f"{b:7.0f} {rule:>7} {len(f):14d} {len(f) / n_days:9.2f} "
                  f"{pnl:10.2f} {pnl / n_days:8.2f} {100 * pnl / n_days / b:13.3f}")
    # per-rung detail at $60 volume rule + per-day
    rows = oracle_run(60.0, rule="volume")
    st = defaultdict(lambda: [0, 0.0, 0.0])
    per_day = defaultdict(float)
    for r in rows:
        per_day[r["day"]] += r["pnl"]
        for rp, sh in r["rungs"].items():
            st[rp][0] += 1
            st[rp][1] += sh
            st[rp][2] += sh * (1 - rp)
    print("\n$60 volume-rule per rung: rung fills sh $  (oracle)")
    for rp in sorted(st, reverse=True):
        n, sh, d = st[rp]
        print(f"  {rp:.2f}: {n:4d} {sh:9.1f} {d:+9.2f}")
    print("\n$60 oracle $/ET-day:", json.dumps({d: round(v, 2)
          for d, v in sorted(per_day.items())}))
    json.dump(curves, open(OUT / "r7_ceiling_curves.json", "w"), indent=1)


def part2():
    c = lr.load_corpus()
    tok_side = {}
    for wd in c["wins"]:
        w = "Up" if wd["up"] else "Down"
        tok_side[wd["token_up"]] = (wd["ep"], w == "Up")
        tok_side[wd["token_down"]] = (wd["ep"], w == "Down")
    day_stat = defaultdict(lambda: dict(sh=0.0, val=0.0, cc_sh=0.0, cc_val=0.0,
                                        w_any=set(), w_50=set(), w_20=set(),
                                        windows=set()))
    bands = defaultdict(float)
    for wd in c["wins"]:
        day_stat[et_day(wd["ep"])]["windows"].add(wd["ep"])
    for tok, (ep, is_winner_tok) in tok_side.items():
        close = ep + 300
        d = day_stat[et_day(ep)]
        for ts, px, sz in c["prints"].get(tok, []):
            if ts < close - K_MAX or ts > close + lr.POST_CLOSE_HOLD:
                continue
            # own-token winner-side SELL flow (what paper credits)
            # side isn't in load_corpus prints; approximate: SELL flow == prints
            # below the prior tick? NO - use raw tape reload instead (main()).
            pass
    print("part2 runs from raw tape (needs side field) — see part2_tape()")


def part2_tape():
    REC = Path(__file__).resolve().parents[2] / "polybot" / "memory" / "recordings"
    DAYS = [f"2026-08-{d:02d}" for d in range(14, 31)]
    con = sqlite3.connect(f"file:{DATA / 'polybot_paper.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tok_info = {}
    for r in con.execute("SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'"):
        ep = int(r["window_id"].rsplit("-", 1)[1])
        if ep < lr.RULE_TS or r["final_price"] is None:
            continue
        wu = bool(r["resolved_up"])
        if r["token_up"]:
            tok_info[r["token_up"]] = (ep, wu)
        if r["token_down"]:
            tok_info[r["token_down"]] = (ep, not wu)
    con.close()
    day = defaultdict(lambda: dict(sh=0.0, val=0.0, cc_sh=0.0, cc_val=0.0,
                                   w_any=set(), w_50=set(), w_20=set()))
    band_val = defaultdict(float)
    for dy in DAYS:
        p = REC / f"tape_{dy}.jsonl.gz"
        if not p.exists():
            p = REC / f"tape_{dy}.jsonl"
        if not p.exists():
            continue
        opener = (lambda q: gzip.open(q, "rt", encoding="utf-8")) if p.suffix == ".gz" \
            else (lambda q: open(q, encoding="utf-8"))
        with opener(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ti = tok_info.get(r["token"])
                if ti is None:
                    continue
                ep, is_winner_tok = ti
                close = ep + 300
                try:
                    ts, px, sz = float(r["ts"]), float(r["price"]), float(r["size"])
                except (TypeError, ValueError):
                    continue
                if ts < close - K_MAX or ts > close + lr.POST_CLOSE_HOLD:
                    continue
                d = day[et_day(ep)]
                if is_winner_tok and r["side"] == "SELL" and px <= 0.80 + 1e-9:
                    d["sh"] += sz
                    d["val"] += sz * (1 - px)
                    band_val[min(int(px * 10), 9)] += sz * (1 - px)
                    d["w_any"].add(ep)
                    if px < 0.50:
                        d["w_50"].add(ep)
                    if px < 0.20:
                        d["w_20"].add(ep)
                elif (not is_winner_tok) and r["side"] == "BUY" and px >= 0.20 - 1e-9:
                    d["cc_sh"] += sz
                    d["cc_val"] += sz * px   # winner-side value = 1-(1-px) ... = px
    print("\n=== PART 2: winner-side sell flow in the resting span "
          "[close-25, close+60], px<=0.80 ===")
    print(f"{'ET day':>6} {'sh':>9} {'$value':>9} {'w/any':>6} {'w/<.50':>6} "
          f"{'w/<.20':>6} {'cc_sh':>9} {'cc_$':>9}")
    for dy in sorted(day):
        d = day[dy]
        print(f"{dy:>6} {d['sh']:9.0f} {d['val']:9.2f} {len(d['w_any']):6d} "
              f"{len(d['w_50']):6d} {len(d['w_20']):6d} {d['cc_sh']:9.0f} "
              f"{d['cc_val']:9.2f}")
    spans = {"08-14..20": ("08-14", "08-20"), "08-21..27": ("08-21", "08-27"),
             "08-28..30": ("08-28", "08-30")}
    print("\nspan means ($value/day, windows-with-flow/day):")
    for name, (a, b) in spans.items():
        ds = [d for dy, d in day.items() if a <= dy <= b]
        if not ds:
            continue
        print(f"  {name}: {sum(x['val'] for x in ds) / len(ds):8.2f} $/day  "
              f"any {sum(len(x['w_any']) for x in ds) / len(ds):5.1f}/day  "
              f"<.50 {sum(len(x['w_50']) for x in ds) / len(ds):4.1f}/day  "
              f"<.20 {sum(len(x['w_20']) for x in ds) / len(ds):4.1f}/day  "
              f"cc {sum(x['cc_val'] for x in ds) / len(ds):7.2f} $/day")
    print("\nceded value by price decile (era total):",
          {f"0.{b}x": round(v, 2) for b, v in sorted(band_val.items())})
    json.dump({dy: {k: (sorted(v) if isinstance(v, set) else v)
                    for k, v in d.items()} for dy, d in day.items()},
              open(OUT / "r7_supply_by_day.json", "w"), indent=1, default=list)


if __name__ == "__main__":
    part1()
    part2_tape()
