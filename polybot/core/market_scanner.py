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
    """

    GAMMA_API = "https://gamma-api.polymarket.com"
    WINDOW_SECONDS = 300  # 5 minutes

    def __init__(self, entry_window_seconds: int = 120, min_time_remaining: int = 30,
                 cache_seconds: int = 5, symbol: str = "btc"):
        self.entry_window_seconds = entry_window_seconds
        self.min_time_remaining = min_time_remaining
        self.cache_seconds = cache_seconds
        self.symbol = symbol
        self._cached_contract = None
        self._cache_time = 0

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
            "active": event.get("active", False),
        }

    def in_entry_window(self, seconds_remaining: float) -> bool:
        seconds_elapsed = self.WINDOW_SECONDS - seconds_remaining
        return (seconds_elapsed <= self.entry_window_seconds and
                seconds_remaining >= self.min_time_remaining)

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
                logger.info(f"Found active contract: {contract['question']} "
                           f"({contract['seconds_remaining']:.0f}s remaining)")
                return contract

        return None
