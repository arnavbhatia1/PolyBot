"""R6 weekly census pull (08-31 charter session): data-api trades, both
counterparties, for a sample of btc-updown-5m windows across 08-28..31.

Same method as r5_census_pull.py (every STRIDE-th labeled window, /trades
paged 500 to the 3,500-row ceiling, resumable). Labels: paper_0831.db
(box backup, labels through 08-31 19:18Z). Output:
data/vps-0831/r6_pm_trades/<epoch>.jsonl (analysis fields only).
"""
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0831"
OUT = DATA / "r6_pm_trades"
DB = DATA / "paper_0831.db"
START = 1787875200          # 2026-08-28 00:00 UTC
END = 1788220800            # 2026-09-01 00:00 UTC
STRIDE = int(sys.argv[1]) if len(sys.argv) > 1 else 12
GAMMA = "https://gamma-api.polymarket.com"
DAPI = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (polybot-research)"}
KEEP = ("proxyWallet", "side", "asset", "size", "price", "timestamp",
        "transactionHash", "outcome", "name")

_last_req = 0.0
_req_lock: asyncio.Lock


async def spaced_get(client, url, params=None, tries=4):
    global _last_req
    for i in range(tries):
        async with _req_lock:
            wait = 0.12 - (time.monotonic() - _last_req)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_req = time.monotonic()
        try:
            r = await client.get(url, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(3 * (i + 1))
                continue
            return r
        except Exception:
            await asyncio.sleep(2 * (i + 1))
    return None


async def do_window(client, ep: int):
    out = OUT / f"{ep}.jsonl"
    if out.exists():
        return "skip"
    r = await spaced_get(client, f"{GAMMA}/events", params={"slug": f"btc-updown-5m-{ep}"})
    if r is None or r.status_code != 200:
        out.with_suffix(".err").write_text(f"gamma {r.status_code if r else 'net'}")
        return "gamma_err"
    try:
        cid = r.json()[0]["markets"][0]["conditionId"]
    except (KeyError, IndexError, ValueError):
        out.write_text("")
        return "no_market"
    rows = []
    offset = 0
    while offset <= 3000:
        r = await spaced_get(client, f"{DAPI}/trades", params={
            "market": cid, "limit": 500, "offset": offset, "takerOnly": "false"})
        if r is None or r.status_code != 200:
            out.with_suffix(".err").write_text(f"dapi {r.status_code if r else 'net'} @ {offset}")
            return "dapi_err"
        page = r.json()
        rows.extend(page)
        if len(page) < 500:
            break
        offset += 500
    tmp = out.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({k: row.get(k) for k in KEEP}) + "\n")
    tmp.replace(out)
    return f"{len(rows)}"


async def main():
    global _req_lock
    _req_lock = asyncio.Lock()
    OUT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    eps = sorted(int(r[0].rsplit("-", 1)[1]) for r in db.execute(
        "SELECT window_id FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'"))
    eps = [ep for ep in eps if START <= ep < END and ep + 300 < time.time() - 900]
    eps = eps[::STRIDE]
    print(f"{len(eps)} windows to pull (stride {STRIDE})", flush=True)
    sem = asyncio.Semaphore(3)
    done = [0]

    async def worker(ep):
        async with sem:
            res = await do_window(client, ep)
        done[0] += 1
        if done[0] % 20 == 0:
            print(f"{done[0]}/{len(eps)} (last: ep {ep} -> {res})", flush=True)

    async with httpx.AsyncClient(http2=True, headers=HEADERS) as client:
        await asyncio.gather(*(worker(ep) for ep in eps))
    n_ok = len(list(OUT.glob("*.jsonl")))
    n_err = len(list(OUT.glob("*.err")))
    print(f"DONE ok={n_ok} err={n_err}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
