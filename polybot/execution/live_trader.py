"""Live trader: real Polymarket CLOB orders via py-clob-client-v2 (v2 contracts)."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import random
import time
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
from polybot.execution.base import BaseTrader, DEFAULT_FEE_RATE, FillResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.05
_RETRY_JITTER = 0.2  # ±20% jitter on backoff to avoid thundering-herd retry storms
_MIN_ORDER_USD = 1.0  # Polymarket CLOB rejects marketable orders below $1 notional
_NON_RETRYABLE_ERRORS = frozenset({
    "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    "MARKET_NOT_READY",
    "INVALID_ORDER_EXPIRATION",
})
_ALLOWANCE_RECHECK_EVERY = 10
_FILL_PRICE_LOOKUP_RETRIES = 3
_FILL_PRICE_LOOKUP_DELAY = 0.075
_DUST_THRESHOLD_SHARES = 0.01
_BALANCE_SETTLE_FLOOR = 0.05  # min chain-settle wait even if WS fires immediately
_BALANCE_SETTLE_DELAY = 0.25  # max wait (legacy fixed delay, used as ceiling + no-WS fallback)

def _retry_sleep(attempt: int) -> float:
    """Exponential backoff with multiplicative jitter. attempt is 1-indexed."""
    base = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
    return base * random.uniform(1.0 - _RETRY_JITTER, 1.0 + _RETRY_JITTER)

# Substrings that indicate auth/signing failure (revoked Safe, bad nonce, expired
# API creds). On any of these we stop retrying and raise — silent fail-loops
# would let the bot run for hours posting orders that never reach the exchange.
_AUTH_ERR_TOKENS = (
    "status_code=401", "status_code=403", "unauthorized", "forbidden",
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
        self._submit_count_since_allowance_check: int = 0
        self._min_allowance_warn_threshold: float = float(
            kwargs.get("min_allowance_warn_usd", 25.0)
        )
        self._sign_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="clob-sign"
        )
        self._latched_auth_error: str | None = None
        logger.info("LiveTrader authenticated with Polymarket CLOB")

    async def start_keepalive(self) -> None:
        """Ping the CLOB API every 10s to keep the HTTP/2 connection warm.

        py-clob-client uses a persistent httpx.Client(http2=True). If the connection
        goes idle between trades (>60s), the HTTP/2 stream may close and the next
        order submission pays a full TLS handshake penalty (~100-200ms). Pinging
        every 10s gives 3× more retry attempts before that 60s cliff.

        Auth-looking failures during a ping latch `self._latched_auth_error`;
        the next FOK submit then raises AuthError without making a live order
        attempt, so the main loop's AuthError handler can shut trading down.
        """
        try:
            await asyncio.to_thread(self.client.get_sampling_simplified_markets)
        except Exception as e:
            if _looks_like_auth_error(e):
                logger.error(
                    "AUTH FAILURE during keepalive pre-warm: %s — "
                    "latching for fail-fast on next FOK submit", e,
                )
                self._latched_auth_error = str(e)
        async def _ping() -> None:
            while True:
                try:
                    await asyncio.sleep(10)
                    await asyncio.to_thread(self.client.get_sampling_simplified_markets)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    if _looks_like_auth_error(e):
                        logger.error(
                            "AUTH FAILURE during keepalive ping: %s — "
                            "latching for fail-fast on next FOK submit", e,
                        )
                        self._latched_auth_error = str(e)
                        break  # stop pinging; next FOK submit raises AuthError
                    # other transient errors — best-effort, never crash trading
        self._keepalive_task = asyncio.create_task(_ping())
        logger.info("LiveTrader: HTTP keepalive started (ping every 10s)")

    async def stop_keepalive(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
        try:
            self._sign_executor.shutdown(wait=False)
        except Exception:
            pass

    async def get_balance(self) -> float:
        """Fetch USDC balance from Polymarket. Returns float in dollars."""
        return _get_balance_usd(self.client)

    async def _maybe_recheck_allowance(self) -> None:
        """Re-check USDC allowance every _ALLOWANCE_RECHECK_EVERY submits; warn if revoked mid-session."""
        self._submit_count_since_allowance_check += 1
        if self._submit_count_since_allowance_check < _ALLOWANCE_RECHECK_EVERY:
            return
        self._submit_count_since_allowance_check = 0
        try:
            _, allowance = await asyncio.to_thread(
                _get_balance_and_allowance_usd, self.client
            )
        except Exception as e:
            logger.debug("Allowance recheck failed: %s", e)
            return
        if allowance < self._min_allowance_warn_threshold:
            logger.error(
                "USDC allowance dropped to $%.2f (warn threshold $%.2f). "
                "Safe %s may need re-approval before next batch of orders. "
                "Subsequent orders will fail INSUFFICIENT_ALLOWANCE.",
                allowance,
                self._min_allowance_warn_threshold,
                os.environ.get("POLYMARKET_FUNDER", "<safe>"),
            )

    async def _execute_buy(
        self, token_id: str, price: float, size: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> FillResult:
        """Buy: try maker limit order (0% fee) if enabled, else FOK (1.8% fee)."""
        if self.use_maker_orders:
            return await self._execute_buy_limit(token_id, price, size, self.maker_timeout_s)
        return await self._submit_fok_order(token_id, BUY, size, price, fee_rate=fee_rate)

    async def _await_buy_settle(self, ws_event: asyncio.Event | None) -> None:
        """Wait for the chain to register a BUY fill before reading balance.

        Adaptive: always waits the floor (~50ms) for chain settlement, then
        either returns the moment the CLOB WS reports a trade on our token,
        or hits the legacy 250ms ceiling. Falls back to the fixed delay if
        no WS is attached (parity with the pre-event-driven behavior).

        Wakes from any trade on our token, not just ours — the downstream
        balance-delta check tolerates that (a noise wake reads `delta=0`,
        which gracefully falls through to associate_trades VWAP).
        """
        await asyncio.sleep(_BALANCE_SETTLE_FLOOR)
        if ws_event is None:
            await asyncio.sleep(_BALANCE_SETTLE_DELAY - _BALANCE_SETTLE_FLOOR)
            return
        if ws_event.is_set():
            return  # trade landed during the floor sleep
        try:
            await asyncio.wait_for(
                ws_event.wait(),
                timeout=_BALANCE_SETTLE_DELAY - _BALANCE_SETTLE_FLOOR,
            )
        except asyncio.TimeoutError:
            pass

    async def _execute_sell(
        self, token_id: str, shares: float, price: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> FillResult:
        """FOK market sell for `shares` shares."""
        return await self._submit_fok_order(token_id, SELL, shares, price, fee_rate=fee_rate)

    async def _resolve_bankroll(self, position: dict[str, Any], exit_price: float) -> float:
        """Sync bankroll with real Polymarket balance.

        On winning resolutions, poll until the auto-redeem USDC lands on-chain
        (up to 60s total). Stops early once balance rises above pre-redeem level,
        so we don't wait 60s on a fast chain. Losses skip entirely — no redeem tx.
        """
        if exit_price < 0.99:
            real_balance = await self.get_balance()
            logger.info("Resolution bankroll sync: real balance=%.2f", real_balance)
            return real_balance

        pre_balance = await self.get_balance()
        expected_gain = position.get("shares_held", 0) * exit_price
        # Poll with backoff: 5s, 5s, 10s, 10s, 15s, 15s = 60s max
        for delay in (5, 5, 10, 10, 15, 15):
            await asyncio.sleep(delay)
            balance = await self.get_balance()
            if balance >= pre_balance + expected_gain * 0.95:
                logger.info(
                    "Winning resolution — auto-redeem confirmed: balance %.2f -> %.2f",
                    pre_balance, balance,
                )
                return balance
        logger.warning(
            "Winning resolution — auto-redeem not detected after 60s "
            "(pre=%.2f expected_gain=%.2f final=%.2f). Using final balance.",
            pre_balance, expected_gain, balance,
        )
        return balance

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
            clob_ws = self._clob_ws
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

    # -- Dust helpers (Polymarket FOK fills sometimes leave fractional residuals
    # when `_get_fill_price` falls back to the limit price and shares_received is
    # undercount) --
    async def _get_token_balance(self, token_id: str) -> float:
        """Return on-chain conditional-token balance in shares. 0.0 on failure."""
        if not token_id:
            return 0.0
        try:
            result = await asyncio.to_thread(
                self.client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id),
            )
            return int(result.get("balance", "0")) / 1e6
        except Exception as e:
            logger.warning("Token balance query failed for %s: %s", token_id[:12], e)
            return 0.0

    async def _sweep_residual(self, token_id: str, ref_price: float) -> None:
        """Sell any leftover shares of token_id (FOK fill-price-lookup undercount).

        Best-effort: failures are logged but never propagate. Always called as a
        background task so it doesn't add latency to the originating SELL's return.
        """
        if not token_id:
            return
        try:
            await asyncio.sleep(_BALANCE_SETTLE_DELAY)
            residual = await self._get_token_balance(token_id)
            if residual <= _DUST_THRESHOLD_SHARES:
                return
            logger.warning(
                "Dust detected: %.4f shares of token %s (ref_price=%.4f) — sweeping",
                residual, token_id[:12], ref_price,
            )
            # Clamp ref_price into a valid FOK range so the sweep doesn't bounce on
            # tick / spread issues. Use the existing ref_price as best-effort.
            safe_price = max(0.01, min(0.99, float(ref_price) if ref_price else 0.5))
            sweep_args = MarketOrderArgs(
                token_id=token_id, amount=residual, side=SELL, price=safe_price,
            )
            def _sweep_sign_and_post() -> dict:
                return self.client.post_order(
                    self.client.create_market_order(sweep_args), OrderType.FOK,
                )
            resp = await asyncio.to_thread(_sweep_sign_and_post)
            if resp.get("success") and resp.get("status") == "matched":
                logger.info(
                    "Dust swept: %.4f shares of %s @ ~%.4f",
                    residual, token_id[:12], safe_price,
                )
            else:
                logger.warning(
                    "Dust sweep did not match for %s: status=%s err=%s — "
                    "shares may resolve on their own at expiry",
                    token_id[:12], resp.get("status"), resp.get("errorMsg", ""),
                )
        except Exception as e:
            logger.warning("Dust sweep error for %s: %s", token_id[:12], e)

    async def reconcile_dust(self, db: Database, max_age_hours: int = 24) -> int:
        """Scan recently-closed positions for residual on-chain shares and sweep them.

        Called once at startup. Reads token_ids from the indicator_snapshot of
        recently-closed positions and queries each conditional-token balance; if
        residual shares > dust threshold, fires a FOK SELL to recover value before
        expiry. Returns count of swept token_ids. Non-blocking on any error.
        """
        swept = 0
        try:
            cursor = await db.conn.execute(
                "SELECT indicator_snapshot, exit_price, side, market_id "
                "FROM positions WHERE status='closed' "
                "AND exit_timestamp >= datetime('now', ?) "
                "AND indicator_snapshot IS NOT NULL",
                (f"-{max_age_hours} hours",),
            )
            rows = await cursor.fetchall()
        except Exception as e:
            logger.warning("Dust reconciliation: DB scan failed: %s", e)
            return 0

        seen: set[str] = set()
        for snap_text, exit_price, side, market_id in rows:
            try:
                snap = _json.loads(snap_text) if isinstance(snap_text, str) else {}
                ctx = snap.get("trade_context", {}) if isinstance(snap, dict) else {}
                tok = ctx.get("token_id_up") if side == "Up" else ctx.get("token_id_down")
                if not tok or tok in seen:
                    continue
                seen.add(tok)
                bal = await self._get_token_balance(tok)
                if bal <= _DUST_THRESHOLD_SHARES:
                    continue
                logger.warning(
                    "Startup dust: %.4f shares of token %s (market %s, side %s) — sweeping",
                    bal, tok[:12], market_id, side,
                )
                await self._sweep_residual(tok, float(exit_price or 0.5))
                swept += 1
            except Exception as e:
                logger.debug("Dust reconciliation row skipped: %s", e)
                continue
        if swept:
            logger.warning("Dust reconciliation swept %d token(s)", swept)
        else:
            logger.info("Dust reconciliation: no residuals found in last %dh", max_age_hours)
        return swept

    # -- FOK order submission with retry ------------------------------------

    async def _submit_fok_order(
        self,
        token_id: str,
        side: str,
        amount: float,
        expected_price: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> FillResult:
        """Submit FOK market order with exponential-backoff retry.

        Args:
            token_id: CLOB token ID.
            side: BUY or SELL.
            amount: USDC for BUY, shares for SELL.
            expected_price: Used as fallback if fill price lookup fails.
            fee_rate: Used to convert WS-derived gross VWAP into the
                net-shares-based fill_price the rest of the system expects.

        Returns:
            FillResult with fill details or failure reason.
        """
        if self._latched_auth_error is not None:
            raise AuthError(f"latched from keepalive: {self._latched_auth_error}")

        # Polymarket rejects marketable orders below $1 notional. Short-circuit
        # before hammering CLOB 3× for a guaranteed-fail order. BUY amount is
        # USDC; SELL amount is shares (× expected_price for notional).
        notional_usd = amount if side == BUY else amount * expected_price
        if notional_usd < _MIN_ORDER_USD - 0.01:
            logger.info(
                "FOK %s skipped: notional $%.2f below $%.2f minimum",
                side, notional_usd, _MIN_ORDER_USD,
            )
            return FillResult(
                filled=False,
                reason=f"Order ${notional_usd:.2f} below ${_MIN_ORDER_USD:.2f} CLOB minimum",
            )

        balance_task: asyncio.Task[float] | None = None
        balance_before: float = -1.0
        ws_settle_event: asyncio.Event | None = None
        clob_ws = self._clob_ws if side == BUY else None
        # submit_ts is captured BEFORE signing so the WS trade-buffer scan
        # below can find our matched trades — they land on the WS within
        # ~50-200ms of the POST returning success, but their `timestamp` is
        # set when the trade is dispatched (close to submit_ts + chain latency).
        # A small slack (-50ms) tolerates clock skew between our host and
        # Polymarket's match-engine timestamp.
        submit_ts = time.time()
        if side == BUY:
            balance_task = asyncio.create_task(self._get_token_balance(token_id))
            if clob_ws is not None and hasattr(clob_ws, "trade_event_for"):
                ws_settle_event = clob_ws.trade_event_for(token_id)
                ws_settle_event.clear()

        last_error = ""
        loop = asyncio.get_running_loop()
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                mo = MarketOrderArgs(token_id=token_id, amount=amount, side=side, price=expected_price)
                # Sign and post in one thread dispatch on the dedicated executor.
                def _sign_and_post(order_args: MarketOrderArgs) -> dict:
                    return self.client.post_order(
                        self.client.create_market_order(order_args), OrderType.FOK
                    )
                resp = await loop.run_in_executor(self._sign_executor, _sign_and_post, mo)

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
                    logger.debug("FOK %d/%d: price moved before fill", attempt, _MAX_RETRIES)
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_retry_sleep(attempt))
                    continue

                if resp.get("status") == "matched":
                    order_id = resp.get("orderID", "")
                    fill_price: float | None = None
                    if side == BUY:
                        # Same settle window the balance-delta path used — gives
                        # the WS time to deliver our matched trade event(s).
                        await self._await_buy_settle(ws_settle_event)
                        # --- Fast path: WS-derived VWAP ---
                        # Skips the second _get_token_balance REST call
                        # (~30-100ms saved). Sanity-bound against expected fill
                        # size so background trades on our level or a missed WS
                        # event fall through to the balance-delta fallback.
                        if clob_ws is not None and hasattr(clob_ws, "trades_since"):
                            try:
                                ws_trades = clob_ws.trades_since(token_id, submit_ts - 0.05)
                            except Exception:
                                ws_trades = []
                            # Taker buys can't fill above limit; 0.005 absorbs tick rounding.
                            candidates = [
                                t for t in ws_trades
                                if 0 < float(t.get("price", 0) or 0) <= expected_price + 0.005
                            ]
                            if candidates:
                                gross_shares = sum(float(t["size"]) for t in candidates)
                                gross_cost = sum(
                                    float(t["size"]) * float(t["price"]) for t in candidates
                                )
                                if gross_shares > _DUST_THRESHOLD_SHARES:
                                    gross_vwap = gross_cost / gross_shares
                                    expected_shares = amount / expected_price
                                    # Lower 0.85: tolerate small slippage / one
                                    # missed level. Upper 1.30: VWAP >23% below
                                    # limit would be very surprising — likely
                                    # background trade pollution.
                                    if 0.85 * expected_shares <= gross_shares <= 1.30 * expected_shares:
                                        # Polymarket entry fee in shares:
                                        #   fee_shares = fee_rate * shares * p * (1-p)
                                        # Convert gross VWAP -> net-shares so
                                        # downstream sees the same "amount/net"
                                        # form the balance-delta path emits.
                                        fee_shares = (
                                            fee_rate * gross_shares
                                            * gross_vwap * (1 - gross_vwap)
                                        )
                                        net_shares = max(
                                            gross_shares - fee_shares, _DUST_THRESHOLD_SHARES
                                        )
                                        fill_price = amount / net_shares
                                        logger.debug(
                                            "BUY WS-derived VWAP: %d trade(s), "
                                            "gross=%.4f @ %.4f -> fill_price=%.4f "
                                            "(skipped 2nd balance read)",
                                            len(candidates), gross_shares, gross_vwap, fill_price,
                                        )
                                        # WS path won — discard the parallel balance
                                        # pre-fetch so it doesn't leak a task.
                                        if balance_task is not None and not balance_task.done():
                                            balance_task.cancel()
                                            balance_task = None
                        # --- Fallback: balance-delta (existing path) ---
                        if fill_price is None and balance_task is not None:
                            try:
                                balance_before = await balance_task
                            except Exception:
                                balance_before = -1.0
                            balance_task = None
                            if balance_before >= 0:
                                balance_after = await self._get_token_balance(token_id)
                                delta = balance_after - balance_before
                                if delta > _DUST_THRESHOLD_SHARES:
                                    fill_price = amount / delta
                                    logger.debug(
                                        "BUY balance-delta VWAP fallback: %.4f shares -> "
                                        "price=%.4f (before=%.4f after=%.4f notional=%.2f)",
                                        delta, fill_price, balance_before, balance_after, amount,
                                    )
                    if fill_price is None:
                        fill_price = await self._get_fill_price(order_id, expected_price)
                    logger.info(
                        "FOK %s filled: order=%s, price=%.4f, amount=%.4f",
                        side, order_id, fill_price, amount,
                    )
                    _update_fill_stats(filled=True, side=side)
                    # Fire-and-forget: the recheck only logs a warning when allowance
                    # drops below threshold; blocking the FOK return path on it cost
                    # 30-100ms every 10th submit for zero trading-logic benefit.
                    asyncio.create_task(self._maybe_recheck_allowance())
                    if side == SELL:
                        asyncio.create_task(self._sweep_residual(token_id, fill_price))
                    notional_usdc = amount if side == BUY else amount * fill_price
                    return FillResult(
                        filled=True,
                        fill_price=fill_price,
                        fill_size=notional_usdc,
                    )

                # Unexpected status
                last_error = f"Unexpected status: {resp.get('status')}"
                logger.warning("FOK %d/%d: unexpected status %s", attempt, _MAX_RETRIES, resp.get('status'))
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_retry_sleep(attempt))

            except AuthError:
                raise
            except Exception as e:
                if _looks_like_auth_error(e):
                    logger.error("AUTH FAILURE during FOK submit: %s", e)
                    raise AuthError(str(e)) from e
                last_error = str(e)
                logger.debug("FOK %d/%d: price moved before fill", attempt, _MAX_RETRIES)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_retry_sleep(attempt))

        # All retries exhausted — order never executed. Cancel the orphaned
        # balance_task so it doesn't leave a dangling reference.
        if balance_task is not None and not balance_task.done():
            balance_task.cancel()
        _update_fill_stats(filled=False, side=side)
        return FillResult(
            filled=False,
            reason=f"price moved before fill after {_MAX_RETRIES} attempts",
        )

    # -- Fill price lookup --------------------------------------------------

    async def _get_fill_price(self, order_id: str, fallback_price: float) -> float:
        """Fetch actual fill price via VWAP from associate_trades.

        Retries a few times because the CLOB's REST view often lags the match
        engine by 100–300ms — falling back to the submitted limit price
        misreports VWAP for partial fills and breaks fee accounting downstream.
        Only falls back to the limit price after all retries fail or the
        order genuinely has no associated trades.
        """
        last_err: Exception | None = None
        for attempt in range(_FILL_PRICE_LOOKUP_RETRIES):
            try:
                order = await asyncio.to_thread(self.client.get_order, order_id)
                trades = order.get("associate_trades", [])
                trades = [t for t in trades if isinstance(t, dict)]
                if not trades:
                    if attempt < _FILL_PRICE_LOOKUP_RETRIES - 1:
                        await asyncio.sleep(_FILL_PRICE_LOOKUP_DELAY)
                        continue
                    return fallback_price
                total_shares = sum(float(t["size"]) for t in trades)
                if total_shares == 0:
                    return fallback_price
                total_cost = sum(float(t["size"]) * float(t["price"]) for t in trades)
                return total_cost / total_shares
            except Exception as e:
                last_err = e
                if attempt < _FILL_PRICE_LOOKUP_RETRIES - 1:
                    await asyncio.sleep(_FILL_PRICE_LOOKUP_DELAY)
        logger.warning(
            "Fill price lookup for %s exhausted retries (%s) — falling back to "
            "submitted limit %.4f. Fee math may be off if the order walked the book.",
            order_id, last_err, fallback_price,
        )
        return fallback_price
