"""Phase 5: cross-horizon box-arb monitor (LOG-ONLY until Phase 1 is live).

The 15-minute BTC up/down (btc-updown-15m-T, window [T, T+900)) and the final
five-minute window inside it (btc-updown-5m-{T+600}) resolve at the same
instant off the same Chainlink series with different strikes — 96 shared
expiries/day (verified live 2026-06-11; no hourly series exists). Monotonicity:
P(BTC > K_high) <= P(BTC > K_low). A violation priced beyond two taker fees is
a riskless box (buy 5m side + 15m counter-side, guaranteed $1 > cost).

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

MIN_LEG_PRICE = 0.02        # below this an ask is settling dust, not fillable box liquidity
MIN_SECONDS_TO_EXPIRY = 15  # don't evaluate a settling/expired book



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


def strike_of(event: dict) -> float | None:
    """The window strike (priceToBeat) from Gamma eventMetadata — needed to pick
    the riskless leg pair, since the box direction depends on K_5m vs K_15m."""
    try:
        meta = event.get("eventMetadata")
        if isinstance(meta, dict) and meta.get("priceToBeat") is not None:
            return float(meta["priceToBeat"])
    except (TypeError, ValueError):
        pass
    return None


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


async def check_overlap(client: httpx.AsyncClient, expiry_ts: int) -> None:
    """Every quarter hour: the last 5m window and the 15m window share the expiry."""
    five = await fetch_event(client, f"btc-updown-5m-{expiry_ts - 300}")
    quarter = await fetch_event(client, f"btc-updown-15m-{expiry_ts - 900}")
    row: dict = {"ts": round(time.time(), 1), "expiry": expiry_ts,
                 "five_found": bool(five), "quarter_found": bool(quarter)}
    if not five or not quarter:
        log_row(row)
        return

    f_up, f_dn = tokens_of(five)
    h_up, h_dn = tokens_of(quarter)
    if not all((f_up, f_dn, h_up, h_dn)):
        log_row(row | {"note": "tokens_missing"})
        return

    k5, k15 = strike_of(five), strike_of(quarter)
    row["strikes"] = {"five": k5, "quarter": k15}
    asks = {}
    for name, tok in (("five_up", f_up), ("five_down", f_dn),
                      ("quarter_up", h_up), ("quarter_down", h_dn)):
        asks[name] = await best_ask(client, tok)
    row["asks"] = {k: v[0] for k, v in asks.items()}
    row["sizes"] = {k: v[1] for k, v in asks.items()}

    # Only a live, non-settling, two-sided book can host a real box.
    if expiry_ts - time.time() < MIN_SECONDS_TO_EXPIRY:
        log_row(row | {"note": "too_close_to_expiry"})
        return
    if k5 is None or k15 is None:
        log_row(row | {"note": "strike_missing"})
        return

    # Exactly ONE leg pair is a guaranteed-$1 box, fixed by the strike order:
    #   five_up + quarter_down  is riskless iff K_5m <= K_15m
    #   five_down + quarter_up  is riskless iff K_5m >= K_15m
    # The other pair leaves a payout hole (a strike band paying $0), so a sub-$1
    # cost there is a loss, not an arb — never log it as a violation.
    legs = ("five_up", "quarter_down") if k5 <= k15 else ("five_down", "quarter_up")
    p1, p2 = asks[legs[0]][0], asks[legs[1]][0]
    if p1 >= MIN_LEG_PRICE and p2 >= MIN_LEG_PRICE:
        fee = DEFAULT_FEE_RATE * (p1 * (1 - p1) + p2 * (1 - p2))
        cost = p1 + p2 + fee
        if cost < 1.0:
            row["violation"] = {"legs": legs, "cost": round(cost, 4),
                                "margin": round(1.0 - cost, 4),
                                "strikes": {"five": k5, "quarter": k15},
                                "fillable": round(min(asks[legs[0]][1], asks[legs[1]][1]), 2)}
            print(f"{datetime.now(timezone.utc):%H:%M:%S} BOX: {legs} cost {cost:.4f} "
                  f"margin {1-cost:+.4f} size {row['violation']['fillable']}")
    log_row(row)


async def main() -> None:
    print(f"box-arb monitor running (log-only) -> {OUT}")
    async with httpx.AsyncClient() as client:
        while True:
            now = time.time()
            next_q = (int(now // 900) + 1) * 900
            # Sample during the overlap: the final 4 minutes before each quarter hour
            # (the 5m leg only exists for the last 5 minutes).
            wake = next_q - 240 if now < next_q - 240 else now + 45
            await asyncio.sleep(max(5.0, wake - now))
            if time.time() >= next_q - 240:
                await check_overlap(client, next_q)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
