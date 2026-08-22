"""H1 pass 5a: pull data-api trades (both counterparties) for a SAMPLE of
windows chosen for the two attribution targets:

  C  mid-window bid wall  -> top windows by k300-60 taker-SELL maker capture
  B  terminal loser-ask   -> top windows by k6-0 p<=0.10 taker-BUY notional

35 windows each, union deduped. Output: data/vps-0821/h1_pm_trades/<ep>.jsonl
Resumable (existing files skipped). Scoped: only these windows are downloaded.
"""
import asyncio
import json
import pickle
import time
from pathlib import Path

import httpx

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
OUT = DATA / "h1_pm_trades"
GAMMA = "https://gamma-api.polymarket.com"
DAPI = "https://data-api.polymarket.com"
KEEP = ("proxyWallet", "side", "asset", "size", "price", "timestamp",
        "transactionHash", "outcome", "name")

_last_req = 0.0
_req_lock: asyncio.Lock


def pick_windows():
    with open(DATA / "h1_cellstats.pkl", "rb") as f:
        st = pickle.load(f)
    lab = st["labels"]
    c_score, b_score = {}, {}
    for w, d in st["cells"].items():
        ru = lab[w]
        c = b = 0.0
        for (kb, pb, side, is_up), (s, ps, n) in d.items():
            v = 1.0 if is_up == ru else 0.0
            if kb == "k300-60" and side == "SELL":
                c += v * s - ps          # maker (bid) capture
            if kb == "k6-0" and side == "BUY" and pb == 0:
                b += ps                   # lottery notional sold by makers
        c_score[w] = c
        b_score[w] = b
    top_c = sorted(c_score, key=lambda w: -c_score[w])[:35]
    top_b = sorted(b_score, key=lambda w: -b_score[w])[:35]
    sample = sorted(set(top_c) | set(top_b))
    meta = {"C": top_c, "B": top_b, "union": sample}
    with open(DATA / "h1_wallet_sample.json", "w") as f:
        json.dump(meta, f, indent=1)
    return sample


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
    r = await spaced_get(client, f"{GAMMA}/events/slug/btc-updown-5m-{ep}")
    if r is None or r.status_code != 200:
        return "gamma_err"
    try:
        cid = r.json()["markets"][0]["conditionId"]
    except (KeyError, IndexError, ValueError):
        out.write_text("")
        return "no_market"
    rows = []
    offset = 0
    while offset <= 3000:
        r = await spaced_get(client, f"{DAPI}/trades", params={
            "market": cid, "limit": 500, "offset": offset, "takerOnly": "false"})
        if r is None or r.status_code != 200:
            return f"dapi_err@{offset}"
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
    return str(len(rows))


async def main():
    global _req_lock
    _req_lock = asyncio.Lock()
    OUT.mkdir(parents=True, exist_ok=True)
    sample = pick_windows()
    print(f"{len(sample)} windows to pull", flush=True)
    sem = asyncio.Semaphore(4)
    results = {}

    async def worker(ep):
        async with sem:
            results[ep] = await do_window(client, ep)

    async with httpx.AsyncClient(http2=True) as client:
        await asyncio.gather(*(worker(ep) for ep in sample))
    from collections import Counter
    print(Counter(v if not v.isdigit() else "ok" for v in results.values()))
    trunc = [ep for ep, v in results.items() if v.isdigit() and int(v) >= 3500]
    print(f"row-capped windows: {len(trunc)}")


if __name__ == "__main__":
    asyncio.run(main())
