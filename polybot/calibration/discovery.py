"""Gamma discovery + parsing for long-horizon crypto markets + resolution derivation.

Mirrors market_scanner.parse_contract conventions (outcomePrices/clobTokenIds may be
JSON-stringified) but is standalone and multi-rung (these are negRisk strike ladders,
not single 5-min markets). Three target families, each mapped to a pricing kind so the
Deribit cross-check knows whether to price a terminal digital or a one-touch barrier:

  daily_updown    {coin}-above-on-DATE              -> 'digital'  P(S_T >= K at date)
  touch_window    what-price-will-{coin}-hit-...     -> 'touch'    P(touch K before end)
  touch_milestone {coin}-all-time-high-by / when-will-{coin}-hit-... -> 'touch'

Excluded: bitcoin-price-on-DATE (between-brackets, not a clean digital/touch).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
COINS = ("bitcoin", "ethereum", "solana", "xrp")
_COIN_RE = "|".join(COINS)

# (compiled slug pattern, family, pricing_kind)
_FAMILY_PATTERNS = [
    (re.compile(rf"^({_COIN_RE})-above-on-"), "daily_updown", "digital"),
    (re.compile(rf"^what-price-will-({_COIN_RE})-hit-"), "touch_window", "touch"),
    (re.compile(rf"^({_COIN_RE})-all-time-high-by"), "touch_milestone", "touch"),
    (re.compile(rf"^when-will-({_COIN_RE})-hit-"), "touch_milestone", "touch"),
]


@dataclass
class MarketRef:
    slug: str
    coin: str
    family: str
    pricing_kind: str          # 'digital' | 'touch'
    condition_id: str
    token0_id: str             # the "Yes"/"above"/"touched" outcome token
    strike: float | None
    title: str
    end_dt: datetime | None
    neg_risk: bool
    closed: bool
    outcome: int | None        # 1 if token0 resolved YES, 0 if NO, None if unresolved


def classify(slug: str) -> tuple[str, str, str] | None:
    """Return (family, coin, pricing_kind) for a target slug, else None."""
    for pat, family, kind in _FAMILY_PATTERNS:
        m = pat.match(slug)
        if m:
            return family, m.group(1), kind
    return None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return []
    return v or []


def _strike_of(market: dict) -> float | None:
    t = (market.get("groupItemTitle") or market.get("question") or "").replace(",", "")
    m = re.search(r"(\d[\d.]*)\s*[kK]?", t)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    # "60k" style
    if re.search(r"\d\s*[kK]\b", t) and val < 1000:
        val *= 1000
    return val


def _resolution(market: dict, event_closed: bool) -> int | None:
    """Derive token0 outcome (1/0) for a resolved market; None if not cleanly resolved."""
    if not (event_closed or market.get("closed")):
        return None
    prices = _as_list(market.get("outcomePrices"))
    if not prices:
        return None
    try:
        p0 = float(prices[0])
    except (TypeError, ValueError):
        return None
    # require a degenerate (settled) price, not a mid-life quote
    if not (p0 >= 0.98 or p0 <= 0.02):
        return None
    return 1 if p0 >= 0.5 else 0


def parse_event(event: dict) -> list[MarketRef]:
    """Parse one Gamma event into per-rung MarketRefs (empty if not a target family)."""
    slug = event.get("slug", "")
    cls = classify(slug)
    if not cls:
        return []
    family, coin, kind = cls
    end_dt = _parse_dt(event.get("endDate"))
    neg_risk = bool(event.get("negRisk"))
    event_closed = bool(event.get("closed"))
    refs: list[MarketRef] = []
    for m in event.get("markets", []):
        tokens = _as_list(m.get("clobTokenIds"))
        if not tokens:
            continue
        refs.append(MarketRef(
            slug=slug, coin=coin, family=family, pricing_kind=kind,
            condition_id=str(m.get("conditionId") or m.get("condition_id") or ""),
            token0_id=str(tokens[0]),
            strike=_strike_of(m),
            title=(m.get("groupItemTitle") or m.get("question") or "")[:48],
            end_dt=end_dt, neg_risk=neg_risk,
            closed=event_closed or bool(m.get("closed")),
            outcome=_resolution(m, event_closed),
        ))
    return refs


async def fetch_event_by_slug(client: httpx.AsyncClient, slug: str) -> dict | None:
    """Fetch a single event by slug (used by the label pass to read resolution).

    GET /events is deprecated upstream (Sunset header, still tolerated); a
    non-2xx falls back to the undeprecated GET /events/slug/{slug} so labeling
    doesn't silently stop the day it's enforced.
    """
    try:
        resp = await client.get(f"{GAMMA_API}/events", params={"slug": slug})
        if not resp.is_success:
            resp = await client.get(f"{GAMMA_API}/events/slug/{slug}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data[0] if data else None
        return data or None
    except Exception:  # noqa: BLE001
        return None


async def discover(client: httpx.AsyncClient, max_pages: int = 8,
                   page_size: int = 100, open_only: bool = True) -> list[MarketRef]:
    """Page through Gamma crypto events and parse all target-family rungs.
    Dedups by (condition_id). open_only filters to not-yet-closed events."""
    seen: set[str] = set()
    out: list[MarketRef] = []
    for page in range(max_pages):
        params = {"limit": page_size, "offset": page * page_size,
                  "tag_slug": "crypto", "order": "volume24hr", "ascending": "false"}
        if open_only:
            params["closed"] = "false"
        try:
            resp = await client.get(f"{GAMMA_API}/events", params=params)
            resp.raise_for_status()
            events = resp.json()
        except Exception:  # noqa: BLE001
            break
        if not events:
            break
        for ev in events:
            for ref in parse_event(ev):
                key = ref.condition_id or f"{ref.slug}:{ref.title}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(ref)
    return out
