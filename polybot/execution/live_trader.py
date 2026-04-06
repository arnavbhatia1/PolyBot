"""Live trader for Polymarket US API.

Same interface as PaperTrader — the trading loop doesn't know the difference.
Uses FOK market orders via the US API with Ed25519 auth.
DB still tracks positions so Discord, learning pipeline, and bankroll all work.
"""

import asyncio
import logging

from polybot.db.models import Database
from polybot.execution.base import TradeResult
from polybot.execution.polymarket_us import PolymarketUSClient
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)

# Side → intent mapping
ENTRY_INTENT = {"Up": "ORDER_INTENT_BUY_LONG", "Down": "ORDER_INTENT_BUY_SHORT"}
EXIT_INTENT = {"Up": "ORDER_INTENT_SELL_LONG", "Down": "ORDER_INTENT_SELL_SHORT"}

# Order states that mean "filled"
FILLED_STATES = {"ORDER_STATE_FILLED", "FILLED", "matched", "MATCHED"}


class LiveTrader:
    """Executes real trades on Polymarket US via Ed25519-authenticated API.

    Same interface as PaperTrader. The trading loop calls open_trade/close_trade
    identically regardless of mode.
    """

    def __init__(self, db: Database, us_client: PolymarketUSClient,
                 max_slippage: float = 0.02,
                 max_bankroll_deployed: float = 0.80,
                 max_concurrent_positions: int = 1):
        self.db = db
        self.us_client = us_client
        self.max_slippage = max_slippage
        self.max_bankroll_deployed = max_bankroll_deployed
        self.max_concurrent_positions = max_concurrent_positions

    async def get_balance(self) -> float:
        """Fetch USD balance from Polymarket US."""
        return await self.us_client.get_balance()

    async def open_trade(self, market_id, question, side, price, size,
                         signal_score, signal_strength, ev_at_entry,
                         exit_target, stop_loss, weight_version,
                         indicator_snapshot: str = "",
                         token_id: str = "") -> TradeResult:
        """Place a market buy order on Polymarket US."""
        if await self.db.has_position_for_market(market_id):
            return TradeResult(success=False, reason="Duplicate market — already have position")

        if await self.db.get_open_position_count() >= self.max_concurrent_positions:
            return TradeResult(success=False, reason="Max positions reached")

        # Use live balance for sizing
        balance = await self.get_balance()
        if balance <= 0:
            return TradeResult(success=False, reason=f"No balance ({balance})")

        deployed = await self._get_deployed_capital()
        max_deployable = balance * self.max_bankroll_deployed
        if deployed + size > max_deployable:
            return TradeResult(success=False,
                               reason=f"Bankroll limit — deployed {deployed:.2f}, max {max_deployable:.2f}")

        # Map side to US API intent
        intent = ENTRY_INTENT.get(side)
        if not intent:
            return TradeResult(success=False, reason=f"Unknown side: {side}")

        # Compute share quantity from USD size
        quantity = max(1, int(size / price))

        # Place FOK market order (retry once on failure)
        resp = None
        last_error = None
        for attempt in range(2):
            try:
                resp = await self.us_client.place_order(
                    market_slug=market_id, intent=intent,
                    price=price, quantity=quantity)

                state = resp.get("state", resp.get("status", ""))
                if state in FILLED_STATES:
                    break
                last_error = f"Order state: {state} — {resp}"
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Order attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                await asyncio.sleep(0.5)

        # Check if filled
        state = (resp or {}).get("state", (resp or {}).get("status", ""))
        if state not in FILLED_STATES:
            logger.warning(f"Order not filled after retries: {last_error}")
            return TradeResult(success=False, reason=f"Order not filled: {last_error}")

        # Extract fill price (use order price as fallback)
        fill_price = self._extract_fill_price(resp, price)

        # Slippage check (warning only — order already filled)
        slippage = abs(fill_price - price)
        if slippage > self.max_slippage:
            logger.warning(f"Slippage: expected {price:.4f}, got {fill_price:.4f} ({slippage:.4f})")

        # Record in DB (keeps Discord/learning pipeline working)
        actual_size = quantity * fill_price  # Actual USD deployed
        pos_id = await self.db.open_position(
            market_id=market_id, question=question, side=side,
            entry_price=fill_price, size=actual_size, signal_score=signal_score,
            signal_strength=signal_strength, ev_at_entry=ev_at_entry,
            exit_target=exit_target, stop_loss=stop_loss,
            weight_version=weight_version, indicator_snapshot=indicator_snapshot,
        )

        # Sync DB bankroll with live balance
        new_balance = await self.get_balance()
        await self.db.set_bankroll(new_balance)

        logger.info(f"LIVE BUY {side} @ {fill_price:.4f} | ${actual_size:.2f} | qty={quantity}")
        return TradeResult(success=True, position_id=pos_id)

    async def close_trade(self, position_id: int, exit_price: float,
                          token_id: str = "") -> TradeResult:
        """Sell position on Polymarket US."""
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False,
                               reason=f"Position {position_id} not found or already closed")

        # Map side to sell intent
        intent = EXIT_INTENT.get(position["side"])
        if not intent:
            return TradeResult(success=False, reason=f"Unknown side: {position['side']}")

        shares = max(1, int(position["size"] / position["entry_price"]))

        # Place FOK sell order (retry once)
        resp = None
        last_error = None
        for attempt in range(2):
            try:
                resp = await self.us_client.close_position(
                    market_slug=position["market_id"], intent=intent,
                    price=exit_price, quantity=shares)

                state = resp.get("state", resp.get("status", ""))
                if state in FILLED_STATES:
                    break
                last_error = f"Sell state: {state} — {resp}"
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Sell attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                await asyncio.sleep(0.5)

        state = (resp or {}).get("state", (resp or {}).get("status", ""))
        if state not in FILLED_STATES:
            logger.warning(f"Sell not filled after retries: {last_error}")
            return TradeResult(success=False, reason=f"Sell not filled: {last_error}")

        fill_price = self._extract_fill_price(resp, exit_price)
        lr = log_return(position["entry_price"], fill_price)

        # Close in DB
        await self.db.close_position(position_id, exit_price=fill_price, log_return=lr)

        # Sync balance
        new_balance = await self.get_balance()
        await self.db.set_bankroll(new_balance)

        logger.info(f"LIVE SELL {position['side']} @ {fill_price:.4f} | lr={lr:.4f}")
        return TradeResult(success=True, position_id=position_id, log_return=lr)

    async def _get_deployed_capital(self) -> float:
        positions = await self.db.get_open_positions()
        return sum(p["size"] for p in positions)

    def _extract_fill_price(self, resp: dict, fallback: float) -> float:
        """Parse average fill price from API response."""
        try:
            # Check for fills/trades in the response
            fills = (resp.get("fills", []) or resp.get("trades", [])
                     or resp.get("matched_trades", []))
            if fills:
                total_qty = sum(float(f.get("quantity", f.get("size", 0))) for f in fills)
                if total_qty > 0:
                    weighted = sum(
                        float(f.get("price", {}).get("value", f.get("price", 0)))
                        * float(f.get("quantity", f.get("size", 0)))
                        for f in fills
                    )
                    return weighted / total_qty
            # Check for a top-level averagePrice or price field
            avg = resp.get("averagePrice", resp.get("avgPrice"))
            if avg:
                return float(avg)
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        return fallback
