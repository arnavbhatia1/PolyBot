"""WS3.2/3.3: behavioral fingerprints of the top wallets, era-split.

Per wallet x era (30s: 08-07..13 / 60s: 08-14+), BUY fills only:
  timing (k at fill: median/deciles, share k>60 / 60..25 / 25..6 / 6..0 / post),
  price bands, windows/day, one-sidedness, window-gap selection,
  sign-match vs OUR projection at fill time (era-appropriate horizon: the
  plain projection the era's engine could compute), win-rate when
  agreeing/disagreeing.
"""
import gzip
import json
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
RULE60 = 1786665600
SPOT_STALE_S = 3.0


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


def main():
    wins = {}
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wd = json.loads(line)
            wins[wd["ep"]] = wd
    fills = json.load(open(DATA / "top_wallet_fills.json"))
    census = {o["wallet"]: o for o in json.load(open(DATA / "wallet_census_top.json"))}

    def proj_sign(ep, ts):
        """Plain-projection sign at fill ts under the era's horizon; None if
        outside zone / cold."""
        wd = wins.get(ep)
        if wd is None or not wd["strike"]:
            return None
        close = ep + 300
        hor = 60.0 if ep >= RULE60 else 30.0
        t0 = close - hor
        t = min(ts, close)          # post-close fills judge on the close value
        if t <= t0:
            return None
        l_rx = sorted((rx, p) for rx, _ts, p in wd["l"])
        i = bisect_right(l_rx, (t, float("inf"))) - 1
        if i < 0 or t - l_rx[i][0] > SPOT_STALE_S:
            return None
        A = running_avg(l_rx, t0, t)
        if A is None:
            return None
        w = (t - t0) / hor
        proj = w * A + (1 - w) * l_rx[i][1]
        return "Up" if proj - wd["strike"] >= 0 else "Down"

    agg = defaultdict(lambda: defaultdict(lambda: dict(
        n=0, k=[], px=[], win=0, agree=0, agree_win=0, dis=0, dis_win=0,
        nosign=0, nosign_win=0, wnd=defaultdict(set), gap=[], post=0, vol=0.0)))
    for r in fills:
        if r["side"] != "BUY":
            continue
        ep = r["ep"]
        wd = wins.get(ep)
        if wd is None:
            continue
        era = "60s" if ep >= RULE60 else "30s"
        try:
            ts = int(r["ts"])
            px = float(r["px"])
            sz = float(r["sz"])
        except (TypeError, ValueError):
            continue
        a = agg[r["w"]][era]
        k = ep + 300 - ts
        a["n"] += 1
        a["k"].append(k)
        a["px"].append(px)
        a["vol"] += px * sz
        if k < 0:
            a["post"] += 1
        winner = "Up" if wd["up"] else "Down"
        won = r["out"] == winner
        a["win"] += won
        a["wnd"][ep].add(r["out"])
        a["gap"].append(abs(wd["final"] - wd["strike"]))
        s = proj_sign(ep, ts)
        if s is None:
            a["nosign"] += 1
            a["nosign_win"] += won
        elif s == r["out"]:
            a["agree"] += 1
            a["agree_win"] += won
        else:
            a["dis"] += 1
            a["dis_win"] += won

    def q(xs, f):
        xs = sorted(xs)
        return xs[min(int(f * len(xs)), len(xs) - 1)] if xs else float("nan")

    for wal, eras in agg.items():
        c = census.get(wal, {})
        print(f"\n===== {wal[:14]} ({c.get('name', '?')})  pnl {c.get('pnl')} "
              f"(pre {c.get('pnl_pre')} / post {c.get('pnl_post')}) =====")
        for era in ("30s", "60s"):
            a = eras.get(era)
            if not a or a["n"] == 0:
                continue
            one_sided = sum(1 for s in a["wnd"].values() if len(s) == 1)
            kk = a["k"]
            in_zone = a["agree"] + a["dis"]
            print(f"  {era}: {a['n']:6d} buys  ${a['vol']:9.0f}  win {a['win'] / a['n']:.0%}  "
                  f"px q25/50/75 {q(a['px'], .25):.2f}/{q(a['px'], .5):.2f}/{q(a['px'], .75):.2f}")
            print(f"       k q10/50/90: {q(kk, .1):6.0f}/{q(kk, .5):6.0f}/{q(kk, .9):6.0f}s  "
                  f"post-close {a['post'] / a['n']:.0%}  one-sided {one_sided}/{len(a['wnd'])}  "
                  f"gap-med ${q(a['gap'], .5):.1f}")
            if in_zone:
                print(f"       sign-match {a['agree'] / in_zone:.0%} of {in_zone} in-zone buys  "
                      f"(win agree {a['agree_win'] / max(1, a['agree']):.0%} / "
                      f"disagree {a['dis_win'] / max(1, a['dis']):.0%})  "
                      f"outside-zone {a['nosign']} (win {a['nosign_win'] / max(1, a['nosign']):.0%})")


if __name__ == "__main__":
    main()
