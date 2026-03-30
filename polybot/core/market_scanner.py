import logging
import time
from datetime import datetime, timezone
import httpx

logger = logging.getLogger(__name__)

class BTCMarketScanner:
    CLOB_BASE_URL = "https://clob.polymarket.com"

    def __init__(self, entry_window_seconds: int = 120, min_time_remaining: int = 30, cache_seconds: int = 5):
        self.entry_window_seconds = entry_window_seconds
        self.min_time_remaining = min_time_remaining
        self.cache_seconds = cache_seconds
        self._cached_contract = None
        self._cache_time = 0

    def is_btc_5min_market(self, market: dict) -> bool:
        question = market.get("question", "").lower()
        category = market.get("category", "").lower()
        is_btc = "btc" in question or "bitcoin" in question
        is_crypto = "crypto" in category
        is_short = any(term in question for term in ["5 min", "5min", "5-min", ":05", ":10", ":15", ":20", ":25", ":30", ":35", ":40", ":45", ":50", ":55", ":00"])
        return is_btc and (is_crypto or is_short)

    def parse_contract(self, market: dict) -> dict:
        tokens = market.get("tokens", [])
        price_yes = price_no = 0.0
        token_id_yes = token_id_no = ""
        for token in tokens:
            outcome = token.get("outcome", "").lower()
            if outcome == "yes":
                price_yes = float(token.get("price", 0))
                token_id_yes = token.get("token_id", "")
            elif outcome == "no":
                price_no = float(token.get("price", 0))
                token_id_no = token.get("token_id", "")
        end_date_str = market.get("end_date_iso", "")
        seconds_remaining = 0
        if end_date_str:
            try:
                end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                seconds_remaining = max(0, (end - datetime.now(timezone.utc)).total_seconds())
            except ValueError:
                pass
        return {"condition_id": market.get("condition_id", ""), "question": market.get("question", ""),
                "price_yes": price_yes, "price_no": price_no, "token_id_yes": token_id_yes,
                "token_id_no": token_id_no, "seconds_remaining": seconds_remaining, "end_date": end_date_str}

    def in_entry_window(self, seconds_remaining: float) -> bool:
        contract_duration = 300
        seconds_elapsed = contract_duration - seconds_remaining
        return seconds_elapsed <= self.entry_window_seconds and seconds_remaining >= self.min_time_remaining

    async def find_active_contract(self) -> dict | None:
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
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.CLOB_BASE_URL}/markets",
                                        params={"active": True, "closed": False, "limit": 100})
                resp.raise_for_status()
                data = resp.json()
                markets = data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            return None
        for market in markets:
            if self.is_btc_5min_market(market):
                contract = self.parse_contract(market)
                if contract["seconds_remaining"] > self.min_time_remaining:
                    self._cached_contract = contract
                    self._cache_time = now
                    return contract
        return None
