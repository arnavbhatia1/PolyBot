"""H3 ground truth (n is tiny): did the era live ladder fills print on the
filled token's public tape?

A real maker fill IS a trade — it should print on our token. If a live fill
has no own-token print at <= its price in the resting span but the complement
printed at >= 1-p, the fill arrived via a complement cross that paper's
own-token-only matcher structurally cannot see. Output: h3_live_fill_check.json.
"""
import gzip
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
EPOCH = 1786665600
TOL = 0.005


def main():
    c = sqlite3.connect(f"file:{DATA / 'polybot_live_0821.db'}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    fills = []
    for r in c.execute("SELECT market_id, side, entry_price, size, entry_timestamp, "
                       "indicator_snapshot FROM positions WHERE entry_timestamp >= '2026-08-14'"):
        ep = int(r["market_id"].rsplit("-", 1)[1])
        if ep < EPOCH:
            continue
        snap = json.loads(r["indicator_snapshot"] or "{}")
        tc = snap.get("trade_context") or {}
        own = tc.get("token_id_up") if r["side"] == "Up" else tc.get("token_id_down")
        comp = tc.get("token_id_down") if r["side"] == "Up" else tc.get("token_id_up")
        fills.append({"window": ep, "side": r["side"], "price": r["entry_price"],
                      "size_usd": r["size"], "own": own, "comp": comp})
    c.close()

    by_day = {}
    for f in fills:
        day = datetime.fromtimestamp(f["window"], timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(f)
    # window close can spill into the next UTC day's tape
    for day in list(by_day):
        for f in by_day[day]:
            nxt = datetime.fromtimestamp(f["window"] + 400, timezone.utc).strftime("%Y-%m-%d")
            if nxt != day:
                by_day.setdefault(nxt, []).append(f)

    out = []
    for day, dfills in sorted(by_day.items()):
        path = DATA / f"tape_{day}.jsonl.gz"
        if not path.exists():
            path = DATA / f"tape_{day}.jsonl"
        if not path.exists():
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        toks = {}
        for f in dfills:
            f.setdefault("own_prints_le_p", 0)
            f.setdefault("own_prints_any", 0)
            f.setdefault("comp_prints_ge_1mp", 0)
            toks.setdefault(f["own"], []).append(("own", f))
            toks.setdefault(f["comp"], []).append(("comp", f))
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                hits = toks.get(r["token"])
                if not hits:
                    continue
                ts, px = float(r["ts"]), float(r["price"])
                for role, f in hits:
                    close = f["window"] + 300
                    if not (close - 90 <= ts <= close + 60):
                        continue
                    if role == "own":
                        f["own_prints_any"] += 1
                        if px <= f["price"] + TOL:
                            f["own_prints_le_p"] += 1
                    elif px >= (1.0 - f["price"]) - TOL:
                        f["comp_prints_ge_1mp"] += 1
        for f in dfills:
            if f not in out:
                out.append(f)

    for f in out:
        f.pop("own", None); f.pop("comp", None)
    (DATA / "h3_live_fill_check.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
