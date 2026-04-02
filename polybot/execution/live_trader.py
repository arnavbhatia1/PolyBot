import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType,
)
from polybot.db.models import Database
from polybot.execution.base import TradeResult
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)

# Polymarket CLOB sides
BUY = "BUY"
SELL = "SELL"


class LiveTrader:
    """Executes real trades on Polymarket's CLOB via py-clob-client.

    Same interface as PaperTrader — the trading loop doesn't know the difference.
    Uses FOK (fill-or-kill) market orders for instant execution on 5-min contracts.

    DB still tracks positions so Discord commands, learning pipeline, and
    bankroll display all work identically to paper mode.
    """

    def __init__(self, db: Database, clob: ClobClient,
                 max_slippage: float = 0.02,
                 max_bankroll_deployed: float = 0.80,
                 max_concurrent_positions: int = 1):
        self.db = db
        self.clob = clob
        self.max_slippage = max_slippage
        self.max_bankroll_deployed = max_bankroll_deployed
        self.max_concurrent_positions = max_concurrent_positions

    async def get_usdc_balance(self) -> float:
        """Fetch USDC balance from Polymarket."""
        try:
            resp = self.clob.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return float(resp.get("balance", "0")) / 1e6  # USDC has 6 decimals
        except Exception as e:
            logger.error(f"Failed to fetch USDC balance: {e}")
            return 0.0

    async def open_trade(self, market_id, question, side, price, size,
                         signal_score, signal_strength, ev_at_entry,
                         exit_target, stop_loss, weight_version,
                         indicator_snapshot: str = "",
                         token_id: str = "") -> TradeResult:
        """Place a market buy order on Polymarket's CLOB.

        Args:
            token_id: The CLOB token ID for the side we're buying (Up or Down).
                      Must be passed by the trading loop from the contract data.
        """
        if not token_id:
            return TradeResult(success=False, reason="No token_id — cannot place live order")

        if await self.db.has_position_for_market(market_id):
            return TradeResult(success=False, reason="Duplicate market — already have position")

        if await self.db.get_open_position_count() >= self.max_concurrent_positions:
            return TradeResult(success=False, reason="Max positions reached")

        # Use on-chain USDC balance for sizing
        usdc_balance = await self.get_usdc_balance()
        if usdc_balance <= 0:
            return TradeResult(success=False, reason=f"No USDC balance ({usdc_balance})")

        max_deployable = usdc_balance * self.max_bankroll_deployed
        deployed = await self._get_deployed_capital()
        if deployed + size > max_deployable:
            return TradeResult(success=False,
                               reason=f"Bankroll limit — deployed {deployed:.2f}, max {max_deployable:.2f}")

        # Place FOK market buy order
        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=size,
                side=BUY,
                price=price,
            )
            signed_order = self.clob.create_market_order(order_args)
            resp = self.clob.post_order(signed_order, order_type=OrderType.FOK)
        except Exception as e:
            logger.error(f"CLOB order failed: {e}")
            return TradeResult(success=False, reason=f"CLOB error: {e}")

        # Check if the order was filled
        if not resp or resp.get("status") not in ("matched", "MATCHED"):
            logger.warning(f"Order not filled: {resp}")
            return TradeResult(success=False, reason=f"Order not filled: {resp}")

        # Extract fill details
        fill_price = self._extract_fill_price(resp, price)

        # Slippage check
        if abs(fill_price - price) > self.max_slippage:
            logger.warning(f"Slippage exceeded: expected {price:.4f}, got {fill_price:.4f}")
            # Order already filled — can't undo, but log the warning
            # In FOK this shouldn't happen if price was set correctly

        # Record in DB (keeps Discord/learning pipeline working)
        pos_id = await self.db.open_position(
            market_id=market_id, question=question, side=side,
            entry_price=fill_price, size=size, signal_score=signal_score,
            signal_strength=signal_strength, ev_at_entry=ev_at_entry,
            exit_target=exit_target, stop_loss=stop_loss,
            weight_version=weight_version, indicator_snapshot=indicator_snapshot,
        )

        # Update DB bankroll to reflect USDC balance
        new_balance = await self.get_usdc_balance()
        await self.db.set_bankroll(new_balance)

        logger.info(f"LIVE BUY filled: {side} @ {fill_price:.4f} | ${size:.2f} | token={token_id[:16]}...")
        return TradeResult(success=True, position_id=pos_id)

    async def close_trade(self, position_id: int, exit_price: float,
                          token_id: str = "") -> TradeResult:
        """Sell position on Polymarket's CLOB.

        Args:
            token_id: The CLOB token ID for the side we're holding.
                      Must be passed by the trading loop from the contract data.
        """
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False,
                               reason=f"Position {position_id} not found or already closed")

        if not token_id:
            return TradeResult(success=False, reason="No token_id — cannot place live sell order")

        shares = position["size"] / position["entry_price"]

        # Place FOK market sell order
        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(shares, 2),
                side=SELL,
                price=exit_price,
            )
            signed_order = self.clob.create_market_order(order_args)
            resp = self.clob.post_order(signed_order, order_type=OrderType.FOK)
        except Exception as e:
            logger.error(f"CLOB sell failed: {e}")
            return TradeResult(success=False, reason=f"CLOB sell error: {e}")

        if not resp or resp.get("status") not in ("matched", "MATCHED"):
            logger.warning(f"Sell not filled: {resp}")
            return TradeResult(success=False, reason=f"Sell not filled: {resp}")

        fill_price = self._extract_fill_price(resp, exit_price)
        lr = log_return(position["entry_price"], fill_price)

        # Close in DB
        await self.db.close_position(position_id, exit_price=fill_price, log_return=lr)

        # Sync DB bankroll with on-chain balance
        new_balance = await self.get_usdc_balance()
        await self.db.set_bankroll(new_balance)

        logger.info(f"LIVE SELL filled: {position['side']} @ {fill_price:.4f} | lr={lr:.4f}")
        return TradeResult(success=True, position_id=position_id, log_return=lr)

    async def _get_deployed_capital(self) -> float:
        positions = await self.db.get_open_positions()
        return sum(p["size"] for p in positions)

    def _extract_fill_price(self, resp: dict, fallback: float) -> float:
        """Parse average fill price from CLOB response."""
        try:
            # The CLOB response includes matched trades with prices
            trades = resp.get("trades", []) or resp.get("matched_trades", [])
            if trades:
                total_size = sum(float(t.get("size", 0)) for t in trades)
                if total_size > 0:
                    weighted = sum(float(t.get("price", 0)) * float(t.get("size", 0)) for t in trades)
                    return weighted / total_size
        except (ValueError, TypeError, KeyError):
            pass
        return fallback
