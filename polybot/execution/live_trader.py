"""Live trader — real Polymarket CLOB orders via py-clob-client-v2 SDK.

py-clob-client v0.34.6 was hardcoded to v1 order structs and signed against the
v1 EIP-712 domain. After Polymarket migrated wallets to v2 contracts
(0xE111180000d2663C0091e4f400237545B87B996B regular,
0xe2222d279d744050d28e00520010520000310F59 NegRisk), every order POST returned
{"error": "order_version_mismatch"}. py-clob-client-v2 ships v2 order structs
(timestamp/metadata/builder; no expiration/nonce/feeRateBps) and the v2 domain.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderPayload,
    OrderType,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

from polybot.db.models import Database
from polybot.execution.base import BaseTrader, FillResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.5  # seconds, doubles each attempt
_MIN_ORDER_USD = 1.0  # Polymarket CLOB rejects marketable orders below $1 notional
_NON_RETRYABLE_ERRORS = frozenset({
    "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    "MARKET_NOT_READY",
    "INVALID_ORDER_EXPIRATION",
})

# Substrings that indicate auth/signing failure (revoked Safe, bad nonce, expired
# API creds). On any of these we stop retrying and raise — silent fail-loops
# would let the bot run for hours posting orders that never reach the exchange.
_AUTH_ERR_TOKENS = (
    "401", "403", "unauthorized", "forbidden",
    "signature", "signing", "nonce",
    "private key", "api credentials", "invalid api",
)


class AuthError(RuntimeError):
    """Raised when Polymarket rejects an order on auth/signing grounds.

    The main loop catches this once and shuts down so the operator notices
    immediately instead of watching every entry silently fail.
    """


def _looks_like_auth_error(err: object) -> bool:
    s = str(err).lower()
    return any(token in s for token in _AUTH_ERR_TOKENS)

# ---------------------------------------------------------------------------
# Fill rate tracking (live mode only)
# ---------------------------------------------------------------------------
import json as _json
from pathlib import Path as _Path
from datetime import datetime as _dt, timezone as _tz

_FILL_STATS_PATH = _Path("polybot/memory/fill_stats.json")


def _update_fill_stats(filled: bool, side: str) -> None:
    """Atomically update FOK fill rate stats. Silent on I/O errors."""
    try:
        stats = {"total_attempts": 0, "total_fills": 0,
                 "buy_attempts": 0, "buy_fills": 0,
                 "sell_attempts": 0, "sell_fills": 0}
        if _FILL_STATS_PATH.exists():
            try:
                stats.update(_json.loads(_FILL_STATS_PATH.read_text()))
            except Exception:
                pass
        stats["total_attempts"] += 1
        if filled:
            stats["total_fills"] += 1
        if side == BUY:
            stats["buy_attempts"] += 1
            if filled:
                stats["buy_fills"] += 1
        else:
            stats["sell_attempts"] += 1
            if filled:
                stats["sell_fills"] += 1
        stats["fill_rate"] = round(stats["total_fills"] / max(stats["total_attempts"], 1), 4)
        stats["last_updated"] = _dt.now(_tz.utc).isoformat()
        _FILL_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FILL_STATS_PATH.write_text(_json.dumps(stats, indent=2))
    except Exception:
        pass


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
        signature_type=SignatureTypeV2.POLY_GNOSIS_SAFE,  # MetaMask EOA → Polymarket Safe
        funder=funder,
    )
    # Derive first (GET) — Cloudflare blocks POST /auth/api-key with 403 even
    # though the library swallows it via fallback. Calling derive directly
    # avoids the noisy 403 log on every startup. Fall back to create only
    # for fresh accounts that haven't generated keys yet.
    try:
        creds = client.derive_api_key()
    except Exception:
        creds = client.create_api_key()
    client.set_api_creds(creds)
    return client


def _get_balance_usd(client: ClobClient) -> float:
    """Fetch USDC balance from Polymarket. Returns float in dollars."""
    result = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    return int(result.get("balance", "0")) / 1e6


def _get_balance_and_allowance_usd(client: ClobClient) -> tuple[float, float]:
    """Fetch (USDC_balance_usd, min_USDC_allowance_usd) from Polymarket.

    Polymarket returns a dict ``allowances: {spender_addr: amount}`` keyed by the
    three exchange/adapter contracts: CTF Exchange, Neg Risk Exchange, Neg Risk
    Adapter. Any one of them at zero blocks that market type, so we return the
    MIN across all three — if any spender is under-approved, preflight fails.
    Returns (balance, 0.0) if the allowances dict is missing or empty.
    """
    result = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    balance = int(result.get("balance", "0")) / 1e6
    allowances = result.get("allowances") or {}
    if not allowances:
        return balance, 0.0
    min_allowance = min(int(v) for v in allowances.values()) / 1e6
    return balance, min_allowance


def verify_auth(min_allowance_usd: float | None = None) -> tuple[bool, str, float]:
    """Verify Polymarket auth and return (ok, message, balance).

    If ``min_allowance_usd`` is provided, auth fails when the Safe's USDC allowance
    to the CTF Exchange is below it. Typical production threshold:
    ``(bankroll × kelly_fraction) × max_concurrent_positions × safety_multiplier``.

    Used by verify_keys.py (no threshold — informational) and main.py preflight
    (threshold passed from config — hard gate).
    """
    try:
        client = _create_clob_client()
    except ValueError as e:
        return False, str(e), 0.0
    except Exception as e:
        return False, f"Auth failed — check POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER: {e}", 0.0

    try:
        balance, allowance = _get_balance_and_allowance_usd(client)
    except Exception as e:
        return False, f"Authenticated but balance/allowance fetch failed: {e}", 0.0

    msg = f"Authenticated OK, USDC balance: ${balance:,.2f}, allowance: ${allowance:,.2f}"
    if balance < 1.0:
        msg += " — WARNING: low balance, deposit USDC on Polymarket before trading"

    if min_allowance_usd is not None and allowance < min_allowance_usd:
        funder = os.environ.get("POLYMARKET_FUNDER", "<safe>")
        return False, (
            f"USDC allowance ${allowance:,.2f} below required ${min_allowance_usd:,.2f} "
            f"(balance=${balance:,.2f}). Safe {funder} needs to re-approve USDC to the "
            f"CTF Exchange contract — orders will silently fail until this is fixed. "
            f"Deposit/withdraw any amount on Polymarket UI to trigger the auto-approval, "
            f"or call USDC.approve(CTF_EXCHANGE, max_uint) directly."
        ), balance

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
        self._keepalive_task: asyncio.Task | None = None
        logger.info("LiveTrader authenticated with Polymarket CLOB")

    async def start_keepalive(self) -> None:
        """Ping the CLOB API every 30s to keep the HTTP/2 connection warm.

        py-clob-client uses a persistent httpx.Client(http2=True). If the connection
        goes idle between trades (>60s), the HTTP/2 stream may close and the next
        order submission pays a full TLS handshake penalty (~100-200ms). Keepalive
        pings prevent that by re-using the already-established connection.
        """
        async def _ping() -> None:
            while True:
                try:
                    await asyncio.sleep(30)
                    await asyncio.to_thread(self.client.get_sampling_simplified_markets)
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass  # best-effort — never crash trading on a keepalive failure
        self._keepalive_task = asyncio.create_task(_ping())
        logger.info("LiveTrader: HTTP keepalive started (ping every 30s)")

    async def stop_keepalive(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

    async def get_balance(self) -> float:
        """Fetch USDC balance from Polymarket. Returns float in dollars."""
        return _get_balance_usd(self.client)

    async def _execute_buy(self, token_id: str, price: float, size: float) -> FillResult:
        """Buy: try maker limit order (0% fee) if enabled, else FOK (1.8% fee)."""
        if self.use_maker_orders:
            return await self._execute_buy_limit(token_id, price, size, self.maker_timeout_s)
        return await self._submit_fok_order(token_id, BUY, size, price)

    def set_clob_ws(self, clob_ws) -> None:
        """Attach CLOB WebSocket for fast maker fill detection via trade events."""
        self._clob_ws = clob_ws

    async def _execute_sell(self, token_id: str, shares: float, price: float) -> FillResult:
        """FOK market sell for `shares` shares."""
        return await self._submit_fok_order(token_id, SELL, shares, price)

    async def _resolve_bankroll(self, position: dict[str, Any], exit_price: float) -> float:
        """Sync bankroll with real Polymarket balance.

        On winning resolutions, wait briefly for Polymarket's auto-redeem to fire
        (~5 Polygon blocks) before fetching balance — otherwise we sync to the
        pre-redeem USDC and undercount the bankroll until the next resolution.
        Harmless if auto-redeem is off: balance just reflects the unredeemed state
        either way, but the next get_balance() picks up the manual redeem when it
        happens. Losses skip the wait — no redemption tx fires for $0 shares.
        """
        if exit_price >= 0.99:
            logger.info("Winning resolution — waiting 10s for auto-redeem to confirm on-chain")
            await asyncio.sleep(10)
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

            order = await asyncio.to_thread(
                self.client.create_order,
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=shares,
                    side=BUY,
                ),
            )
            resp = await asyncio.to_thread(self.client.post_order, order)

            order_id = resp.get("orderID") or resp.get("id")
            if not order_id:
                logger.warning("Maker order: no order ID returned, falling back to FOK")
                return await self._submit_fok_order(token_id, BUY, size, price)

            logger.info(
                "Maker limit order posted: %s at $%.4f (%.4f shares)",
                order_id, price, shares,
            )

            # Wait for fill — use CLOB WebSocket trade events for near-instant
            # detection, with periodic REST poll as backup.
            clob_ws = getattr(self, "_clob_ws", None)
            elapsed = 0.0
            check_interval = 0.3  # fast cycle: WS event or short sleep
            last_rest_check = 0.0
            rest_poll_interval = 5.0  # REST backup every 5s in case WS misses it

            while elapsed < timeout_s:
                # If CLOB WS available, wait for trade event (fast path)
                if clob_ws and hasattr(clob_ws, "book_updated"):
                    try:
                        await asyncio.wait_for(clob_ws.book_updated.wait(), timeout=check_interval)
                        clob_ws.book_updated.clear()
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(check_interval)
                elapsed += check_interval

                # Check for fill via REST (every 5s, or immediately after WS trade event)
                should_check = (elapsed - last_rest_check) >= rest_poll_interval
                # Also check if a trade just landed on our token via WS
                if clob_ws:
                    last_trade = clob_ws.last_trade.get(token_id, {})
                    if last_trade and abs(float(last_trade.get("price", 0)) - price) < 0.01:
                        should_check = True  # trade at our price — likely our fill

                if should_check:
                    last_rest_check = elapsed
                    try:
                        order_status = await asyncio.to_thread(self.client.get_order, order_id)
                        status = order_status.get("status", "")
                        if status == "MATCHED":
                            fill_price = await self._get_fill_price(order_id, price)
                            logger.info(
                                "Maker order filled: %s at $%.4f (detected in %.1fs)",
                                order_id, fill_price, elapsed,
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
                            return await self._submit_fok_order(token_id, BUY, size, price)
                    except Exception as poll_err:
                        logger.warning("Maker poll error: %s", poll_err)

            # Timeout — cancel the resting order and fall back to FOK
            try:
                await asyncio.to_thread(
                    self.client.cancel_order, OrderPayload(orderID=order_id)
                )
                logger.info(
                    "Maker order timed out after %.0fs, cancelled %s — falling back to FOK",
                    timeout_s, order_id,
                )
            except Exception as cancel_err:
                logger.warning("Failed to cancel maker order %s: %s", order_id, cancel_err)

            return await self._submit_fok_order(token_id, BUY, size, price)

        except Exception as e:
            logger.warning("Maker order flow failed: %s — falling back to FOK", e)
            return await self._submit_fok_order(token_id, BUY, size, price)

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
        # Polymarket rejects marketable orders below $1 notional. Short-circuit
        # before hammering CLOB 3× for a guaranteed-fail order. BUY amount is
        # USDC; SELL amount is shares (× expected_price for notional).
        notional_usd = amount if side == BUY else amount * expected_price
        if notional_usd < _MIN_ORDER_USD:
            logger.info(
                "FOK %s skipped: notional $%.2f below $%.2f minimum",
                side, notional_usd, _MIN_ORDER_USD,
            )
            return FillResult(
                filled=False,
                reason=f"Order ${notional_usd:.2f} below ${_MIN_ORDER_USD:.2f} CLOB minimum",
            )

        last_error = ""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                # Pass expected_price as the FOK limit. py-clob-client treats price=0
                # as "fetch from /price cross-matched API" which would silently bypass
                # the bot's best_ask/best_bid-based slippage protection from main.py.
                mo = MarketOrderArgs(token_id=token_id, amount=amount, side=side, price=expected_price)
                # Offload blocking SDK calls to thread pool — keeps event loop free
                signed = await asyncio.to_thread(self.client.create_market_order, mo)
                resp = await asyncio.to_thread(self.client.post_order, signed, OrderType.FOK)

                if not resp.get("success"):
                    error_msg = resp.get("errorMsg", "unknown error")
                    if _looks_like_auth_error(error_msg):
                        logger.error("AUTH FAILURE — Polymarket rejected order: %s", error_msg)
                        raise AuthError(error_msg)
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
                    _update_fill_stats(filled=True, side=side)
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

            except AuthError:
                raise
            except Exception as e:
                if _looks_like_auth_error(e):
                    logger.error("AUTH FAILURE during FOK submit: %s", e)
                    raise AuthError(str(e)) from e
                last_error = str(e)
                logger.warning(
                    "FOK attempt %d/%d exception: %s", attempt, _MAX_RETRIES, e,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))

        _update_fill_stats(filled=False, side=side)
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
