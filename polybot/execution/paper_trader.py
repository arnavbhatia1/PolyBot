"""Paper trader — simulated fills with instant execution."""
from polybot.execution.base import BaseTrader, FillResult, DEFAULT_FEE_RATE, exit_fee_usdc


class PaperTrader(BaseTrader):
    """Simulated trading. Same logic as LiveTrader, instant fills."""

    async def _execute_buy(self, token_id: str, price: float, size: float) -> FillResult:
        """Instant fill at the given price."""
        return FillResult(filled=True, fill_price=price, fill_size=size)

    async def _execute_sell(self, token_id: str, shares: float, price: float) -> FillResult:
        """Instant fill at the given price."""
        return FillResult(filled=True, fill_price=price)

    async def _resolve_bankroll(self, position: dict, exit_price: float) -> float:
        """Compute revenue from shares. Fee is $0 at resolution extremes."""
        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE
        fee_usdc = exit_fee_usdc(shares, exit_price, fee_rate)
        revenue = shares * exit_price - fee_usdc
        bankroll = await self.db.get_bankroll()
        return bankroll + revenue
