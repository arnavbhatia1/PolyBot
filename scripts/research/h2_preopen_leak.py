"""H2 secondary: does N+1's book already price the incoming strike PRE-open?

Micro-tape "b" rows record every subscribed token's BBO change while global
time mod 300 >= 210 (each window's final 90s) — so N+1's tokens, when already
subscribed, have event-true pre-open quotes. For k in {5,15,30}s before open:
    d_pre = chainlink spot(open-k) - price_to_beat(N+1)
(ptb is ground truth here; live we'd know it to ~$0.03 via the running avg.)
Buy the favored side at N+1's standing pre-open ask; score against the label.

"b" rows carry NO size — $5 FOK executability is NOT verifiable from them.
The tape check below counts actual pre-open prints to establish whether the
CLOB matches orders pre-open at all.

Usage: python scripts/research/h2_preopen_leak.py
Writes: scripts/research/data/vps-0821/h2_preopen_results.json
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data" / "vps-0821"
ERA_TS = 1786665600
ET_OFFSET = 4 * 3600
KS = [5, 15, 30]
FEE_RATE = 0.07
ABS_EDGES = [0.0, 2.0, 5.0, 10.0, 20.0, 50.0, float("inf")]


def et_day(ts: float) -> int:
    return int((ts - ET_OFFSET) // 86400)


def abs_bucket(d: float) -> int:
    a = abs(d)
    for i in range(len(ABS_EDGES) - 1):
        if ABS_EDGES[i] <= a < ABS_EDGES[i + 1]:
            return i
    return len(ABS_EDGES) - 2


def fee_per_share(p: float) -> float:
    return FEE_RATE * p * (1.0 - p)


def load_labels() -> dict[int, tuple[int, float]]:
    con = sqlite3.connect(f"file:{DATA / 'polybot_paper_0821.db'}?mode=ro", uri=True)
    out = {}
    for wid, up, ptb in con.execute(
            "SELECT window_id, resolved_up, price_to_beat FROM window_labels"):
        ts = int(wid.rsplit("-", 1)[1])
        if ts >= ERA_TS and ptb is not None:
            out[ts] = (up, ptb)
    con.close()
    return out


def load_spots() -> dict[int, dict[int, float]]:
    """window N open_ts -> k -> chainlink spot at N_close - k (fresh only)."""
    con = sqlite3.connect(f"file:{DATA / 'window_paths_60s.db'}?mode=ro", uri=True)
    best: dict[int, dict[int, tuple[float, float]]] = defaultdict(dict)
    q = ("SELECT window_id, elapsed_s, chainlink_price, chainlink_age_s "
         "FROM window_paths WHERE elapsed_s >= 268 AND chainlink_price IS NOT NULL "
         "AND chainlink_age_s <= 3.0")
    for wid, el, cl, _age in con.execute(q):
        ts = int(wid.rsplit("-", 1)[1])
        for k in KS:
            target = 300.0 - k
            gap = abs(el - target)
            if gap <= 0.6:
                cur = best[ts].get(k)
                if cur is None or gap < cur[0]:
                    best[ts][k] = (gap, cl)
    con.close()
    return {ts: {k: v[1] for k, v in per.items()} for ts, per in best.items()}


def main() -> None:
    labels = load_labels()
    tm = json.load(open(DATA / "token_map.json"))["map"]
    tok2win: dict[str, tuple[int, str]] = {}
    for ts_s, d in tm.items():
        ts = int(ts_s)
        if ts >= ERA_TS:
            if d.get("up"):
                tok2win[d["up"]] = (ts, "up")
            if d.get("down"):
                tok2win[d["down"]] = (ts, "down")

    # --- scan micro files: standing pre-open quote per (window, side, k) ---
    # keep the latest b row with ts <= open - k (ts >= open - 90).
    # String-sliced field extraction — full json.loads on ~90M rows is too slow.
    quotes: dict[tuple[int, str, int], tuple[float, float, float]] = {}
    have_preopen_rows: set[int] = set()
    files = sorted(DATA.glob("micro_2026-08-*.jsonl")) + \
        sorted(DATA.glob("micro_2026-08-*.jsonl.gz"))
    n_b = 0
    TOK = '"token": "'
    TS = '"ts": '
    BID = '"bid": "'
    ASK = '"ask": "'
    for fp in files:
        op = gzip.open if fp.suffix == ".gz" else open
        with op(fp, "rt") as f:
            for line in f:
                if '"b"' not in line[:12]:
                    continue
                i = line.find(TOK)
                if i < 0:
                    continue
                j = line.find('"', i + len(TOK))
                w = tok2win.get(line[i + len(TOK):j])
                if w is None:
                    continue
                i = line.find(TS)
                if i < 0:
                    continue
                j = line.find(",", i)
                try:
                    ts = float(line[i + len(TS):j])
                except ValueError:
                    continue
                open_ts, side = w
                if not (open_ts - 90 <= ts < open_ts):
                    continue
                n_b += 1
                have_preopen_rows.add(open_ts)
                try:
                    i = line.find(BID)
                    j = line.find('"', i + len(BID))
                    bid = float(line[i + len(BID):j])
                    i = line.find(ASK)
                    j = line.find('"', i + len(ASK))
                    ask = float(line[i + len(ASK):j])
                except ValueError:
                    continue
                for k in KS:
                    if ts <= open_ts - k:
                        key = (open_ts, side, k)
                        cur = quotes.get(key)
                        if cur is None or ts > cur[0]:
                            quotes[key] = (ts, bid, ask)

    # --- tape check: does the CLOB match orders pre-open? ---
    pre_prints = 0
    pre_print_windows = set()
    pre_shares = 0.0
    tfiles = sorted(DATA.glob("tape_2026-08-*.jsonl")) + \
        sorted(DATA.glob("tape_2026-08-*.jsonl.gz"))
    for fp in tfiles:
        op = gzip.open if fp.suffix == ".gz" else open
        with op(fp, "rt") as f:
            for line in f:
                i = line.find(TOK)
                if i < 0:
                    continue
                j = line.find('"', i + len(TOK))
                w = tok2win.get(line[i + len(TOK):j])
                if w is None:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                open_ts, _side = w
                ets = float(r["ets"]) / 1000.0 if r.get("ets") else r["ts"]
                if open_ts - 90 <= ets < open_ts:
                    pre_prints += 1
                    pre_print_windows.add(open_ts)
                    try:
                        pre_shares += float(r["size"])
                    except (ValueError, TypeError, KeyError):
                        pass

    spots = load_spots()

    # score half = same alternating ET days as the primary (odd index)
    all_days = sorted({et_day(ts) for ts in labels})
    score_days = set(all_days[1::2])

    results = {"n_era_labeled": len(labels),
               "n_windows_with_preopen_b_rows": len(have_preopen_rows),
               "n_preopen_b_rows": n_b,
               "tape_preopen": {"prints": pre_prints,
                                "windows_with_prints": len(pre_print_windows),
                                "shares": round(pre_shares, 1)},
               "per_k": {}}

    for k in KS:
        stats_all = {"n": 0, "ew_sum": 0.0, "wins": 0}
        stats_score = {"n": 0, "ew_sum": 0.0, "wins": 0}
        anti = {"n": 0, "ew_sum": 0.0, "wins": 0}
        per_abs = defaultdict(lambda: {"n": 0, "ew_sum": 0.0, "wins": 0})
        per_day = defaultdict(lambda: {"n": 0, "ew_sum": 0.0})
        excl = defaultdict(int)
        for open_ts, (up, ptb) in labels.items():
            prev = open_ts - 300
            sp = spots.get(prev, {}).get(k)
            if sp is None:
                excl["no_fresh_spot"] += 1
                continue
            d = sp - ptb
            fav = "up" if d >= 0 else "down"
            q = quotes.get((open_ts, fav, k))
            if q is None:
                excl["no_preopen_quote"] += 1
                continue
            _ts, _bid, ask = q
            if not (0.0 < ask < 1.0):
                excl["ask_degenerate"] += 1
                continue
            won = bool(up) == (fav == "up")
            net = ((1.0 - ask) if won else -ask) - fee_per_share(ask)
            stats_all["n"] += 1
            stats_all["ew_sum"] += net
            stats_all["wins"] += 1 if won else 0
            day = et_day(open_ts)
            if day in score_days:
                stats_score["n"] += 1
                stats_score["ew_sum"] += net
                stats_score["wins"] += 1 if won else 0
                ab = abs_bucket(d)
                per_abs[ab]["n"] += 1
                per_abs[ab]["ew_sum"] += net
                per_abs[ab]["wins"] += 1 if won else 0
                per_day[day]["n"] += 1
                per_day[day]["ew_sum"] += net
                aq = quotes.get((open_ts, "down" if fav == "up" else "up", k))
                if aq is not None and 0.0 < aq[2] < 1.0:
                    awon = not won
                    anet = ((1.0 - aq[2]) if awon else -aq[2]) - fee_per_share(aq[2])
                    anti["n"] += 1
                    anti["ew_sum"] += anet
                    anti["wins"] += 1 if awon else 0

        def summ(s):
            return {"n": s["n"],
                    "ew_cents": round(100 * s["ew_sum"] / s["n"], 2) if s["n"] else None,
                    "win_rate": round(s["wins"] / s["n"], 3) if s["n"] else None}

        results["per_k"][k] = {
            "all": summ(stats_all),
            "score_half": summ(stats_score),
            "anti_side_score_half": summ(anti),
            "per_abs_bucket_score_half": {
                f"[{ABS_EDGES[i]},{ABS_EDGES[i+1]})": summ(per_abs[i])
                for i in range(len(ABS_EDGES) - 1) if per_abs[i]["n"]},
            "per_day_score_half": {
                str(d): {"n": v["n"],
                         "ew_cents": round(100 * v["ew_sum"] / v["n"], 2)}
                for d, v in sorted(per_day.items())},
            "exclusions": dict(excl),
        }

    out = DATA / "h2_preopen_results.json"
    out.write_text(json.dumps(results, indent=1))
    print(f"wrote {out}")
    print(f"windows with pre-open b rows: {len(have_preopen_rows)} / {len(labels)}")
    print(f"tape pre-open prints: {pre_prints} in {len(pre_print_windows)} windows, "
          f"{pre_shares:.0f} shares")
    for k in KS:
        r = results["per_k"][k]
        print(f"k={k:>2}s all={r['all']} score={r['score_half']} "
              f"anti={r['anti_side_score_half']}")


if __name__ == "__main__":
    main()
