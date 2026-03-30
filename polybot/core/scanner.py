import logging
from datetime import datetime, timezone
from polybot.core.filters import MarketFilter

logger = logging.getLogger(__name__)

class MarketScanner:
    CLOB_BASE_URL = "https://clob.polymarket.com"

    def __init__(self, filter: MarketFilter, max_markets: int = 100):
        self.filter = filter
        self.max_markets = max_markets

    async def _fetch_raw_markets(self) -> list[dict]:
        import httpx
        markets = []
        next_cursor = None
        async with httpx.AsyncClient(timeout=30) as client:
            while len(markets) < self.max_markets:
                params = {"limit": 100, "active": True, "closed": False}
                if next_cursor:
                    params["next_cursor"] = next_cursor
                resp = await client.get(f"{self.CLOB_BASE_URL}/markets", params=params)
                resp.raise_for_status()
                data = resp.json()
                batch = data if isinstance(data, list) else data.get("data", [])
                if not batch:
                    break
                markets.extend(batch)
                next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
                if not next_cursor:
                    break
        return markets[:self.max_markets]

    def normalize_market(self, raw: dict) -> dict:
        tokens = raw.get("tokens", [])
        price_yes = 0.0
        price_no = 0.0
        token_id_yes = ""
        token_id_no = ""
        for token in tokens:
            outcome = token.get("outcome", "").lower()
            if outcome == "yes":
                price_yes = float(token.get("price", 0))
                token_id_yes = token.get("token_id", "")
            elif outcome == "no":
                price_no = float(token.get("price", 0))
                token_id_no = token.get("token_id", "")
        end_date_str = raw.get("end_date_iso", "")
        days_to_expiry = 0
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_to_expiry = max(0, (end_date - datetime.now(timezone.utc)).days)
            except ValueError:
                days_to_expiry = 0
        spread = abs(price_yes - (1.0 - price_no)) if price_yes and price_no else float(raw.get("spread", "0.99"))
        return {
            "condition_id": raw.get("condition_id", ""),
            "question": raw.get("question", ""),
            "price_yes": price_yes,
            "price_no": price_no,
            "token_id_yes": token_id_yes,
            "token_id_no": token_id_no,
            "volume_24h": float(raw.get("volume_num_fmt", "0").replace(",", "")),
            "liquidity": float(raw.get("liquidity_num_fmt", "0").replace(",", "")),
            "spread": float(raw.get("spread", spread)),
            "days_to_expiry": days_to_expiry,
            "category": raw.get("category", ""),
            "end_date": end_date_str,
            "active": raw.get("active", False),
        }

    async def fetch_and_filter(self) -> list[dict]:
        raw_markets = await self._fetch_raw_markets()
        normalized = [self.normalize_market(m) for m in raw_markets]
        filtered = self.filter.filter_batch(normalized)
        logger.info(f"Scanned {len(raw_markets)} markets, {len(filtered)} passed filters")
        return filtered
