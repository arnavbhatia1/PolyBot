"""Deep winner-side panic supply per day: the resource deep_proj harvests.

For every labeled window (both eras): prints on the WINNER token at price
<= 0.80 inside [close-60, close+60], bucketed by depth. This is the leg's
capacity ceiling — arming is now ~free (73% of windows), so fills == supply.
"""
import gzip
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
REC = Path(__file__).resolve().parents[2] / "polybot" / "memory" / "recordings"
TWAP_SWITCH = 1786060800
DAYS = [f"2026-08-{d:02d}" for d in range(7, 18)]
BUCKETS = [0.80, 0.65, 0.50, 0.35, 0.20]


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
    tok = {}
    for ep, lab in labels.items():
        wtok = lab["token_up"] if lab["resolved_up"] else lab["token_down"]
        tok[wtok] = ep

    # per-day: windows with a winner-side deep print, volumes by bucket
    day_win = {}
    for day in DAYS:
        p = REC / f"tape_{day}.jsonl.gz"
        if not p.exists():
            continue
        with gzip.open(p, "rt") as f:
            for line in f:
                r = json.loads(line)
                ep = tok.get(r["token"])
                if ep is None:
                    continue
                try:
                    ts, px, sz = float(r["ts"]), float(r["price"]), float(r["size"])
                except (TypeError, ValueError):
                    continue
                close = ep + 300
                if not (close - 60 <= ts <= close + 60):
                    continue
                if px > 0.80 + 1e-9:
                    continue
                d = datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d")
                w = day_win.setdefault(d, {}).setdefault(ep, {b: 0.0 for b in BUCKETS})
                for b in BUCKETS:
                    if px <= b + 1e-9:
                        w[b] += sz

    print("day    n_win_with_deep_print   vol<=.80  <=.65  <=.50  <=.35  <=.20   (sh, winner side, close+-60s)")
    for d in sorted(day_win):
        ws = day_win[d]
        tots = {b: sum(w[b] for w in ws.values()) for b in BUCKETS}
        era = "60s" if d >= "08-14" else "30s"
        print(f"{d} ({era})  {len(ws):3d}   " +
              "  ".join(f"{tots[b]:8.0f}" for b in BUCKETS))
        for ep, w in sorted(ws.items()):
            if w[0.80] >= 5:
                t = datetime.fromtimestamp(ep, timezone.utc).strftime("%H:%M")
                print(f"    {t}  " + "  ".join(f"{w[b]:7.1f}" for b in BUCKETS))


if __name__ == "__main__":
    main()
