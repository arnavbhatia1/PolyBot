"""WS3.4: deep-level resting-depth time series from sweep prints.

When the tape prints volume AT level L and then prints STRICTLY below L within
the same episode (<=60s), the accumulated at-L volume ~= the resting depth
that was consumed. Daily median per level across both tokens of every window
= the honest history of AT_PRICE_QUEUE_SH (135 was a one-day book-watch).
"""
import gzip
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
REC = Path(__file__).resolve().parents[2] / "polybot" / "memory" / "recordings"
TWAP_SWITCH = 1786060800
LEVELS = [0.80, 0.65, 0.50, 0.35, 0.20]
EPS = 1e-9
EPISODE_S = 60.0


def main():
    labels = {}
    for name in ("polybot_paper.db", "polybot_live.db"):
        p = DATA / name
        if not p.exists():
            continue
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        for r in db.execute("SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'"):
            ep = int(r["window_id"].rsplit("-", 1)[1])
            if ep >= TWAP_SWITCH:
                labels.setdefault(ep, dict(r))
        db.close()
    toks = set()
    for lab in labels.values():
        toks.add(lab["token_up"])
        toks.add(lab["token_down"])

    sweeps = defaultdict(lambda: defaultdict(list))   # day -> level -> [consumed]
    cur = {}                                          # (token, L) -> [start_ts, vol]
    for f in sorted(REC.glob("tape_2026-08-*.jsonl.gz")):
        day = f.stem.split("_")[1][5:]
        with gzip.open(f, "rt") as fh:
            for line in fh:
                r = json.loads(line)
                tok = r["token"]
                if tok not in toks:
                    continue
                try:
                    ts, px, sz = float(r["ts"]), float(r["price"]), float(r["size"])
                except (TypeError, ValueError):
                    continue
                for L in LEVELS:
                    key = (tok, L)
                    st = cur.get(key)
                    if st and ts - st[0] > EPISODE_S:
                        del cur[key]
                        st = None
                    if abs(px - L) <= EPS:
                        if st is None:
                            cur[key] = [ts, sz]
                        else:
                            st[1] += sz
                    elif px < L - EPS and st is not None:
                        sweeps[day][L].append(st[1])
                        del cur[key]

    print("day     " + "  ".join(f"L={L}: n/med/p75" for L in LEVELS))
    for day in sorted(sweeps):
        cells = []
        for L in LEVELS:
            xs = sorted(sweeps[day][L])
            if xs:
                cells.append(f"{len(xs):3d}/{xs[len(xs) // 2]:6.0f}/{xs[int(0.75 * len(xs))]:6.0f}")
            else:
                cells.append("  -")
        print(f"08-{day}  " + "  ".join(cells))
    allx = sorted(x for d in sweeps.values() for L in d.values() for x in L)
    if allx:
        print(f"\nall levels pooled: n={len(allx)} med={allx[len(allx) // 2]:.0f} "
              f"p75={allx[int(0.75 * len(allx))]:.0f}")


if __name__ == "__main__":
    main()
