class DecisionTable:
    def __init__(self, ev_threshold=0.05, kelly_fraction=0.25, entry_discount=0.85, exit_target=0.90, stop_loss_pct=0.15):
        self.ev_threshold = ev_threshold
        self.kelly_fraction = kelly_fraction
        self.entry_discount = entry_discount
        self.exit_target = exit_target
        self.stop_loss_pct = stop_loss_pct
        self.table: dict[int, dict] = {}

    def build(self):
        self.table = {}
        for cents in range(1, 100):
            prob = cents / 100.0
            max_buy = prob * self.entry_discount
            exit_price = prob * self.exit_target
            odds = (1.0 - max_buy) / max_buy if max_buy > 0 else 0
            q = 1.0 - prob
            if odds > 0:
                kelly_raw = (prob * odds - q) / odds
                kelly = max(0.0, kelly_raw * self.kelly_fraction)
            else:
                kelly = 0.0
            self.table[cents] = {
                "probability": prob,
                "max_buy_price": round(max_buy, 4),
                "exit_price": round(exit_price, 4),
                "kelly_fraction": round(kelly, 6),
            }

    def lookup(self, probability: float) -> dict:
        cents = max(1, min(99, round(probability * 100)))
        return self.table[cents]

    def calculate_ev(self, probability: float, market_price: float) -> float:
        profit = 1.0 - market_price
        loss = market_price
        return probability * profit - (1.0 - probability) * loss

    def should_buy(self, probability: float, market_price: float) -> bool:
        decision = self.lookup(probability)
        ev = self.calculate_ev(probability, market_price)
        return market_price <= decision["max_buy_price"] and ev >= self.ev_threshold

    def should_exit(self, probability: float, market_price: float) -> bool:
        decision = self.lookup(probability)
        return market_price >= decision["exit_price"]

    def should_stop_loss(self, entry_price: float, market_price: float) -> bool:
        return market_price <= entry_price * (1.0 - self.stop_loss_pct)

    def position_size(self, probability: float, market_price: float, bankroll: float) -> float:
        decision = self.lookup(probability)
        return round(bankroll * decision["kelly_fraction"], 2)
