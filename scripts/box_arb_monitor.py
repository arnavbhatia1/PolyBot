"""Phase 5: cross-horizon box-arb monitor (LOG-ONLY until Phase 1 is live).

The hourly BTC up/down and the :55 five-minute window resolve at the same
instant off the same Chainlink series with different strikes. Monotonicity:
P(BTC > K_high) <= P(BTC > K_low). A violation priced beyond two taker fees is
a riskless box (buy 5m side + hourly counter-side, guaranteed $1 > cost).

Standalone process — does not touch the trading bot:
  python scripts/box_arb_monitor.py            # runs continuously
Logs every checked overlap and every violation to
memory/recordings/box_arb.jsonl. Execution is deliberately absent per the
plan's sequencing (build it only after Phase 1 proves order mechanics).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polybot.execution.base import DEFAULT_FEE_RATE  # noqa: E402
from polybot.paths import MEMORY_DIR  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
OUT = MEMORY_DIR / "recordings" / "box_arb.jsonl"

# Hourly slug candidates — Polymarket naming drifts; first that resolves wins.
HOURLY_SLUGS = ("btc-updown-1h-{ts}", "btc-updown-hourly-{ts}", "btc-up-or-down-1h-{ts}")


async def fetch_event(client: httpx.AsyncClient, slug: str) -> dict | None:
    try:
        r = await client.get(f"{GAMMA}/events", params={"slug": slug}, timeout=5)
        r.raise_for_status()
        data = r.json()
        return data[0] if isinstance(data, list) and data else (data or None)
    except Exception:
        return None


async def best_ask(client: httpx.AsyncClient, token_id: str) -> tuple[float, float]:
    """(price, size) of the best ask via CLOB book."""
    try:
        r = await client.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=5)
        r.raise_for_status()
        asks = (r.json() or {}).get("asks") or []
        if asks:
            lvl = min(asks, key=lambda a: float(a["price"]))
            return float(lvl["price"]), float(lvl["size"])
    except Exception:
        pass
    return 0.0, 0.0


def tokens_of(event: dict) -> tuple[str, str]:
    """(token_up, token_down) from a Gamma event."""
    try:
        m = (event.get("markets") or [{}])[0]
        toks = m.get("clobTokenIds")
        if isinstance(toks, str):
            toks = json.loads(toks)
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        pair = dict(zip([o.lower() for o in outcomes], toks))
        return pair.get("up", ""), pair.get("down", "")
    except Exception:
        return "", ""


def log_row(row: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


async def check_overlap(client: httpx.AsyncClient, hour_ts: int) -> None:
    """At each top of hour, the :55 5m window and the hourly share the expiry."""
    five_ts = hour_ts - 300
    five = await fetch_event(client, f"btc-updown-5m-{five_ts}")
    hourly = None
    for pattern in HOURLY_SLUGS:
        hourly = await fetch_event(client, pattern.format(ts=hour_ts - 3600))
        if hourly:
            break
    row: dict = {"ts": round(time.time(), 1), "expiry": hour_ts,
                 "five_found": bool(five), "hourly_found": bool(hourly)}
    if not five or not hourly:
        log_row(row)
        return

    f_up, f_dn = tokens_of(five)
    h_up, h_dn = tokens_of(hourly)
    if not all((f_up, f_dn, h_up, h_dn)):
        log_row(row | {"note": "tokens_missing"})
        return

    # Box A: buy 5m-Up + hourly-Down. Pays $1 iff strikes ordered K5 > Kh... the
    # guaranteed-$1 leg pair depends on strike order; check both directions and
    # let the offline analysis sort strike order from the logged metadata.
    asks = {}
    for name, tok in (("five_up", f_up), ("five_down", f_dn),
                      ("hour_up", h_up), ("hour_down", h_dn)):
        asks[name] = await best_ask(client, tok)
    row["asks"] = {k: v[0] for k, v in asks.items()}
    row["sizes"] = {k: v[1] for k, v in asks.items()}

    for legs in (("five_up", "hour_down"), ("five_down", "hour_up")):
        p1, p2 = asks[legs[0]][0], asks[legs[1]][0]
        if p1 <= 0 or p2 <= 0:
            continue
        fee = DEFAULT_FEE_RATE * (p1 * (1 - p1) + p2 * (1 - p2))
        cost = p1 + p2 + fee
        if cost < 1.0:
            row["violation"] = {"legs": legs, "cost": round(cost, 4),
                                "margin": round(1.0 - cost, 4),
                                "fillable": round(min(asks[legs[0]][1], asks[legs[1]][1]), 2)}
            print(f"{datetime.now(timezone.utc):%H:%M:%S} BOX: {legs} cost {cost:.4f} "
                  f"margin {1-cost:+.4f} size {row['violation']['fillable']}")
    log_row(row)


async def main() -> None:
    print(f"box-arb monitor running (log-only) -> {OUT}")
    async with httpx.AsyncClient() as client:
        while True:
            now = time.time()
            next_hour = (int(now // 3600) + 1) * 3600
            # Sample during the overlap window: last 6 minutes before each hour.
            wake = next_hour - 360 if now < next_hour - 360 else now + 30
            await asyncio.sleep(max(5.0, wake - now))
            if time.time() >= next_hour - 360:
                await check_overlap(client, next_hour)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
