"""Deep-sell supply watch (the pre-registered kill-line input).

Per ET day: winner-token taker-SELL flow in the ladder's resting span
[close-25, close+60] at px <= 0.80 (value ceded = sum sz*(1-px)), windows with
any such flow, windows printing < 0.50 / < 0.20, tape coverage (windows with
any print). Trailing-7-day mean of ceded value vs the $450/day kill line from
docs/research/proposal_floor_redecision_2026-09-11.md. Earlier days come from
r7_supply_by_day.json; new days are scanned from tape + window_labels.
Usage: python r23_supply_watch.py 2026-09-01 2026-09-02 2026-09-03
"""
import gzip
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
D = SP / "data" / "vps-0831"
REC = SP.resolve().parents[1] / "polybot" / "memory" / "recordings"
KILL_LINE = 450.0
DAYS = sys.argv[1:]


def et_day(ep):
    return datetime.fromtimestamp(ep - 4 * 3600, tz=timezone.utc).strftime("%m-%d")


con = sqlite3.connect(f"file:{SP / 'data' / 'polybot_paper.db'}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
tok = {}
n_labels = defaultdict(int)
for r in con.execute("SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'"):
    ep = int(r["window_id"].rsplit("-", 1)[1])
    if r["final_price"] is None or r["resolved_up"] is None:
        continue
    wu = bool(r["resolved_up"])
    if r["token_up"]:
        tok[r["token_up"]] = (ep, wu)
    if r["token_down"]:
        tok[r["token_down"]] = (ep, not wu)
    n_labels[et_day(ep)] += 1
con.close()

day = defaultdict(lambda: dict(sh=0.0, val=0.0, w_any=set(), w_50=set(), w_20=set(), w_print=set()))
for dy in DAYS:
    p = REC / f"tape_{dy}.jsonl.gz"
    if not p.exists():
        p = REC / f"tape_{dy}.jsonl"
    opener = (lambda q: gzip.open(q, "rt", encoding="utf-8")) if p.suffix == ".gz" \
        else (lambda q: open(q, encoding="utf-8"))
    with opener(p) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = tok.get(r.get("token"))
            if t is None:
                continue
            ep, is_winner = t
            try:
                ts, px, sz = float(r["ts"]), float(r["price"]), float(r["size"])
            except (TypeError, ValueError):
                continue
            d = day[et_day(ep)]
            d["w_print"].add(ep)
            close = ep + 300
            if not (close - 25 <= ts <= close + 60):
                continue
            if is_winner and r.get("side") == "SELL" and px <= 0.80 + 1e-9:
                d["sh"] += sz
                d["val"] += sz * (1 - px)
                d["w_any"].add(ep)
                if px < 0.50:
                    d["w_50"].add(ep)
                if px < 0.20:
                    d["w_20"].add(ep)

prev = json.load(open(D / "r7_supply_by_day.json"))
hist = {k: v["val"] for k, v in prev.items()}
print(f"{'ET day':>6} {'labels':>6} {'w/prints':>8} {'deep sh':>8} {'ceded $':>8} {'w/any':>5} {'w<.50':>5} {'w<.20':>5}")
# An ET day is fully covered only when the tapes for BOTH of its UTC days were
# scanned (ET day = UTC day 04:00 -> next UTC day 04:00); partial days keep
# the earlier full-day value and are shown with a marker.
utc_days = set(DAYS)
for dy in sorted(day):
    d = day[dy]
    mm, dd = dy.split("-")
    this_utc = f"2026-{mm}-{dd}"
    nxt = datetime(2026, int(mm), int(dd)).timestamp() + 86400
    next_utc = datetime.fromtimestamp(nxt).strftime("%Y-%m-%d")
    complete = this_utc in utc_days and next_utc in utc_days
    if complete or dy not in hist:
        hist[dy] = d["val"]
    else:
        print(f"   ({dy}: partial scan, keeping recorded ${hist[dy]:.0f})")
    print(f"{dy:>6} {n_labels[dy]:6d} {len(d['w_print']):8d} {d['sh']:8.0f} {d['val']:8.2f} "
          f"{len(d['w_any']):5d} {len(d['w_50']):5d} {len(d['w_20']):5d}")
days_sorted = sorted(hist)
last7 = days_sorted[-7:]
mean7 = sum(hist[k] for k in last7) / len(last7)
print(f"\ntrailing-7-day mean ceded value ({last7[0]}..{last7[-1]}): ${mean7:.0f}/day "
      f"vs kill line ${KILL_LINE:.0f}/day -> {'BELOW — escalation clause met' if mean7 < KILL_LINE else 'above'}")
print("per-day history:", {k: round(hist[k]) for k in days_sorted[-10:]})
json.dump({dy: {k: (sorted(v) if isinstance(v, set) else v) for k, v in d.items()}
           for dy, d in day.items()}, open(D / "r23_supply_watch.json", "w"), indent=1)
