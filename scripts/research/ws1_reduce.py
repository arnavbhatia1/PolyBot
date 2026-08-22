"""Pass 1: stream TWAP-era micro-tape once -> compact per-window streams.

Per labeled window ep (epoch, close = ep+300), collects records with time in
[ep+215, ep+312] (enough for the 45s raw ring seeding running_avg at k=30 and
the 10s Binance ring):
  l  raw Chainlink reports  [rx, ts, p]
  bz Binance relay ticks    [rx, ts, p]     (exists 08-15+)
  cb Coinbase ticks         [rx, ts_est, p] (ts_est = rx - d/1000; 08-11..12)
Plus every boundary capture, replicated exactly like ChainlinkFeed._record_boundary:
first t-record (arrival order) whose floor(ts/300) == B -> {B: [ts, rx, p, prev_ts]}.

Output: data/win_streams.jsonl.gz (one JSON per window) + data/boundaries.json
"""
import gzip
import json
import sqlite3
import sys
import time
from pathlib import Path

SP = Path(__file__).parent
REC = Path(__file__).resolve().parents[2] / "polybot" / "memory" / "recordings"
TWAP_SWITCH = 1786060800
DAYS = [f"2026-08-{d:02d}" for d in range(7, 22)]


def load_label_eps():
    eps = {}
    for name in ("polybot_paper.db", "polybot_live.db"):
        p = SP / "data" / name
        if not p.exists():
            continue
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute("SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'").fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            ep = int(r["window_id"].rsplit("-", 1)[1])
            if ep >= TWAP_SWITCH:
                eps.setdefault(ep, dict(r))
        db.close()
    return eps


def main():
    labels = load_label_eps()
    print(f"{len(labels)} labeled TWAP-era windows", flush=True)
    win = {ep: {"l": [], "bz": [], "cb": [], "t": []} for ep in labels}
    bnd = {}            # B -> [ts, rx, p, prev_ts]
    last_t_ts = None
    t0 = time.time()

    def bucket(x):
        ep = int((x - 215) // 300) * 300
        return ep if (x - 215) - ep <= 97 and ep in win else None

    def bucket_t(x):
        # official-stream records get a longer window [ep+170, ep+312] so the
        # twap_frozen replica has >=20s of value history at any decision tick
        ep = int((x - 170) // 300) * 300
        return ep if (x - 170) - ep <= 142 and ep in win else None

    for day in DAYS:
        path = REC / f"micro_{day}.jsonl.gz"
        if not path.exists():
            path = REC / f"micro_{day}.jsonl"      # today's file is still plain
        if not path.exists():
            print(f"MISSING {path}", flush=True)
            continue
        n = 0
        opener = (lambda p: gzip.open(p, "rt", encoding="utf-8")) \
            if path.suffix == ".gz" else (lambda p: open(p, encoding="utf-8"))
        with opener(path) as f:
            for line in f:
                n += 1
                if len(line) < 9:
                    continue
                kind = line[7]
                if kind == "b":
                    continue
                if kind == "l":
                    r = json.loads(line)
                    rx = r.get("rx") or r["ts"]
                    ep = bucket(rx)
                    if ep is not None:
                        win[ep]["l"].append((round(rx, 3), r["ts"], r["p"]))
                elif kind == "t" and line[8] == '"':   # "t" only — never "t3" (the retired-stream A/B record)
                    r = json.loads(line)
                    ts = r["ts"]
                    B = int(ts // 300) * 300
                    if B not in bnd:
                        bnd[B] = [ts, r.get("rx"), r["p"], last_t_ts]
                    last_t_ts = ts
                    rx = r.get("rx") or ts
                    ep = bucket_t(rx)
                    if ep is not None:
                        win[ep]["t"].append((round(rx, 3), ts, r["p"]))
                elif kind == "s":
                    src = line[19]
                    r = json.loads(line)
                    rx = r["rx"]
                    ep = bucket(rx)
                    if ep is None:
                        continue
                    if src == "b":
                        win[ep]["bz"].append((rx, r["ts"], r["p"]))
                    else:  # cb: ts_est = rx - d/1000
                        d = r.get("d")
                        ts_est = rx - (d / 1000.0 if d else 0.35)
                        win[ep]["cb"].append((round(rx, 3), round(ts_est, 3), r["p"]))
        print(f"{day}: {n} lines ({time.time() - t0:.0f}s)", flush=True)

    out = SP / "data" / "win_streams.jsonl.gz"
    kept = 0
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for ep in sorted(win):
            w = win[ep]
            if len(w["l"]) < 5:
                continue
            lab = labels[ep]
            f.write(json.dumps({
                "ep": ep, "strike": lab["price_to_beat"], "final": lab["final_price"],
                "up": lab["resolved_up"], "token_up": lab["token_up"],
                "token_down": lab["token_down"],
                "l": w["l"], "bz": w["bz"], "cb": w["cb"], "t": w["t"]}) + "\n")
            kept += 1
    json.dump(bnd, open(SP / "data" / "boundaries.json", "w"))
    print(f"DONE: {kept} windows with raw coverage, {len(bnd)} boundaries "
          f"({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
