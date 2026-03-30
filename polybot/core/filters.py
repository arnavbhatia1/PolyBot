class MarketFilter:
    def __init__(self, min_volume_24h, min_liquidity, min_days_to_expiry, max_days_to_expiry, max_spread, category_whitelist, category_blacklist):
        self.min_volume_24h = min_volume_24h
        self.min_liquidity = min_liquidity
        self.min_days_to_expiry = min_days_to_expiry
        self.max_days_to_expiry = max_days_to_expiry
        self.max_spread = max_spread
        self.category_whitelist = category_whitelist
        self.category_blacklist = category_blacklist

    def passes(self, market: dict) -> bool:
        if market.get("volume_24h", 0) < self.min_volume_24h:
            return False
        if market.get("liquidity", 0) < self.min_liquidity:
            return False
        days = market.get("days_to_expiry", 0)
        if days < self.min_days_to_expiry or days > self.max_days_to_expiry:
            return False
        if market.get("spread", 1.0) > self.max_spread:
            return False
        category = market.get("category", "")
        if self.category_blacklist and category in self.category_blacklist:
            return False
        if self.category_whitelist and category not in self.category_whitelist:
            return False
        return True

    def filter_batch(self, markets: list[dict]) -> list[dict]:
        return [m for m in markets if self.passes(m)]

    def update(self, param: str, value):
        if hasattr(self, param):
            setattr(self, param, value)
        else:
            raise ValueError(f"Unknown filter param: {param}")
