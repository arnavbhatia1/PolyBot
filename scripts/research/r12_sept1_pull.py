"""R12 (09-01): first post-rewards-expiry census pull — btc-updown-5m windows
of 2026-09-01 00:00Z → now, stride 4. Same method as r6_census_pull.
Output: data/vps-0831/r12_pm_trades/<epoch>.jsonl
"""
import asyncio
import json
import time
from pathlib import Path

import httpx

SP = Path(__file__).parent
OUT = SP / "data" / "vps-0831" / "r12_pm_trades"
START = 1788220800          # 2026-09-01 00:00 UTC
STRIDE = 4
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
        return "gamma_err"
    try:
        m = r.json()[0]["markets"][0]
        cid = m["conditionId"]
    except (KeyError, IndexError, ValueError):
        out.write_text("")
        return "no_market"
    (OUT / f"{ep}.meta.json").write_text(json.dumps(
        {k: m.get(k) for k in ("conditionId", "outcomePrices", "outcomes", "clobTokenIds")}))
    rows = []
    offset = 0
    while offset <= 3000:
        r = await spaced_get(client, f"{DAPI}/trades", params={
            "market": cid, "limit": 500, "offset": offset, "takerOnly": "false"})
        if r is None or r.status_code != 200:
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
    eps = [ep for ep in range(START, int(time.time()), 300) if ep + 300 < time.time() - 900]
    eps = eps[::STRIDE]
    print(f"{len(eps)} windows to pull (stride {STRIDE})", flush=True)
    sem = asyncio.Semaphore(3)

    async def worker(ep):
        async with sem:
            await do_window(client, ep)

    async with httpx.AsyncClient(http2=True, headers=HEADERS) as client:
        await asyncio.gather(*(worker(ep) for ep in eps))
    print(f"DONE ok={len(list(OUT.glob('*.jsonl')))}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
