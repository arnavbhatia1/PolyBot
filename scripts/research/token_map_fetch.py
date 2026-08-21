"""Fetch token_id -> (window, Up/Down) for every labeled 60s-era window.

Gamma is the authoritative mapping (market slug btc-updown-5m-<ts> carries
clobTokenIds aligned with outcomes). Writes data/vps-0821/token_map.json:
{window_ts: {"up": token_id, "down": token_id}}. Misses are listed under
"missing" — a handful of unresolved slugs is normal.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import httpx

DATA = Path(__file__).parent / "data" / "vps-0821"
OUT = DATA / "token_map.json"
GAMMA = "https://gamma-api.polymarket.com/events"


def windows() -> list[int]:
    db = sqlite3.connect(DATA / "polybot_paper_0821.db")
    rows = db.execute("SELECT window_id FROM window_labels").fetchall()
    db.close()
    out = set()
    for (wid,) in rows:
        try:
            ts = int(str(wid).rsplit("-", 1)[-1])
        except ValueError:
            continue
        if ts >= 1786665600:   # 60s era only
            out.add(ts)
    return sorted(out)


def main() -> None:
    done: dict = {}
    if OUT.exists():
        done = json.loads(OUT.read_text()).get("map", {})
    missing = []
    todo = [w for w in windows() if str(w) not in done]
    print(f"{len(todo)} windows to fetch ({len(done)} cached)")
    with httpx.Client(timeout=15) as c:
        for i, w in enumerate(todo):
            slug = f"btc-updown-5m-{w}"
            try:
                r = c.get(GAMMA, params={"slug": slug})
                ev = r.json()
                mkt = (ev[0].get("markets") or [{}])[0] if ev else {}
                toks = json.loads(mkt.get("clobTokenIds") or "[]")
                outs = json.loads(mkt.get("outcomes") or "[]")
                pair = {o.lower(): t for o, t in zip(outs, toks)}
                if "up" in pair and "down" in pair:
                    done[str(w)] = {"up": pair["up"], "down": pair["down"]}
                else:
                    missing.append(w)
            except Exception:
                missing.append(w)
            if i % 100 == 0:
                OUT.write_text(json.dumps({"map": done, "missing": missing}))
                print(f"{i}/{len(todo)}", flush=True)
            time.sleep(0.12)   # polite to Gamma
    OUT.write_text(json.dumps({"map": done, "missing": missing}))
    print(f"done: {len(done)} mapped, {len(missing)} missing")


if __name__ == "__main__":
    main()
