"""R10 (08-31 charter): btc-updown-15m in-window deep-flow measurement, pull side.

Since 08-14 the 15m family resolves on the SAME 60s Chainlink TWAP
(resolutionSource btc-usd-twap-60s-streams, venue memo 08-31), so deep_proj's
projection transfers identically; the unknown is the SUPPLY: winner-token
sell-side flow <= 0.80 in the final 25s + post-close. This pulls data-api
trades (both counterparties) + Gamma market metadata (outcomePrices for the
winner) for every STRIDE-th 15m window in [START, END).
Output: data/vps-0831/r10_pm_15m/<epoch>.jsonl + <epoch>.meta.json
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

SP = Path(__file__).parent
OUT = SP / "data" / "vps-0831" / "r10_pm_15m"
START = 1787443200          # 2026-08-23 00:00 UTC (8 days)
END = 1788220800            # 2026-09-01 00:00 UTC
STRIDE = int(sys.argv[1]) if len(sys.argv) > 1 else 3
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
    r = await spaced_get(client, f"{GAMMA}/events", params={"slug": f"btc-updown-15m-{ep}"})
    if r is None or r.status_code != 200:
        out.with_suffix(".err").write_text(f"gamma {r.status_code if r else 'net'}")
        return "gamma_err"
    try:
        m = r.json()[0]["markets"][0]
        cid = m["conditionId"]
    except (KeyError, IndexError, ValueError):
        out.write_text("")
        return "no_market"
    meta = {k: m.get(k) for k in ("conditionId", "outcomePrices", "outcomes",
                                  "clobTokenIds", "volume", "closed",
                                  "orderPriceMinTickSize", "orderMinSize")}
    (OUT / f"{ep}.meta.json").write_text(json.dumps(meta))
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
    eps = [ep for ep in range(START, END, 900) if ep + 900 < time.time() - 900]
    eps = eps[::STRIDE]
    print(f"{len(eps)} 15m windows to pull (stride {STRIDE})", flush=True)
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
