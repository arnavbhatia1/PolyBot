"""Collect winner-side ask events (final 66s + close) per 60s-era window.

Streams micro 08-14..18 "b" records for labeled winner tokens ->
data/winner_books.jsonl.gz: {ep, asks: [[ts, ask], ...]}.
"""
import gzip
import json
import sqlite3
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
REC = Path(__file__).resolve().parents[2] / "polybot" / "memory" / "recordings"
RULE_TS = 1786665600
DAYS = ["2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18"]


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
            if ep >= RULE_TS:
                labels.setdefault(ep, dict(r))
        db.close()
    tok = {}
    for ep, lab in labels.items():
        w = lab["token_up"] if lab["resolved_up"] else lab["token_down"]
        tok[w] = ep
    books = {}
    for day in DAYS:
        path = REC / f"micro_{day}.jsonl.gz"
        if not path.exists():
            path = REC / f"micro_{day}.jsonl"
        if not path.exists():
            continue
        opener = (lambda p: gzip.open(p, "rt", encoding="utf-8")) \
            if path.suffix == ".gz" else (lambda p: open(p, encoding="utf-8"))
        n = 0
        with opener(path) as f:
            for line in f:
                n += 1
                if len(line) < 9 or line[7] != "b":
                    continue
                i = line.find('"token": "')
                if i < 0:
                    continue
                t = line[i + 10: i + 120].split('"', 1)[0]
                ep = tok.get(t)
                if ep is None:
                    continue
                r = json.loads(line)
                ts = r["ts"]
                if ep + 234 <= ts <= ep + 302:
                    try:
                        books.setdefault(ep, []).append(
                            (round(ts, 3), float(r["ask"])))
                    except (TypeError, ValueError):
                        pass
        print(f"{day}: {n} lines", flush=True)
    with gzip.open(DATA / "winner_books.jsonl.gz", "wt") as f:
        for ep in sorted(books):
            f.write(json.dumps({"ep": ep, "asks": books[ep]}) + "\n")
    print(f"DONE {len(books)} windows with winner-side book events")


if __name__ == "__main__":
    main()
