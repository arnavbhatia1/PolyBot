import logging
from polybot.db.models import Database
from polybot.execution.base import TradeResult
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)


DEFAULT_FEE_RATE = 0.018  # Polymarket crypto taker fee: 1.8% peak (Dynamic Taker-Fee Model, March 2026)


def taker_fee(shares: float, price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """Polymarket fee: feeRate × shares × p × (1-p). Zero at extremes, max at p=0.50."""
    return round(fee_rate * shares * price * (1.0 - price), 6)


def entry_fee_shares(shares_ordered: float, price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """On buys, Polymarket collects fee in shares. Returns shares deducted."""
    fee_dollars = taker_fee(shares_ordered, price, fee_rate)
    return fee_dollars / price if price > 0 else 0.0


def exit_fee_usdc(shares: float, price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """On sells, Polymarket collects fee in USDC. Returns USDC deducted."""
    return taker_fee(shares, price, fee_rate)


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
                         indicator_snapshot: str = "", token_id: str = "",
                         fee_rate: float = DEFAULT_FEE_RATE) -> TradeResult:
        if await self.db.has_position_for_market(market_id):
            return TradeResult(success=False, reason="Duplicate market — already have position")
        if await self.db.get_open_position_count() >= self.max_concurrent_positions:
            return TradeResult(success=False, reason="Max positions reached")
        bankroll = await self.db.get_bankroll()
        deployed = await self._get_deployed_capital()
        max_deployable = bankroll * self.max_bankroll_deployed
        if deployed + size > max_deployable:
            return TradeResult(success=False, reason=f"Bankroll limit — deployed {deployed:.2f}, max {max_deployable:.2f}")

        # Polymarket collects entry fee in SHARES on buys.
        # You pay `size` USDC, receive fewer shares than size/price.
        shares_ordered = size / price
        fee_in_shares = entry_fee_shares(shares_ordered, price, fee_rate)
        shares_received = shares_ordered - fee_in_shares

        pos_id = await self.db.open_position(
            market_id=market_id, question=question, side=side,
            entry_price=price, size=size, signal_score=signal_score,
            signal_strength=signal_strength, ev_at_entry=ev_at_entry, exit_target=exit_target,
            stop_loss=stop_loss, weight_version=weight_version,
            indicator_snapshot=indicator_snapshot,
            fee_rate=fee_rate, shares_held=shares_received,
        )
        # Bankroll debit = USDC spent only (fee is in shares, not extra USDC)
        await self.db.set_bankroll(bankroll - size)
        return TradeResult(success=True, position_id=pos_id)

    async def resolve_position(self, position_id: int, exit_price: float) -> TradeResult:
        """Resolution: exit at $1 or $0. Fee formula gives $0 at extremes."""
        return await self.close_trade(position_id, exit_price)

    async def close_trade(self, position_id: int, exit_price: float, token_id: str = "") -> TradeResult:
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False, reason=f"Position {position_id} not found or already closed")
        lr = log_return(position["entry_price"], exit_price)

        # Use actual shares held (after entry fee deduction)
        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE

        # Polymarket collects exit fee in USDC on sells
        fee_usdc = exit_fee_usdc(shares, exit_price, fee_rate)
        revenue = shares * exit_price - fee_usdc

        await self.db.close_position(position_id, exit_price=exit_price, log_return=lr)
        bankroll = await self.db.get_bankroll()
        await self.db.set_bankroll(bankroll + revenue)
        return TradeResult(success=True, position_id=position_id, log_return=lr)
