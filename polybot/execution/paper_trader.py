import logging
from polybot.db.models import Database
from polybot.execution.base import TradeResult
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)


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
        pos_id = await self.db.open_position(
            market_id=market_id, question=question, side=side,
            entry_price=price, size=size, signal_score=signal_score,
            signal_strength=signal_strength, ev_at_entry=ev_at_entry, exit_target=exit_target,
            stop_loss=stop_loss, weight_version=weight_version,
            indicator_snapshot=indicator_snapshot,
        )
        await self.db.set_bankroll(bankroll - size)
        return TradeResult(success=True, position_id=pos_id)

    async def close_trade(self, position_id: int, exit_price: float, token_id: str = "") -> TradeResult:
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False, reason=f"Position {position_id} not found or already closed")
        lr = log_return(position["entry_price"], exit_price)
        shares = position["size"] / position["entry_price"]
        revenue = shares * exit_price
        await self.db.close_position(position_id, exit_price=exit_price, log_return=lr)
        bankroll = await self.db.get_bankroll()
        await self.db.set_bankroll(bankroll + revenue)
        return TradeResult(success=True, position_id=position_id, log_return=lr)
