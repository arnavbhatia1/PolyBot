"""H1 pass 1: stream the era tape into per-window cell sufficient statistics.

Per print, attribution is by TOKEN -> (window, up/down). Cell key per window:
(k_band, price_band, taker_side, token_is_up). We store sum(size) and
sum(price*size) per cell so maker P&L under ANY outcome assignment (real or
shuffled) is recomputable without re-reading the tape:
  BUY  print (taker bought, maker sold):  maker $ = sum(p*s) - v*sum(s)
  SELL print (taker sold, maker bought):  maker $ = v*sum(s) - sum(p*s)
where v = 1 if the token won else 0.

Outputs (scripts/research/data/vps-0821/):
  h1_cellstats.pkl   {win_ts: {cellkey: [sum_s, sum_ps, n]}} + labels + coverage
"""
import gzip
import json
import pickle
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
DAYS = [f"2026-08-{d:02d}" for d in range(14, 22)]

K_BANDS = ["pre", "k300-60", "k60-25", "k25-6", "k6-0",
           "post0-30", "post30-150", "post150+"]


def k_band(ts: float, wts: int) -> str:
    k = (wts + 300) - ts
    if ts < wts:
        return "pre"
    if k > 60:
        return "k300-60"
    if k > 25:
        return "k60-25"
    if k > 6:
        return "k25-6"
    if k > 0:
        return "k6-0"
    if k > -30:
        return "post0-30"
    if k > -150:
        return "post30-150"
    return "post150+"


def main():
    tm = json.load(open(DATA / "token_map.json"))["map"]
    tok2win = {}
    for wts_s, d in tm.items():
        wts = int(wts_s)
        tok2win[d["up"]] = (wts, 1)
        tok2win[d["down"]] = (wts, 0)

    con = sqlite3.connect(f"file:{DATA / 'polybot_paper_0821.db'}?mode=ro", uri=True)
    labels = {}
    for wid, ru in con.execute(
            "SELECT window_id, resolved_up FROM window_labels "
            "WHERE window_id LIKE 'btc-updown-5m-%'"):
        labels[int(wid.rsplit("-", 1)[1])] = int(ru)
    con.close()

    cells = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    win_span = {}                      # win_ts -> [min_ts, max_ts, n_prints]
    fee_hist = Counter()
    unmapped = Counter()               # day -> n prints on tokens outside the map
    unmapped_usd = Counter()
    day_prints = Counter()
    gaps = []                          # (prev_ts, ts) tape holes > 120s inside a file
    t0 = time.time()
    n_total = 0

    for day in DAYS:
        gz = DATA / f"tape_{day}.jsonl.gz"
        pl = DATA / f"tape_{day}.jsonl"
        path = gz if gz.exists() else pl
        opener = (lambda p: gzip.open(p, "rt", encoding="utf-8")) if path.suffix == ".gz" \
            else (lambda p: open(p, "r", encoding="utf-8"))
        prev_ts = None
        with opener(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                n_total += 1
                ts = float(r["ts"])
                if prev_ts is not None and ts - prev_ts > 120:
                    gaps.append((prev_ts, ts))
                prev_ts = ts
                price = float(r["price"])
                size = float(r["size"])
                fee_hist[r.get("fee_bps", "?")] += 1
                w = tok2win.get(r["token"])
                if w is None:
                    unmapped[day] += 1
                    unmapped_usd[day] += price * size
                    continue
                wts, is_up = w
                kb = k_band(ts, wts)
                pb = min(int(price * 10), 9)
                key = (kb, pb, r["side"], is_up)
                c = cells[wts][key]
                c[0] += size
                c[1] += price * size
                c[2] += 1
                day_prints[day] += 1
                sp = win_span.get(wts)
                if sp is None:
                    win_span[wts] = [ts, ts, 1]
                else:
                    sp[0] = min(sp[0], ts)
                    sp[1] = max(sp[1], ts)
                    sp[2] += 1

    out = {
        "cells": {w: {k: list(v) for k, v in d.items()} for w, d in cells.items()},
        "labels": labels,
        "win_span": win_span,
        "fee_hist": dict(fee_hist),
        "unmapped": dict(unmapped),
        "unmapped_usd": dict(unmapped_usd),
        "day_prints": dict(day_prints),
        "gaps": gaps,
        "n_total": n_total,
    }
    with open(DATA / "h1_cellstats.pkl", "wb") as f:
        pickle.dump(out, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"prints={n_total} mapped_windows={len(cells)} "
          f"unmapped={sum(unmapped.values())} ({sum(unmapped_usd.values()):.0f} USD notional) "
          f"gaps>120s={len(gaps)} elapsed={time.time()-t0:.0f}s")
    print("fee_bps histogram:", dict(fee_hist))


if __name__ == "__main__":
    main()
