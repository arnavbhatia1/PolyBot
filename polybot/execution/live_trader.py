"""Live trader — real Polymarket CLOB orders via py-clob-client SDK."""
import asyncio
import logging
import os

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from polybot.db.models import Database
from polybot.execution.base import TradeResult
from polybot.execution.paper_trader import (
    DEFAULT_FEE_RATE,
    entry_fee_shares,
    exit_fee_usdc,
)
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)

# Poll interval and timeout for fill confirmation
_FILL_POLL_INTERVAL = 0.5  # seconds
_FILL_TIMEOUT = 5.0  # seconds


def _create_clob_client() -> ClobClient:
    """Create and authenticate a ClobClient from env vars. Raises on failure."""
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        raise ValueError("Missing required secret: POLYMARKET_PRIVATE_KEY")
    funder = os.environ.get("POLYMARKET_FUNDER", "")

    client = ClobClient(
        "https://clob.polymarket.com",
        key=private_key,
        chain_id=137,
        signature_type=2,  # GNOSIS_SAFE — proxy wallet deployed via Polymarket
        funder=funder,    # NOTE: if auth fails, try signature_type=1 (POLY_PROXY)
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


def _get_balance_usd(client: ClobClient) -> float:
    """Fetch USDC balance from Polymarket. Returns float in dollars."""
    result = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    return int(result.get("balance", "0")) / 1e6


def verify_auth() -> tuple[bool, str, float]:
    """Verify Polymarket auth and return (ok, message, balance).

    Used by verify_keys.py and main.py preflight check.
    """
    try:
        client = _create_clob_client()
    except ValueError as e:
        return False, str(e), 0.0
    except Exception as e:
        return False, f"Auth failed — check POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER: {e}", 0.0

    try:
        balance = _get_balance_usd(client)
    except Exception as e:
        return False, f"Authenticated but balance fetch failed: {e}", 0.0

    msg = f"Authenticated OK, USDC balance: ${balance:,.2f}"
    if balance < 1.0:
        msg += " — WARNING: low balance, deposit USDC on Polymarket before trading"
    return True, msg, balance


class LiveTrader:
    """Real Polymarket CLOB trading. Same interface as PaperTrader."""

    def __init__(self, db: Database, **kwargs):
        self.db = db
        self.max_slippage = kwargs.get("max_slippage", 0.02)
        self.max_bankroll_deployed = kwargs.get("max_bankroll_deployed", 0.80)
        self.max_concurrent_positions = kwargs.get("max_concurrent_positions", 1)

        self.client = _create_clob_client()
        logger.info("LiveTrader authenticated with Polymarket CLOB")

    async def get_balance(self) -> float:
        """Fetch USDC balance from Polymarket. Returns float in dollars."""
        return _get_balance_usd(self.client)

    async def _get_deployed_capital(self) -> float:
        positions = await self.db.get_open_positions()
        return sum(p["size"] for p in positions)

    async def open_trade(
        self, market_id: str, question: str, side: str, price: float,
        size: float, signal_score: float, signal_strength: str,
        ev_at_entry: float, exit_target: float, stop_loss: float,
        weight_version: str, indicator_snapshot: str = "",
        token_id: str = "", fee_rate: float = DEFAULT_FEE_RATE,
    ) -> TradeResult:
        # --- Rejection gates (identical to PaperTrader) ---
        if await self.db.has_position_for_market(market_id):
            return TradeResult(success=False, reason="Duplicate market — already have position")
        if await self.db.get_open_position_count() >= self.max_concurrent_positions:
            return TradeResult(success=False, reason="Max positions reached")
        bankroll = await self.db.get_bankroll()
        deployed = await self._get_deployed_capital()
        max_deployable = bankroll * self.max_bankroll_deployed
        if deployed + size > max_deployable:
            return TradeResult(success=False, reason=f"Bankroll limit — deployed {deployed:.2f}, max {max_deployable:.2f}")

        # --- Build, sign, and submit order ---
        # Uses GTC + poll + cancel pattern (FOK not reliably supported by CLOB SDK)
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)
            order_id = resp.get("orderID", "")
            logger.info("Order submitted: %s (market=%s, price=%.2f, size=%.2f)",
                         order_id, market_id, price, size)
        except Exception as e:
            logger.error("Order submission failed: %s", e)
            return TradeResult(success=False, reason=f"Order submission failed: {e}")

        # --- Poll for fill ---
        elapsed = 0.0
        fill_price = None
        fill_size = None
        while elapsed < _FILL_TIMEOUT:
            order_status = self.client.get_order(order_id)
            status = order_status.get("status", "")

            if status == "MATCHED":
                trades = order_status.get("associate_trades", [])
                if trades:
                    fill_price = float(trades[0]["price"])
                    fill_size = float(trades[0]["size"])
                break

            if status in ("CANCELLED", "EXPIRED"):
                logger.warning("Order %s ended with status %s", order_id, status)
                return TradeResult(success=False, reason=f"Order {status.lower()}")

            await asyncio.sleep(_FILL_POLL_INTERVAL)
            elapsed += _FILL_POLL_INTERVAL

        # Timeout — cancel and bail
        if fill_price is None:
            logger.warning("Order %s timed out after %.1fs — cancelling", order_id, _FILL_TIMEOUT)
            try:
                self.client.cancel(order_id)
            except Exception:
                pass  # Order may already be gone
            return TradeResult(success=False, reason="Order not filled within timeout")

        # --- Fee math (identical to PaperTrader) ---
        shares_ordered = fill_size / fill_price
        fee_in_shares = entry_fee_shares(shares_ordered, fill_price, fee_rate)
        shares_received = shares_ordered - fee_in_shares

        # --- Persist to DB ---
        pos_id = await self.db.open_position(
            market_id=market_id, question=question, side=side,
            entry_price=fill_price, size=fill_size, signal_score=signal_score,
            signal_strength=signal_strength, ev_at_entry=ev_at_entry,
            exit_target=exit_target, stop_loss=stop_loss,
            weight_version=weight_version,
            indicator_snapshot=indicator_snapshot,
            fee_rate=fee_rate, shares_held=shares_received,
        )
        await self.db.set_bankroll(bankroll - fill_size)
        logger.info("Position opened: id=%d, market=%s, shares=%.4f (fee=%.4f shares deducted)",
                     pos_id, market_id, shares_received, fee_in_shares)
        return TradeResult(success=True, position_id=pos_id)

    async def close_trade(
        self, position_id: int, exit_price: float, token_id: str = "",
    ) -> TradeResult:
        # --- Fetch position from DB ---
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False, reason=f"Position {position_id} not found or already closed")

        # --- Shares and fee rate from position (same as PaperTrader) ---
        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE

        # --- Build, sign, and submit SELL order ---
        try:
            order_args = OrderArgs(
                token_id=token_id or position.get("token_id", ""),
                price=exit_price,
                size=shares * exit_price,
                side=SELL,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)
            order_id = resp.get("orderID", "")
            logger.info("Sell order submitted: %s (position=%d, price=%.2f, shares=%.4f)",
                         order_id, position_id, exit_price, shares)
        except Exception as e:
            logger.error("Sell order submission failed: %s", e)
            return TradeResult(success=False, reason=f"Sell order failed: {e}")

        # --- Poll for fill ---
        elapsed = 0.0
        fill_price = None
        while elapsed < _FILL_TIMEOUT:
            order_status = self.client.get_order(order_id)
            status = order_status.get("status", "")

            if status == "MATCHED":
                trades = order_status.get("associate_trades", [])
                if trades:
                    fill_price = float(trades[0]["price"])
                break

            if status in ("CANCELLED", "EXPIRED"):
                logger.warning("Sell order %s ended with status %s", order_id, status)
                return TradeResult(success=False, reason=f"Sell order {status.lower()}")

            await asyncio.sleep(_FILL_POLL_INTERVAL)
            elapsed += _FILL_POLL_INTERVAL

        # Timeout — cancel and bail
        if fill_price is None:
            logger.warning("Sell order %s timed out after %.1fs — cancelling", order_id, _FILL_TIMEOUT)
            try:
                self.client.cancel(order_id)
            except Exception:
                pass  # Order may already be gone
            return TradeResult(success=False, reason="Sell order not filled within timeout")

        actual_exit_price = fill_price

        # --- Fee math and revenue (identical to PaperTrader) ---
        lr = log_return(position["entry_price"], actual_exit_price)
        fee_usdc = exit_fee_usdc(shares, actual_exit_price, fee_rate)
        revenue = shares * actual_exit_price - fee_usdc

        # --- Persist to DB ---
        await self.db.close_position(position_id, exit_price=actual_exit_price, log_return=lr)
        bankroll = await self.db.get_bankroll()
        await self.db.set_bankroll(bankroll + revenue)
        logger.info("Position closed: id=%d, exit=%.4f, log_return=%.4f, revenue=%.4f",
                     position_id, actual_exit_price, lr, revenue)
        return TradeResult(success=True, position_id=position_id, log_return=lr)

    async def resolve_position(self, position_id: int, exit_price: float) -> TradeResult:
        """Resolution: Polymarket auto-credits USDC. No CLOB order needed."""
        # --- Fetch position from DB ---
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False, reason=f"Position {position_id} not found or already closed")

        # --- Compute log return and close in DB ---
        lr = log_return(position["entry_price"], exit_price)
        await self.db.close_position(position_id, exit_price=exit_price, log_return=lr)

        # --- Sync bankroll with real Polymarket balance ---
        real_balance = await self.get_balance()
        await self.db.set_bankroll(real_balance)
        logger.info("Position resolved: id=%d, exit=%.2f, log_return=%.4f, synced bankroll=%.2f",
                     position_id, exit_price, lr, real_balance)
        return TradeResult(success=True, position_id=position_id, log_return=lr)
