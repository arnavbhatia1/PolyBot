"""H4 crux: when does Polymarket auto-redeem actually credit, vs the close?

The sell-the-certain-winner hunt only pays if freed capital arrives before the
NEXT window's ladder placement (k<=25s before its close = current close +275s).
Two sources:
  A. polybot_live_0821.db — exit_timestamp on wins is when the bot CONFIRMED
     tokens gone (10s check cadence, gated on resolution detection first), so
     win-vs-loss booking lags bound how much redeem adds beyond detection.
  B. data-api /activity REDEEM rows for wallet 1723 (always-on maker, holds to
     resolution like us) — on-chain block timestamps vs window closes = the
     true redeem-credit lag distribution.

Output: scripts/research/data/vps-0821/h4_redeem_lag.json + console summary.
"""
import asyncio
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import httpx

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
LIVE_DB = DATA / "polybot_live_0821.db"
PAPER_DB = DATA / "polybot_paper_0821.db"
OUT = DATA / "h4_redeem_lag.json"
ADDR_CACHE = DATA / "h4_wallet_1723.txt"

GAMMA = "https://gamma-api.polymarket.com"
DAPI = "https://data-api.polymarket.com"
PREFIX = "0x3b8407699e83"   # 1723's proxyWallet prefix (WALLETS.md / ws3_census)
ERA = 1786665600            # 60s-rule era split (2026-08-14 00:00 UTC)
DEADLINE_S = 275.0          # next-window ladder arm opens at close+275s (k=25)

_last_req = 0.0
_req_lock: asyncio.Lock


async def spaced_get(client, url, params=None, tries=4):
    global _last_req
    for i in range(tries):
        async with _req_lock:
            wait = 0.15 - (time.monotonic() - _last_req)
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


def quantiles(v):
    if not v:
        return None
    v = sorted(v)
    def q(p):
        return v[max(0, min(len(v) - 1, round(p * (len(v) - 1))))]
    return {"n": len(v), "p10": q(.10), "p50": q(.50), "p90": q(.90),
            "p95": q(.95), "max": max(v),
            "frac_gt_275s": sum(1 for x in v if x > DEADLINE_S) / len(v),
            "frac_gt_294s": sum(1 for x in v if x > 294) / len(v)}


def live_db_lags():
    con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    wins, losses = [], []
    for r in con.execute("SELECT market_id, exit_price, exit_timestamp FROM positions "
                         "WHERE status='closed' AND exit_timestamp IS NOT NULL"):
        try:
            ep = int(r["market_id"].rsplit("-", 1)[1])
        except (ValueError, IndexError):
            continue
        lag = datetime.fromisoformat(r["exit_timestamp"]).timestamp() - (ep + 300)
        if r["exit_price"] >= 0.99:
            wins.append(lag)
        elif r["exit_price"] <= 0.01:
            losses.append(lag)
    con.close()
    return {"win_booking_lag_s": quantiles(wins), "loss_booking_lag_s": quantiles(losses)}


async def rank_wallets(client, n_windows=60) -> list[str]:
    """Rank proxyWallets by presence across recent windows' prints.

    1723 may be dormant post-rule; auto-redeem cadence is Polymarket-side
    infrastructure, so any always-on hold-to-resolution wallet measures it.
    Returns the top wallets by window-presence (1723 promoted if seen).
    """
    con = sqlite3.connect(f"file:{PAPER_DB}?mode=ro", uri=True)
    eps = sorted((int(r[0].rsplit("-", 1)[1]) for r in con.execute(
        "SELECT window_id FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'")),
        reverse=True)
    con.close()
    presence: dict[str, int] = {}
    scanned = 0
    for ep in eps[:n_windows * 4:4]:
        r = await spaced_get(client, f"{GAMMA}/events/slug/btc-updown-5m-{ep}")
        if r is None or r.status_code != 200:
            continue
        try:
            cid = r.json()["markets"][0]["conditionId"]
        except (KeyError, IndexError, ValueError):
            continue
        r = await spaced_get(client, f"{DAPI}/trades", params={
            "market": cid, "limit": 500, "takerOnly": "false"})
        if r is None or r.status_code != 200:
            continue
        scanned += 1
        for w in {(row.get("proxyWallet") or "").lower() for row in r.json()}:
            if w:
                presence[w] = presence.get(w, 0) + 1
    ranked = sorted(presence, key=lambda w: -presence[w])
    found_1723 = [w for w in ranked if w.startswith(PREFIX)]
    if found_1723:
        ADDR_CACHE.write_text(found_1723[0])
    top = (found_1723 + [w for w in ranked if not w.startswith(PREFIX)])[:6]
    print(f"scanned {scanned} windows; top wallets by presence: "
          + ", ".join(f"{w[:10]}({presence[w]})" for w in top))
    return top


async def redeem_lags(client, addr):
    """Page 1723's activity; REDEEM/MERGE rows on btc-updown-5m windows -> lag vs close."""
    rows, offset = [], 0
    while offset <= 10000:
        r = await spaced_get(client, f"{DAPI}/activity", params={
            "user": addr, "type": "REDEEM,MERGE", "limit": 500, "offset": offset})
        if r is None or r.status_code != 200:
            print(f"activity fetch failed at offset {offset}: "
                  f"{r.status_code if r else 'net'}")
            break
        page = r.json()
        if not isinstance(page, list) or not page:
            break
        rows.extend(page)
        if len(page) < 500:
            break
        offset += 500
    lags = {"REDEEM": {"pre": [], "post": []}, "MERGE": {"pre": [], "post": []}}
    kept = 0
    for row in rows:
        slug = row.get("slug") or ""
        if not slug.startswith("btc-updown-5m-"):
            continue
        try:
            ep = int(slug.rsplit("-", 1)[1])
        except ValueError:
            continue
        t = row.get("timestamp")
        typ = row.get("type")
        if t is None or typ not in lags:
            continue
        kept += 1
        lags[typ]["post" if ep >= ERA else "pre"].append(float(t) - (ep + 300))
    print(f"{addr[:10]}: activity rows {len(rows)}, {kept} on btc-updown-5m windows")
    return {"stats": {typ: {era: quantiles(v) for era, v in eras.items()}
                      for typ, eras in lags.items()},
            "raw": lags}


async def main():
    global _req_lock
    _req_lock = asyncio.Lock()
    out = {"deadline_s": DEADLINE_S, "live_db": live_db_lags()}
    async with httpx.AsyncClient(http2=True) as client:
        wallets = await rank_wallets(client)
        out["onchain"] = {}
        pooled = {"REDEEM": {"pre": [], "post": []}, "MERGE": {"pre": [], "post": []}}
        for w in wallets:
            res = await redeem_lags(client, w)
            out["onchain"][w] = res["stats"]
            for typ in pooled:
                for era in pooled[typ]:
                    pooled[typ][era].extend(res["raw"][typ][era])
        out["onchain_pooled"] = {typ: {era: quantiles(v) for era, v in eras.items()}
                                 for typ, eras in pooled.items()}
    OUT.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
