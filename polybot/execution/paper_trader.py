import logging
from polybot.db.models import Database
from polybot.execution.base import TradeResult
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)


TAKER_FEE_THETA = 0.05  # Polymarket US taker fee coefficient (effective 2026-04-03)


def _taker_fee(shares: float, price: float) -> float:
    """Polymarket fee: Θ × shares × p × (1-p). Zero at extremes (resolution), max at p=0.50."""
    return round(TAKER_FEE_THETA * shares * price * (1.0 - price), 2)


class PaperTrader:
    def __init__(self, db: Database, max_slippage=0.02, max_bankroll_deployed=0.80, max_concurrent_positions=1):
        self.db = db
        self.max_slippage = max_slippage
        self.max_bankroll_deployed = max_bankroll_deployed
        self.max_concurrent_positions = max_concurrent_positions

    async def _get_deployed_capital(self) -> float:
        positions = await self.db.get_open_positions()
        return sum(p["size"] for p in positions)

    async def open_trade(self, market_id, question, side, price, size, signal_score,
                         signal_strength, ev_at_entry, exit_target, stop_loss, weight_version,
                         indicator_snapshot: str = "", token_id: str = "") -> TradeResult:
        if await self.db.has_position_for_market(market_id):
            return TradeResult(success=False, reason="Duplicate market — already have position")
        if await self.db.get_open_position_count() >= self.max_concurrent_positions:
            return TradeResult(success=False, reason="Max positions reached")
        bankroll = await self.db.get_bankroll()
        deployed = await self._get_deployed_capital()
        max_deployable = bankroll * self.max_bankroll_deployed
        if deployed + size > max_deployable:
            return TradeResult(success=False, reason=f"Bankroll limit — deployed {deployed:.2f}, max {max_deployable:.2f}")
        shares = size / price
        entry_fee = _taker_fee(shares, price)
        pos_id = await self.db.open_position(
            market_id=market_id, question=question, side=side,
            entry_price=price, size=size, signal_score=signal_score,
            signal_strength=signal_strength, ev_at_entry=ev_at_entry, exit_target=exit_target,
            stop_loss=stop_loss, weight_version=weight_version,
            indicator_snapshot=indicator_snapshot,
        )
        await self.db.set_bankroll(bankroll - size - entry_fee)
        return TradeResult(success=True, position_id=pos_id)

    async def resolve_position(self, position_id: int, exit_price: float) -> TradeResult:
        """Resolution: no sell order, no exit fee (price is 1.0 or 0.0, fee formula gives $0)."""
        return await self.close_trade(position_id, exit_price)

    async def close_trade(self, position_id: int, exit_price: float, token_id: str = "") -> TradeResult:
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False, reason=f"Position {position_id} not found or already closed")
        lr = log_return(position["entry_price"], exit_price)
        shares = position["size"] / position["entry_price"]
        exit_fee = _taker_fee(shares, exit_price)
        revenue = shares * exit_price - exit_fee
        await self.db.close_position(position_id, exit_price=exit_price, log_return=lr)
        bankroll = await self.db.get_bankroll()
        await self.db.set_bankroll(bankroll + revenue)
        return TradeResult(success=True, position_id=position_id, log_return=lr)
