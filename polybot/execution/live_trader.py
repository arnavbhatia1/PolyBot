"""Live trader — real Polymarket CLOB orders via py-clob-client SDK."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from polybot.db.models import Database
from polybot.execution.base import BaseTrader, FillResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.5  # seconds, doubles each attempt
_NON_RETRYABLE_ERRORS = frozenset({
    "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    "MARKET_NOT_READY",
    "INVALID_ORDER_EXPIRATION",
})


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# LiveTrader
# ---------------------------------------------------------------------------

class LiveTrader(BaseTrader):
    """Real Polymarket CLOB trading with FOK market orders and retry."""

    def __init__(self, db: Database, **kwargs: Any) -> None:
        super().__init__(
            db=db,
            max_slippage=kwargs.get("max_slippage", 0.02),
            max_bankroll_deployed=kwargs.get("max_bankroll_deployed", 0.80),
            max_concurrent_positions=kwargs.get("max_concurrent_positions", 1),
        )
        self.client: ClobClient = _create_clob_client()
        self.use_maker_orders: bool = kwargs.get("use_maker_orders", False)
        self.maker_timeout_s: float = kwargs.get("maker_timeout_s", 60.0)
        logger.info("LiveTrader authenticated with Polymarket CLOB")

    async def get_balance(self) -> float:
        """Fetch USDC balance from Polymarket. Returns float in dollars."""
        return _get_balance_usd(self.client)

    async def _execute_buy(self, token_id: str, price: float, size: float) -> FillResult:
        """Buy: try maker limit order (0% fee) if enabled, else FOK (1.8% fee)."""
        if self.use_maker_orders:
            return await self._execute_buy_limit(token_id, price, size, self.maker_timeout_s)
        return await self._submit_fok_order(token_id, BUY, size, price)

    async def _execute_sell(self, token_id: str, shares: float, price: float) -> FillResult:
        """FOK market sell for `shares` shares."""
        return await self._submit_fok_order(token_id, SELL, shares, price)

    async def _resolve_bankroll(self, position: dict[str, Any], exit_price: float) -> float:
        """Sync bankroll with real Polymarket balance."""
        real_balance = await self.get_balance()
        logger.info("Resolution bankroll sync: real balance=%.2f", real_balance)
        return real_balance

    # -- Maker limit order with FOK fallback ---------------------------------

    async def _execute_buy_limit(
        self,
        token_id: str,
        price: float,
        size: float,
        timeout_s: float = 60.0,
    ) -> FillResult:
        """Post a maker limit buy (0% fee) with timeout fallback to FOK.

        Posts a limit order at *price*, polls for fill up to *timeout_s*
        seconds.  If the order fills, returns a maker FillResult.  If not
        filled after the timeout, cancels the resting order and falls back
        to the existing FOK market-order path.  Any exception at any stage
        triggers the same FOK fallback — this method never crashes the loop.

        Args:
            token_id: CLOB token ID.
            price: Limit price (per share).
            size: Order size in USDC.
            timeout_s: Seconds to wait for a maker fill before falling back.

        Returns:
            FillResult with fill details (reason="maker_fill") or FOK result.
        """
        try:
            # OrderArgs.size = shares, not USDC
            shares = size / price

            order = await asyncio.to_thread(self.client.create_order,
                order_args={
                    "token_id": token_id,
                    "price": price,
                    "size": shares,
                    "side": BUY,
                },
            )
            resp = await asyncio.to_thread(self.client.post_order, order)

            order_id = resp.get("orderID") or resp.get("id")
            if not order_id:
                logger.warning("Maker order: no order ID returned, falling back to FOK")
                return await self._execute_buy(token_id, price, size)

            logger.info(
                "Maker limit order posted: %s at $%.4f (%.4f shares)",
                order_id, price, shares,
            )

            # Poll for fill
            poll_interval = 2.0
            elapsed = 0.0
            while elapsed < timeout_s:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    order_status = await asyncio.to_thread(self.client.get_order, order_id)
                    status = order_status.get("status", "")
                    if status == "MATCHED":
                        fill_price = await self._get_fill_price(order_id, price)
                        logger.info(
                            "Maker order filled: %s at $%.4f", order_id, fill_price,
                        )
                        return FillResult(
                            filled=True,
                            fill_price=fill_price,
                            fill_size=size,
                            reason="maker_fill",
                        )
                    if status in ("CANCELLED", "EXPIRED"):
                        logger.info(
                            "Maker order %s, falling back to FOK", status,
                        )
                        return await self._execute_buy(token_id, price, size)
                except Exception as poll_err:
                    logger.warning("Maker poll error: %s", poll_err)

            # Timeout — cancel the resting order and fall back to FOK
            try:
                await asyncio.to_thread(self.client.cancel, order_id)
                logger.info(
                    "Maker order timed out after %.0fs, cancelled %s — falling back to FOK",
                    timeout_s, order_id,
                )
            except Exception as cancel_err:
                logger.warning("Failed to cancel maker order %s: %s", order_id, cancel_err)

            return await self._execute_buy(token_id, price, size)

        except Exception as e:
            logger.warning("Maker order flow failed: %s — falling back to FOK", e)
            return await self._execute_buy(token_id, price, size)

    # -- FOK order submission with retry ------------------------------------

    async def _submit_fok_order(
        self,
        token_id: str,
        side: str,
        amount: float,
        expected_price: float,
    ) -> FillResult:
        """Submit FOK market order with exponential-backoff retry.

        Args:
            token_id: CLOB token ID.
            side: BUY or SELL.
            amount: USDC for BUY, shares for SELL.
            expected_price: Used as fallback if fill price lookup fails.

        Returns:
            FillResult with fill details or failure reason.
        """
        last_error = ""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                mo = MarketOrderArgs(token_id=token_id, amount=amount, side=side)
                # Offload blocking SDK calls to thread pool — keeps event loop free
                signed = await asyncio.to_thread(self.client.create_market_order, mo)
                resp = await asyncio.to_thread(self.client.post_order, signed, OrderType.FOK)

                if not resp.get("success"):
                    error_msg = resp.get("errorMsg", "unknown error")
                    # Non-retryable errors bail immediately
                    if any(code in error_msg for code in _NON_RETRYABLE_ERRORS):
                        logger.error("Order rejected (non-retryable): %s", error_msg)
                        return FillResult(filled=False, reason=error_msg)
                    last_error = error_msg
                    logger.warning(
                        "FOK attempt %d/%d failed: %s", attempt, _MAX_RETRIES, error_msg,
                    )
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    continue

                if resp.get("status") == "matched":
                    order_id = resp.get("orderID", "")
                    fill_price = await self._get_fill_price(order_id, expected_price)
                    logger.info(
                        "FOK %s filled: order=%s, price=%.4f, amount=%.4f",
                        side, order_id, fill_price, amount,
                    )
                    return FillResult(
                        filled=True,
                        fill_price=fill_price,
                        fill_size=amount if side == BUY else 0.0,
                    )

                # Unexpected status
                last_error = f"Unexpected status: {resp.get('status')}"
                logger.warning(
                    "FOK attempt %d/%d: %s", attempt, _MAX_RETRIES, last_error,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    "FOK attempt %d/%d exception: %s", attempt, _MAX_RETRIES, e,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))

        return FillResult(
            filled=False,
            reason=f"Failed after {_MAX_RETRIES} attempts: {last_error}",
        )

    # -- Fill price lookup --------------------------------------------------

    async def _get_fill_price(self, order_id: str, fallback_price: float) -> float:
        """Fetch actual fill price via VWAP from associate_trades."""
        try:
            order = await asyncio.to_thread(self.client.get_order, order_id)
            trades = order.get("associate_trades", [])
            if not trades:
                return fallback_price
            total_shares = sum(float(t["size"]) for t in trades)
            if total_shares == 0:
                return fallback_price
            total_cost = sum(float(t["size"]) * float(t["price"]) for t in trades)
            return total_cost / total_shares
        except Exception as e:
            logger.warning("Failed to fetch fill price for %s: %s", order_id, e)
            return fallback_price
