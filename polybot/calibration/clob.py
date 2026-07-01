"""CLOB order-book fetch + best bid/ask/depth, mirrored from market_scanner conventions.

Standalone (no scanner instance / no WS / no caching) — one-shot HTTP reads for the
periodic snapshot pass. Book shape: {"asks": [{price,size},...], "bids": [{price,size},...]},
asks price-ascending (asks[0]=best), bids price-descending (bids[0]=best).
"""
from __future__ import annotations

import asyncio
import random

import httpx

CLOB_API = "https://clob.polymarket.com"
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.05
_RETRY_JITTER = 0.2


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> dict:
    """GET /book with exponential backoff. Returns {} on persistent failure."""
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = await client.get(f"{CLOB_API}/book", params={"token_id": token_id})
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001 — read-only probe, any failure -> retry/skip
            last_exc = e
            if attempt < _MAX_RETRIES:
                base = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(base * random.uniform(1 - _RETRY_JITTER, 1 + _RETRY_JITTER))
    return {}


def _best(levels: list, want_max: bool) -> tuple[float | None, float]:
    """Return (best_price, depth_usd) from a list of {price,size} levels.
    depth_usd = sum(price*size). want_max picks the highest price (bids), else lowest (asks)."""
    parsed = []
    for lvl in levels or []:
        try:
            parsed.append((float(lvl["price"]), float(lvl["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not parsed:
        return None, 0.0
    best_price = max(parsed, key=lambda x: x[0])[0] if want_max else min(parsed, key=lambda x: x[0])[0]
    depth_usd = sum(p * s for p, s in parsed)
    return best_price, depth_usd


def quote_from_book(book: dict) -> dict:
    """Extract bid/ask/mid + per-side USD depth. Returns None prices for an empty book."""
    bid, bid_depth = _best(book.get("bids", []), want_max=True)
    ask, ask_depth = _best(book.get("asks", []), want_max=False)
    mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None
    return {"pm_bid": bid, "pm_ask": ask, "pm_mid": mid,
            "bid_depth_usd": bid_depth, "ask_depth_usd": ask_depth}
