from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _ensure_client(http_client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a usable httpx client. If `http_client` is None, open a short-lived one.

    Several callers (tests, ad-hoc scripts, agents constructed lazily) invoke the
    scanner without a long-lived pooled client. The methods used to assume a
    client was always passed and would AttributeError on .get(); this helper
    keeps the happy path (pooled client) free of overhead while still letting
    None callers succeed.
    """
    if http_client is not None:
        yield http_client
        return
    async with httpx.AsyncClient(timeout=10) as client:
        yield client

class BTCMarketScanner:
    """Discovers active 5-min BTC Up/Down markets on Polymarket via Gamma API.

    These markets use deterministic slugs based on Unix timestamps:
      btc-updown-5m-{window_ts}
    where window_ts is floored to the nearest 300-second boundary.
    Outcomes are "Up"/"Down" (not "Yes"/"No").

    IMPORTANT: Gamma API outcomePrices are stale/indicative — they reflect
    the last trade price or initial 50/50, NOT the live order book.
    Always use fetch_clob_book() for real bid/ask prices before trading.
    """

    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    WINDOW_SECONDS = 300  # 5 minutes

    def __init__(self, entry_window_seconds: int = 120, min_time_remaining: int = 30,
                 cache_seconds: int = 5, symbol: str = "btc",
                 min_book_depth_usd: float = 50.0) -> None:
        self.entry_window_seconds: int = entry_window_seconds
        self.min_time_remaining: int = min_time_remaining
        self.cache_seconds: int = cache_seconds
        self.symbol: str = symbol
        self.min_book_depth_usd: float = min_book_depth_usd
        self._cached_contract: dict[str, Any] | None = None
        self._cache_time: float = 0
        self._book_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # token_id -> (timestamp, book)
        self._book_cache_seconds: int = 2
        self._fee_rate_cache: dict[str, tuple[float, float]] = {}   # token_id -> (timestamp, rate)
        self._fee_rate_cache_seconds: int = 3600  # 1 hour — fee rates rarely change
        self._tick_size_cache: dict[str, tuple[float, str]] = {}    # token_id -> (timestamp, tick_size)
        self._tick_size_cache_seconds: int = 3600

    def _current_window_ts(self) -> int:
        return int(time.time() // self.WINDOW_SECONDS) * self.WINDOW_SECONDS

    def _make_slug(self, window_ts: int) -> str:
        return f"{self.symbol}-updown-5m-{window_ts}"

    def parse_contract(self, event: dict[str, Any]) -> dict[str, Any] | None:
        markets = event.get("markets", [])
        if not markets:
            return None
        market = markets[0]

        outcomes = market.get("outcomes", [])
        prices_raw = market.get("outcomePrices", [])
        clob_tokens_raw = market.get("clobTokenIds", [])

        # Parse JSON strings if needed
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
                f"tokens={len(clob_tokens_raw)}) — trading disabled for this contract"
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

        # Extract Chainlink oracle prices (populated after resolution)
        raw_meta = event.get("eventMetadata")
        event_metadata = None
        if raw_meta and isinstance(raw_meta, dict):
            ptb = raw_meta.get("priceToBeat")
            fp = raw_meta.get("finalPrice")
            if ptb is not None and fp is not None:
                event_metadata = {
                    "price_to_beat": float(ptb),
                    "final_price": float(fp),
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
        """Fetch full order book from Polymarket CLOB API.

        No auth required. Caches result for _book_cache_seconds (2s).
        Returns the raw book dict on success, or {} on failure.

        Book format:
          {
            "bids": [{"price": "0.45", "size": "200"}, ...],  # price desc
            "asks": [{"price": "0.55", "size": "150"}, ...],  # price asc
            "last_trade_price": "0.50",
            "tick_size": "0.01",
            "min_order_size": "5"
          }
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
        """Fetch taker fee rate from Polymarket CLOB API.

        Returns fee rate as a decimal (e.g., 0.072 for crypto).
        Caches for 1 hour. Falls back to 0.072 (crypto default) on error.
        """
        now = time.time()
        cached = self._fee_rate_cache.get(token_id)
        if cached and (now - cached[0]) < self._fee_rate_cache_seconds:
            return cached[1]

        try:
            url = f"{self.CLOB_API}/fee-rate"
            async with _ensure_client(http_client) as client:
                resp = await client.get(url, params={"token_id": token_id})
                resp.raise_for_status()
                data = resp.json()
            # CLOB base_fee is an internal multiplier, NOT the actual taker fee.
            # As of March 2026, Polymarket uses Dynamic Taker-Fee Model:
            #   Crypto: up to 1.8% peak (at p=0.50)
            #   Fee = rate × shares × p × (1-p)
            # The base_fee=1000 from the API is the internal scaling factor,
            # not basis points. Real crypto taker rate is 0.018 (1.8%).
            rate = 0.018
            self._fee_rate_cache[token_id] = (now, rate)
            return rate
        except Exception as e:
            logger.debug(f"Fee rate fetch failed for {token_id}: {e}")
            return 0.018  # Crypto taker fee: 1.8% peak (Dynamic Taker-Fee Model, March 2026)

    async def fetch_tick_size(self, token_id: str, http_client: httpx.AsyncClient | None = None) -> str:
        """Fetch tick size from Polymarket CLOB API.

        Returns tick size as a string (e.g., "0.01").
        Caches for 1 hour. Falls back to "0.01" on error.
        """
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
        """Round price down to nearest tick size increment.

        Polymarket requires prices to be multiples of tick_size and within
        [tick_size, 1 - tick_size]. Uses string-based precision to avoid
        floating-point drift.
        """
        tick = float(tick_size)
        if tick <= 0:
            return price
        # Round down to tick grid
        snapped = round(int(price / tick) * tick, 10)
        # Clamp to valid range
        min_price = tick
        max_price = round(1.0 - tick, 10)
        return max(min_price, min(snapped, max_price))

    @staticmethod
    def book_min_order_size(book: dict[str, Any]) -> float:
        """Extract min_order_size from a CLOB book response. Default 5."""
        return float(book.get("min_order_size", "5"))

    @staticmethod
    def clob_best_ask(book: dict[str, Any]) -> tuple[float, float]:
        """Return (best_ask_price, total_ask_depth) from a CLOB book dict.

        Asks are sorted price ascending — first entry is the best ask.
        Returns (0.0, 0.0) if book is empty or asks are missing.
        """
        asks = book.get("asks", [])
        if not asks:
            return (0.0, 0.0)
        best_price = float(asks[0]["price"])
        total_depth = sum(float(a["size"]) for a in asks)
        return (best_price, total_depth)

    @staticmethod
    def clob_best_bid(book: dict[str, Any]) -> tuple[float, float]:
        """Return (best_bid_price, total_bid_depth) from a CLOB book dict.

        Bids are sorted price descending — first entry is the best bid.
        Returns (0.0, 0.0) if book is empty or bids are missing.
        """
        bids = book.get("bids", [])
        if not bids:
            return (0.0, 0.0)
        best_price = float(bids[0]["price"])
        total_depth = sum(float(b["size"]) for b in bids)
        return (best_price, total_depth)

    @staticmethod
    def clob_walk_asks(book: dict[str, Any], shares_needed: float) -> float:
        """Walk ask levels to compute VWAP buy price for shares_needed shares.

        Asks are sorted price ascending (cheapest first).
        FOK semantics: returns 0.0 if the book cannot fill 100% of the order.
        """
        asks = book.get("asks", [])
        if not asks or shares_needed <= 0:
            return 0.0
        filled = 0.0
        cost = 0.0
        for level in asks:
            available = float(level["size"])
            take = min(available, shares_needed - filled)
            cost += take * float(level["price"])
            filled += take
            if filled >= shares_needed:
                break
        if filled < shares_needed:
            return 0.0
        return cost / filled

    @staticmethod
    def clob_walk_bids(book: dict[str, Any], shares_needed: float) -> float:
        """Walk bid levels to compute VWAP sell price for shares_needed shares.

        Bids are sorted price descending (highest first).
        FOK semantics: returns 0.0 if the book cannot fill 100% of the order.
        """
        bids = book.get("bids", [])
        if not bids or shares_needed <= 0:
            return 0.0
        filled = 0.0
        proceeds = 0.0
        for level in bids:
            available = float(level["size"])
            take = min(available, shares_needed - filled)
            proceeds += take * float(level["price"])
            filled += take
            if filled >= shares_needed:
                break
        if filled < shares_needed:
            return 0.0
        return proceeds / filled

    @staticmethod
    def clob_ask_depth(book: dict[str, Any]) -> float:
        """Total shares available on the ask side."""
        return sum(float(l["size"]) for l in book.get("asks", []))

    @staticmethod
    def clob_bid_depth(book: dict[str, Any]) -> float:
        """Total shares available on the bid side."""
        return sum(float(l["size"]) for l in book.get("bids", []))

    # --- NegRisk execution prices (accounts for cross-matching) ---

    async def fetch_market_price(self, token_id: str, side: str = "BUY",
                                  http_client: httpx.AsyncClient | None = None) -> float:
        """GET /price — actual execution price accounting for negRisk cross-matching.

        The raw token book (GET /book) only shows direct token orders.
        In negRisk binary markets, the CLOB engine cross-matches orders
        across complementary tokens, so the real executable price is often
        much better than the raw best ask/bid.

        Args:
            token_id: The token to price
            side: "BUY" or "SELL"

        Returns: execution price as float, or 0.0 on error.
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

    DATA_API = "https://data-api.polymarket.com"

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

        # Return cache if fresh
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

        # Try current window, then next window
        window_ts = self._current_window_ts()
        for ts in [window_ts, window_ts + self.WINDOW_SECONDS]:
            slug = self._make_slug(ts)
            try:
                if http_client:
                    resp = await http_client.get(f"{self.GAMMA_API}/events", params={"slug": slug})
                    resp.raise_for_status()
                    data = resp.json()
                else:
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(f"{self.GAMMA_API}/events", params={"slug": slug})
                        resp.raise_for_status()
                        data = resp.json()
            except Exception as e:
                logger.error(f"Gamma API error for {slug}: {e}")
                continue

            if not data:
                continue

            event = data[0] if isinstance(data, list) else data
            if not event.get("active", False):
                continue

            contract = self.parse_contract(event)
            if contract and contract["seconds_remaining"] > self.min_time_remaining:
                self._cached_contract = contract
                self._cache_time = now
                logger.debug(f"Found active contract: {contract['question']} "
                           f"({contract['seconds_remaining']:.0f}s remaining)")
                return contract

        return None

