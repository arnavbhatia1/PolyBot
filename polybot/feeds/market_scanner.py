from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from polybot.execution.base import DEFAULT_FEE_RATE

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _ensure_client(http_client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """Yield `http_client` if given, else a short-lived one. Lets None callers (tests, scripts) work."""
    if http_client is not None:
        yield http_client
        return
    async with httpx.AsyncClient(timeout=10) as client:
        yield client

class BTCMarketScanner:
    """Discovers active 5-min BTC Up/Down markets on Polymarket via Gamma API.

    Deterministic slugs `btc-updown-5m-{window_ts}` (window_ts floored to the
    300s boundary); outcomes are "Up"/"Down".

    Gamma outcomePrices are stale/indicative (last trade or initial 50/50), NOT
    the live book — always use fetch_clob_book() for real bid/ask before trading.
    """

    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    WINDOW_SECONDS = 300  # 5 minutes

    def __init__(self, entry_window_seconds: int = 120, min_time_remaining: int = 30,
                 cache_seconds: int = 5, symbol: str = "btc",
                 min_book_depth_usd: float = 50.0,
                 clob_url: str | None = None) -> None:
        self.entry_window_seconds: int = entry_window_seconds
        self.min_time_remaining: int = min_time_remaining
        self.cache_seconds: int = cache_seconds
        self.symbol: str = symbol
        self.min_book_depth_usd: float = min_book_depth_usd
        # Truthy override only — empty string/None falls back to the class constant.
        if clob_url:
            self.CLOB_API = clob_url
        self._cached_contract: dict[str, Any] | None = None
        self._cache_time: float = 0
        self._book_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # token_id -> (timestamp, book)
        self._book_cache_seconds: int = 2
        self._tick_size_cache: dict[str, tuple[float, str]] = {}    # token_id -> (timestamp, tick_size)
        self._tick_size_cache_seconds: int = 3600
        self._last_dns_error_ts: float = 0.0  # throttle DNS-failure log to once per 30s
        self._prewarmed_condition_id: str = ""  # last contract we kicked pre-warm fetches for
        self._prewarm_tasks: set[asyncio.Task[Any]] = set()  # strong refs so GC doesn't kill them
        self._gamma_events_gone: bool = False  # latched once GET /events is enforced-dead

    def _current_window_ts(self) -> int:
        return int(time.time() // self.WINDOW_SECONDS) * self.WINDOW_SECONDS

    def _make_slug(self, window_ts: int) -> str:
        return f"{self.symbol}-updown-5m-{window_ts}"

    async def gamma_events_by_slug(self, client: httpx.AsyncClient, slug: str) -> list[dict[str, Any]]:
        """Single-event Gamma lookup, normalized to a list of event dicts.

        ``GET /events`` carries a Deprecation/Sunset header (sunset 2026-05-01,
        still tolerated — enforceable any day, possibly via redirect). Any
        non-2xx other than a transient 429/5xx means the deprecated endpoint is
        enforced: this call and every later one (latched per scanner) use the
        undeprecated ``GET /events/slug/{slug}`` instead (one event dict; 404 =
        no such event -> []). 429/5xx raise immediately without a second
        request; network errors propagate, matching callers' existing handling.
        """
        resp = None
        if not self._gamma_events_gone:
            resp = await client.get(f"{self.GAMMA_API}/events", params={"slug": slug})
            if not resp.is_success:
                if resp.status_code == 429 or resp.status_code >= 500:
                    resp.raise_for_status()  # transient throttle/outage — fail fast
                logger.warning(
                    f"Gamma GET /events returned {resp.status_code} — deprecated endpoint "
                    f"treated as enforced; using /events/slug/ from now on")
                self._gamma_events_gone = True
                resp = None
        if resp is None:
            resp = await client.get(f"{self.GAMMA_API}/events/slug/{slug}")
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else ([data] if data else [])

    def parse_contract(self, event: dict[str, Any]) -> dict[str, Any] | None:
        markets = event.get("markets", [])
        if not markets:
            return None
        market = markets[0]

        outcomes = market.get("outcomes", [])
        prices_raw = market.get("outcomePrices", [])
        clob_tokens_raw = market.get("clobTokenIds", [])

        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices_raw, str):
            prices_raw = json.loads(prices_raw)
        if isinstance(clob_tokens_raw, str):
            clob_tokens_raw = json.loads(clob_tokens_raw)

        price_up = price_down = 0.0
        token_id_up = token_id_down = ""

        # Gamma should always return 2 prices and 2 token IDs for binary markets. If
        # fewer come back the market is malformed — log once so it doesn't look like
        # "no edge" in the entry evaluator.
        if len(prices_raw) < len(outcomes) or len(clob_tokens_raw) < len(outcomes):
            slug = market.get("slug") or event.get("slug") or "<unknown>"
            logger.warning(
                f"Gamma market '{slug}' has fewer prices/tokens than outcomes "
                f"(outcomes={len(outcomes)}, prices={len(prices_raw)}, "
                f"tokens={len(clob_tokens_raw)}) — downstream book/spread gates "
                f"will skip this contract"
            )

        for i, outcome in enumerate(outcomes):
            price = float(prices_raw[i]) if i < len(prices_raw) else 0.0
            token_id = clob_tokens_raw[i] if i < len(clob_tokens_raw) else ""
            if outcome.lower() == "up":
                price_up = price
                token_id_up = token_id
            elif outcome.lower() == "down":
                price_down = price
                token_id_down = token_id

        end_date_str = event.get("endDate", "") or market.get("endDate", "")
        seconds_remaining = 0.0
        if end_date_str:
            try:
                end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                seconds_remaining = max(0.0, (end - datetime.now(timezone.utc)).total_seconds())
            except ValueError:
                pass

        condition_id = market.get("conditionId", "")
        neg_risk = market.get("negRisk", False)

        # Extract Chainlink oracle prices.
        # price_to_beat is set at window open and available during active windows.
        # final_price is only available after resolution.
        raw_meta = event.get("eventMetadata")
        event_metadata = None
        if raw_meta and isinstance(raw_meta, dict):
            ptb = raw_meta.get("priceToBeat")
            fp = raw_meta.get("finalPrice")
            if ptb is not None:
                event_metadata = {
                    "price_to_beat": float(ptb),
                    "final_price": float(fp) if fp is not None else None,
                }

        return {
            "condition_id": condition_id,
            "question": event.get("title", ""),
            "slug": event.get("slug", ""),
            "price_up": price_up,
            "price_down": price_down,
            "token_id_up": token_id_up,
            "token_id_down": token_id_down,
            "seconds_remaining": seconds_remaining,
            "end_date": end_date_str,
            "neg_risk": neg_risk,
            "closed": event.get("closed", False) or market.get("closed", False),
            "active": event.get("active", False),
            "event_metadata": event_metadata,
        }

    def in_entry_window(self, seconds_remaining: float) -> bool:
        seconds_elapsed = self.WINDOW_SECONDS - seconds_remaining
        return (seconds_elapsed <= self.entry_window_seconds and
                seconds_remaining >= self.min_time_remaining)

    # --- Polymarket CLOB API (real order book, no auth required) ---

    async def fetch_clob_book(self, token_id: str, http_client: httpx.AsyncClient | None = None) -> dict[str, Any]:
        """Fetch the full CLOB order book (no auth). Cached _book_cache_seconds (2s).
        Returns the raw book dict (bids price-desc, asks price-asc), or {} on failure.
        """
        now = time.time()
        cached = self._book_cache.get(token_id)
        if cached and (now - cached[0]) < self._book_cache_seconds:
            return cached[1]

        try:
            url = f"{self.CLOB_API}/book"
            async with _ensure_client(http_client) as client:
                resp = await client.get(url, params={"token_id": token_id})
                resp.raise_for_status()
                book = resp.json()
            self._book_cache[token_id] = (now, book)
            return book
        except Exception as e:
            logger.debug(f"CLOB book failed for {token_id}: {e}")
            return {}

    async def fetch_fee_rate(self, token_id: str, http_client: httpx.AsyncClient | None = None) -> float:
        """Polymarket crypto taker rate. Constant — the live per-order fee is
        ``rate × shares × p × (1-p)`` (see ``taker_fee`` in execution/base.py),
        so price-dependent variation is already in the formula, not the rate.
        Single source: DEFAULT_FEE_RATE in execution/base.py.
        """
        return DEFAULT_FEE_RATE

    async def fetch_tick_size(self, token_id: str, http_client: httpx.AsyncClient | None = None) -> str:
        """Fetch tick size as a string (e.g. "0.01"). Cached 1h; "0.01" on error."""
        now = time.time()
        cached = self._tick_size_cache.get(token_id)
        if cached and (now - cached[0]) < self._tick_size_cache_seconds:
            return cached[1]

        try:
            url = f"{self.CLOB_API}/tick-size"
            async with _ensure_client(http_client) as client:
                resp = await client.get(url, params={"token_id": token_id})
                resp.raise_for_status()
                data = resp.json()
            tick = str(data.get("minimum_tick_size", "0.01"))
            self._tick_size_cache[token_id] = (now, tick)
            return tick
        except Exception as e:
            logger.debug(f"Tick size fetch failed for {token_id}: {e}")
            return "0.01"

    @staticmethod
    def snap_to_tick(price: float, tick_size: str) -> float:
        """Round price down to the nearest tick, clamped to [tick, 1 - tick]
        (Polymarket's valid range)."""
        tick = float(tick_size)
        if tick <= 0:
            return price
        snapped = round(int(price / tick) * tick, 10)
        min_price = tick
        max_price = round(1.0 - tick, 10)
        return max(min_price, min(snapped, max_price))

    @staticmethod
    def clob_best_ask(book: dict[str, Any]) -> tuple[float, float]:
        """(best_ask_price, total_ask_depth); asks are price-asc. (0.0, 0.0) if empty."""
        asks = book.get("asks", [])
        if not asks:
            return (0.0, 0.0)
        best_price = float(asks[0]["price"])
        total_depth = sum(float(a["size"]) for a in asks)
        return (best_price, total_depth)

    # --- NegRisk execution prices (accounts for cross-matching) ---

    async def fetch_market_price(self, token_id: str, side: str = "BUY",
                                  http_client: httpx.AsyncClient | None = None) -> float:
        """GET /price — execution price including negRisk cross-matching of
        complementary tokens. The cross-match can report phantom prices near
        expiry, so this is never a primary price source — consumed only as the
        SELL-side cross-check against a suspect WS best_bid on exit.
        Returns the price as a float, or 0.0 on error.
        """
        try:
            url = f"{self.CLOB_API}/price"
            params = {"token_id": token_id, "side": side}
            async with _ensure_client(http_client) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            return float(data.get("price", 0))
        except Exception as e:
            logger.debug(f"Market price fetch failed for {token_id} {side}: {e}")
            return 0.0

    # --- Lightweight HTTP helpers (public, no auth) ---

    async def get_spread(self, token_id: str, http_client: httpx.AsyncClient | None = None) -> float:
        """GET /spread — bid-ask spread as a float. Returns -1 on error."""
        try:
            url = f"{self.CLOB_API}/spread"
            async with _ensure_client(http_client) as client:
                resp = await client.get(url, params={"token_id": token_id})
                resp.raise_for_status()
                return float(resp.json().get("spread", "-1"))
        except Exception as e:
            logger.debug(f"Spread fetch failed for {token_id}: {e}")
            return -1.0

    async def find_active_contract(self, http_client: httpx.AsyncClient | None = None) -> dict[str, Any] | None:
        now = time.time()

        if self._cached_contract and (now - self._cache_time) < self.cache_seconds:
            contract = self._cached_contract
            end_str = contract.get("end_date", "")
            if end_str:
                try:
                    end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    remaining = (end - datetime.now(timezone.utc)).total_seconds()
                    if remaining > 0:
                        contract["seconds_remaining"] = remaining
                        return contract
                except ValueError:
                    pass
            self._cached_contract = None

        window_ts = self._current_window_ts()
        for ts in [window_ts, window_ts + self.WINDOW_SECONDS]:
            slug = self._make_slug(ts)
            try:
                if http_client:
                    data = await self.gamma_events_by_slug(http_client, slug)
                else:
                    async with httpx.AsyncClient(timeout=10) as client:
                        data = await self.gamma_events_by_slug(client, slug)
            except Exception as e:
                if "getaddrinfo" in str(e).lower() or "dnserror" in type(e).__name__.lower():
                    if now - self._last_dns_error_ts >= 30:
                        logger.warning("Gamma API: DNS failure — network down? (suppressing for 30s)")
                        self._last_dns_error_ts = now
                else:
                    logger.error(f"Gamma API error for {slug}: {e}")
                continue

            if not data:
                continue

            event = data[0]
            if not event.get("active", False):
                continue

            contract = self.parse_contract(event)
            if contract and contract["seconds_remaining"] > self.min_time_remaining:
                self._cached_contract = contract
                self._cache_time = now
                logger.debug(f"Found active contract: {contract['question']} "
                           f"({contract['seconds_remaining']:.0f}s remaining)")
                # Pre-warm tick_size cache for both sides on new contract
                # discovery. fee_rate is a constant — no RTT to save there.
                cid = contract.get("condition_id", "")
                if cid and cid != self._prewarmed_condition_id:
                    self._prewarmed_condition_id = cid
                    for tok in (contract.get("token_id_up", ""),
                                contract.get("token_id_down", "")):
                        if not tok:
                            continue
                        task = asyncio.create_task(self.fetch_tick_size(tok, http_client))
                        self._prewarm_tasks.add(task)
                        task.add_done_callback(self._prewarm_tasks.discard)
                return contract

        return None

