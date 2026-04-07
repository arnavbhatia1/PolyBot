import json
import logging
import time
from datetime import datetime, timezone
import httpx

logger = logging.getLogger(__name__)

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
                 min_book_depth_usd: float = 50.0):
        self.entry_window_seconds = entry_window_seconds
        self.min_time_remaining = min_time_remaining
        self.cache_seconds = cache_seconds
        self.symbol = symbol
        self.min_book_depth_usd = min_book_depth_usd
        self._cached_contract = None
        self._cache_time = 0
        self._book_cache: dict[str, tuple[float, dict]] = {}  # token_id -> (timestamp, book)
        self._book_cache_seconds = 2

    def _current_window_ts(self) -> int:
        return int(time.time() // self.WINDOW_SECONDS) * self.WINDOW_SECONDS

    def _make_slug(self, window_ts: int) -> str:
        return f"{self.symbol}-updown-5m-{window_ts}"

    def parse_contract(self, event: dict) -> dict | None:
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
        }

    def in_entry_window(self, seconds_remaining: float) -> bool:
        seconds_elapsed = self.WINDOW_SECONDS - seconds_remaining
        return (seconds_elapsed <= self.entry_window_seconds and
                seconds_remaining >= self.min_time_remaining)

    # --- Polymarket CLOB API (real order book, no auth required) ---

    async def fetch_clob_book(self, token_id: str, http_client=None) -> dict:
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
            if http_client:
                resp = await http_client.get(url, params={"token_id": token_id})
            else:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(url, params={"token_id": token_id})
            resp.raise_for_status()
            book = resp.json()
            self._book_cache[token_id] = (now, book)
            return book
        except Exception as e:
            logger.debug(f"CLOB book failed for {token_id}: {e}")
            return {}

    @staticmethod
    def clob_best_ask(book: dict) -> tuple[float, float]:
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
    def clob_best_bid(book: dict) -> tuple[float, float]:
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
    def clob_walk_asks(book: dict, shares_needed: float) -> float:
        """Walk ask levels to compute VWAP buy price for shares_needed shares.

        Asks are sorted price ascending (cheapest first).
        Returns 0.0 if the book cannot fill 90%+ of the order.
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
        if filled < shares_needed * 0.90:
            return 0.0
        return cost / filled

    @staticmethod
    def clob_walk_bids(book: dict, shares_needed: float) -> float:
        """Walk bid levels to compute VWAP sell price for shares_needed shares.

        Bids are sorted price descending (highest first).
        Returns 0.0 if the book cannot fill 90%+ of the order.
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
        if filled < shares_needed * 0.90:
            return 0.0
        return proceeds / filled

    async def find_active_contract(self) -> dict | None:
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
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(f"{self.GAMMA_API}/events",
                                            params={"slug": slug})
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
