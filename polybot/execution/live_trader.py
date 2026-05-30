"""Live trader: real Polymarket CLOB orders via py-clob-client-v2 (v2 contracts)."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import random
import time
from collections import deque
from typing import Any
import httpx
from py_clob_client_v2.http_helpers import helpers as _clob_helpers
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
from polybot.execution.base import (
    BaseTrader, DEFAULT_FEE_RATE, FillResult,
    exit_fee_usdc, _entry_fee_usd_from_position,
)
from polybot.core.returns import log_return

# Replace py-clob-client's module-global HTTP/2 singleton with one whose
# keepalive_expiry (60s) comfortably outlives our 5s keepalive ping. The default
# httpx keepalive_expiry of 5.0s would let the connection lapse between pings and
# make roughly half of order POSTs pay a fresh TLS handshake.
_clob_helpers._http_client = httpx.Client(
    http2=True,
    timeout=20.0,
    limits=httpx.Limits(
        max_connections=10,
        max_keepalive_connections=5,
        keepalive_expiry=60.0,
    ),
)

class OrphanPositionError(Exception):
    """Raised at startup when on-chain positions exist that the DB doesn't know about.

    A FOK fill that acked but failed to write the DB row leaves shares on chain
    with no local record. The trading loop can never manage them and `reconcile_open`
    only reconciles DB-known rows — so without this gate the orphan would resolve
    silently and the gain/loss would just appear in the next bankroll sync.

    `run_polybot.ps1` does not auto-restart on this exception; the operator is
    expected to inspect `memory/state/orphan_positions.json`, reconcile manually, then
    re-run with `--allow-orphans` to acknowledge the residual shares.
    """

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.03
_RETRY_JITTER = 0.2  # ±20% jitter on backoff to avoid thundering-herd retry storms
_MIN_ORDER_USD = 1.0  # Polymarket CLOB rejects marketable orders below $1 notional
_NON_RETRYABLE_ERRORS = frozenset({
    "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    "MARKET_NOT_READY",
    "INVALID_ORDER_EXPIRATION",
})
_ALLOWANCE_RECHECK_EVERY = 10
_FILL_PRICE_LOOKUP_RETRIES = 3
_FILL_PRICE_LOOKUP_DELAY = 0.05
_DUST_THRESHOLD_SHARES = 0.01
_BALANCE_SETTLE_FLOOR = 0.03  # min chain-settle wait even if WS fires immediately
_BALANCE_SETTLE_DELAY = 0.15  # max wait (legacy fixed delay, used as ceiling + no-WS fallback)
# Tightened from 0.05/0.25. WS trade events for our token typically arrive within
# 50-150ms of the match-engine confirmation — the old 250ms ceiling left ~100ms
# of idle wait on every successful BUY. Floor stays small enough to absorb any
# ordering jitter between POST-success and the WS event landing.

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
from datetime import datetime as _dt, timezone as _tz

from polybot.paths import FILL_STATS_PATH, LATENCY_STATS_PATH, ORPHAN_POSITIONS_PATH

_FILL_STATS_PATH = FILL_STATS_PATH
_LATENCY_STATS_PATH = LATENCY_STATS_PATH
_LATENCY_SAMPLES: deque[float] = deque(maxlen=200)        # total = sign + post
_SIGN_LATENCY_SAMPLES: deque[float] = deque(maxlen=200)   # excludes presigned (sign=0)
_POST_LATENCY_SAMPLES: deque[float] = deque(maxlen=200)
# Bimodal-distribution detector for the POST leg. A TLS-handshake-on-half-of-requests
# pattern shows up as a cluster in the 250–500ms bucket; with a warm connection
# pool the bulk lands in 50–250ms.
_POST_BUCKET_EDGES_MS: tuple[float, ...] = (50, 100, 250, 500, 1000)


def _percentile(sorted_samples: list[float], pct: float) -> float:
    """Nearest-rank percentile on an already-sorted list. ``pct`` is 0–100."""
    if not sorted_samples:
        return 0.0
    idx = max(0, min(len(sorted_samples) - 1, int(len(sorted_samples) * pct / 100) - 1))
    return sorted_samples[idx]


def _bucket_counts(samples_ms: list[float]) -> dict[str, int]:
    """Histogram counts for the POST-time distribution. Names are bucket upper-bound (ms)."""
    edges = _POST_BUCKET_EDGES_MS
    counts = {f"le_{int(e)}ms": 0 for e in edges}
    counts[f"gt_{int(edges[-1])}ms"] = 0
    for v_ms in samples_ms:
        placed = False
        for e in edges:
            if v_ms <= e:
                counts[f"le_{int(e)}ms"] += 1
                placed = True
                break
        if not placed:
            counts[f"gt_{int(edges[-1])}ms"] += 1
    return counts


def _record_submit_latency(total_secs: float, sign_secs: float, post_secs: float) -> None:
    """Persist combined + per-leg latencies. ``sign_secs == 0`` for presigned SELL FOKs."""
    try:
        _LATENCY_SAMPLES.append(total_secs)
        if sign_secs > 0:
            _SIGN_LATENCY_SAMPLES.append(sign_secs)
        _POST_LATENCY_SAMPLES.append(post_secs)
        if len(_LATENCY_SAMPLES) < 5:
            return
        total_sorted = sorted(_LATENCY_SAMPLES)
        sign_sorted = sorted(_SIGN_LATENCY_SAMPLES)
        post_sorted = sorted(_POST_LATENCY_SAMPLES)
        post_ms = [v * 1000 for v in post_sorted]
        stats = {
            "n": len(total_sorted),
            "p50_ms": round(_percentile(total_sorted, 50) * 1000, 1),
            "p99_ms": round(_percentile(total_sorted, 99) * 1000, 1),
            "max_ms": round(total_sorted[-1] * 1000, 1),
            "sign": {
                "n": len(sign_sorted),
                "p50_ms": round(_percentile(sign_sorted, 50) * 1000, 1) if sign_sorted else 0.0,
                "p99_ms": round(_percentile(sign_sorted, 99) * 1000, 1) if sign_sorted else 0.0,
            },
            "post": {
                "n": len(post_sorted),
                "p25_ms": round(_percentile(post_sorted, 25) * 1000, 1),
                "p50_ms": round(_percentile(post_sorted, 50) * 1000, 1),
                "p75_ms": round(_percentile(post_sorted, 75) * 1000, 1),
                "p99_ms": round(_percentile(post_sorted, 99) * 1000, 1),
                "buckets": _bucket_counts(post_ms),
            },
            "last_updated": _dt.now(_tz.utc).isoformat(),
        }
        _LATENCY_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LATENCY_STATS_PATH.write_text(_json.dumps(stats, indent=2))
    except Exception:
        pass


# Failure-cause buckets — lets pipeline distinguish "price moved" rejects (a feature)
# from "network error" or "depth" rejects (a defect). Empty/unmatched reasons go to
# `other` so future failure modes show up rather than getting silently lumped in.
_FAILURE_BUCKETS: tuple[str, ...] = (
    "price_moved", "non_retryable", "precheck_depth",
    "below_min", "network_error", "auth", "other",
)


def _categorize_failure(reason: str) -> str:
    """Map a FillResult.reason or last_error string to a failure-cause bucket."""
    r = (reason or "").lower()
    if "price moved" in r:
        return "price_moved"
    if "pre-check" in r or "book walk" in r:
        return "precheck_depth"
    if "below" in r and ("minimum" in r or "clob minimum" in r):
        return "below_min"
    if any(kw in r for kw in ("timeout", "network", "connection refused", "rpc")):
        return "network_error"
    if "auth" in r:
        return "auth"
    if "non-retryable" in r or "not retryable" in r:
        return "non_retryable"
    return "other"


def _update_fill_stats(filled: bool, side: str, reason: str = "") -> None:
    """Atomically update FOK fill rate stats. Silent on I/O errors.

    `reason` is bucketed into _FAILURE_BUCKETS when filled=False so the pipeline
    can stratify retryable rejects (price_moved — a feature) from network/depth
    errors (a defect). New fields are additive; scheduler.py reads fill_rate and
    buy/sell counts the same way.
    """
    try:
        stats = {"total_attempts": 0, "total_fills": 0,
                 "buy_attempts": 0, "buy_fills": 0,
                 "sell_attempts": 0, "sell_fills": 0,
                 "failure_buckets": {b: 0 for b in _FAILURE_BUCKETS}}
        if _FILL_STATS_PATH.exists():
            try:
                stats.update(_json.loads(_FILL_STATS_PATH.read_text()))
            except Exception:
                pass
        # Backfill the buckets dict if a pre-S7 stats file is loaded.
        if "failure_buckets" not in stats or not isinstance(stats["failure_buckets"], dict):
            stats["failure_buckets"] = {b: 0 for b in _FAILURE_BUCKETS}
        for b in _FAILURE_BUCKETS:
            stats["failure_buckets"].setdefault(b, 0)
        stats["total_attempts"] += 1
        if filled:
            stats["total_fills"] += 1
        else:
            bucket = _categorize_failure(reason)
            stats["failure_buckets"][bucket] = stats["failure_buckets"].get(bucket, 0) + 1
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
        # 2 workers lets a concurrent BUY+SELL (entry firing while a scalp is in
        # flight) sign in parallel instead of queueing serially. Each worker
        # holds its own ECDSA signer; py-clob-client is thread-safe per call.
        self._sign_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="clob-sign"
        )
        self._latched_auth_error: str | None = None
        # Post-BUY chain-balance cache: token_id → (timestamp, balance_shares).
        # Filled after each successful FOK BUY by a background task; consumed by
        # _sellable_shares to skip the ~300ms /balance-allowance REST call on
        # the subsequent SELL. Safe because this bot runs single-position per
        # market: nothing else touches the wallet between our BUY and our SELL.
        # TTL of 300s (one full 5-min market window) bounds staleness; beyond
        # that we re-query to be safe.
        self._balance_cache: dict[str, tuple[float, float]] = {}
        self._BALANCE_CACHE_TTL_S: float = 300.0
        # Pre-signed SELL warm-ups. Filled in the background when main.py detects
        # holding_edge is in the "danger zone" — eliminates the ECDSA sign step
        # (~150ms) from the scalp's hot path so we just POST when PRE-SCALP fires.
        # Entry: token_id → {"order": signed_dict, "amount": shares, "price": p, "ts": time}.
        self._sell_warmups: dict[str, dict] = {}
        self._SELL_WARMUP_TTL_S: float = 5.0
        self._buy_warmups: dict[str, dict] = {}
        self._BUY_WARMUP_TTL_S: float = 5.0
        # Last condition_id whose market-info (tick/neg-risk/fee) we warmed into the
        # py-clob client cache at discovery — dedups prewarm_market_info per window.
        self._prewarmed_condition_id: str = ""
        logger.info("LiveTrader authenticated with Polymarket CLOB")

    async def prewarm_http(self) -> None:
        try:
            await asyncio.to_thread(self.client.get_sampling_simplified_markets)
        except Exception as e:
            if _looks_like_auth_error(e):
                logger.error(
                    "AUTH FAILURE during HTTP prewarm: %s — latching for fail-fast on next FOK submit", e,
                )
                self._latched_auth_error = str(e)

    async def prewarm_market_info(self, condition_id: str) -> None:
        """Warm the py-clob client's tick-size / neg-risk / fee caches for a
        market's tokens at window discovery, off the hot path.

        Without this, the first FOK of each new 5-min window pays ~2 sequential
        REST round-trips *inside* create_market_order (resolve condition_id +
        get_clob_market_info) before it can sign. A single get_clob_market_info
        call populates tick_size + neg_risk + fee + condition map for BOTH tokens,
        so the entry order signs with zero network. Best-effort and idempotent per
        condition_id; on failure the order just falls back to the per-order fetch.
        """
        if not condition_id or condition_id == self._prewarmed_condition_id:
            return
        self._prewarmed_condition_id = condition_id
        try:
            await asyncio.to_thread(self.client.get_clob_market_info, condition_id)
        except Exception as e:
            self._prewarmed_condition_id = ""  # allow retry on the next discovery tick
            logger.debug("prewarm_market_info failed for %s: %s", condition_id, e)

    async def start_keepalive(self) -> None:
        await self.prewarm_http()
        async def _ping() -> None:
            while True:
                try:
                    await asyncio.sleep(5)
                    await asyncio.to_thread(self.client.get_sampling_simplified_markets)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    if _looks_like_auth_error(e):
                        logger.error(
                            "AUTH FAILURE during keepalive ping: %s — latching for fail-fast on next FOK submit", e,
                        )
                        self._latched_auth_error = str(e)
                        break
        self._keepalive_task = asyncio.create_task(_ping())
        logger.info("LiveTrader: HTTP keepalive started (ping every 5s)")

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
    @staticmethod
    def _estimate_fok_walk(book: dict, side: str, amount: float,
                           limit_price: float) -> bool | None:
        """Simulate the FOK walk against the current book snapshot.

        Returns True if the order would likely fill (vwap on the correct side
        of limit_price), False if the walk would clearly exceed the limit,
        or None if the book is empty/unparseable (skip pre-check, let FOK try).

        BUY: vwap must be ≤ limit_price. SELL: vwap must be ≥ limit_price.
        For BUY, `amount` is USDC notional; for SELL, it is shares.
        """
        levels_key = "asks" if side == BUY else "bids"
        levels_raw = book.get(levels_key) or []
        if not levels_raw:
            return None
        try:
            parsed = [(float(l["price"]), float(l["size"])) for l in levels_raw
                      if l.get("price") and l.get("size")]
        except (TypeError, ValueError, KeyError):
            return None
        if not parsed:
            return None
        # Asks ascending (lowest first), bids descending (highest first).
        parsed.sort(key=lambda ps: ps[0], reverse=(side == SELL))

        spent = 0.0
        consumed = 0.0
        if side == BUY:
            remaining = amount  # USDC
            for px, sz in parsed:
                if remaining <= 0:
                    break
                level_usd = px * sz
                take_usd = min(remaining, level_usd)
                spent += take_usd
                consumed += take_usd / px
                remaining -= take_usd
        else:
            remaining = amount  # shares
            for px, sz in parsed:
                if remaining <= 0:
                    break
                take_shares = min(remaining, sz)
                spent += px * take_shares
                consumed += take_shares
                remaining -= take_shares
        if remaining > 1e-6 or consumed <= 0:
            return None  # insufficient book depth — let FOK decide
        vwap = spent / consumed
        return vwap <= limit_price if side == BUY else vwap >= limit_price

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

    async def _sellable_shares(self, token_id: str, fallback_shares: float) -> float:
        """Query on-chain balance so close_trade sells what we really own.

        Hot path: returns the cached post-BUY chain balance (set by
        _cache_post_buy_balance after each successful entry) when it's still
        within TTL. This skips the ~300ms /balance-allowance REST round-trip
        on the subsequent SELL, since nothing else touches the wallet between
        our BUY and SELL in single-position mode.

        Falls back to fallback_shares if (a) no token_id, (b) the API query
        failed (returned 0), or (c) the on-chain balance is implausibly far
        from the DB-tracked value (>3x or <0.3x).
        """
        if not token_id:
            return fallback_shares

        # Cache hit — skip the REST call.
        cached = self._balance_cache.get(token_id)
        if cached is not None:
            cache_ts, cache_bal = cached
            age = time.time() - cache_ts
            if age <= self._BALANCE_CACHE_TTL_S and cache_bal > 0:
                if fallback_shares > 0 and (
                    cache_bal > 3 * fallback_shares or cache_bal < 0.3 * fallback_shares
                ):
                    logger.warning(
                        "Cached chain balance %.4f diverges from DB shares %.4f for %s "
                        "(age %.1fs) — re-querying",
                        cache_bal, fallback_shares, token_id[:12], age,
                    )
                else:
                    logger.debug(
                        "Sell uses cached chain balance %.4f (age %.1fs) — saved REST roundtrip",
                        cache_bal, age,
                    )
                    return cache_bal
            # Stale or zero cache → fall through to live query.

        chain_bal = await self._get_token_balance(token_id)
        if chain_bal <= 0:
            return fallback_shares
        if fallback_shares > 0 and (chain_bal > 3 * fallback_shares or chain_bal < 0.3 * fallback_shares):
            logger.warning(
                "Chain balance %.4f diverges sharply from DB shares %.4f for %s — using DB value",
                chain_bal, fallback_shares, token_id[:12],
            )
            return fallback_shares
        if abs(chain_bal - fallback_shares) > 0.01:
            logger.info(
                "Sell uses chain balance %.4f (DB had %.4f) for %s",
                chain_bal, fallback_shares, token_id[:12],
            )
        return chain_bal

    async def _cache_post_buy_balance(self, token_id: str) -> None:
        """Background task: fetch post-BUY chain balance once and cache it so
        the subsequent SELL's _sellable_shares can skip the REST roundtrip.
        Fire-and-forget; quiet on failure (cache miss → SELL falls back to a
        live query, same behaviour as before this optimization)."""
        if not token_id:
            return
        try:
            bal = await self._get_token_balance(token_id)
            if bal > 0:
                self._balance_cache[token_id] = (time.time(), bal)
        except Exception:
            pass  # best-effort: missing cache just falls back to live query

    def _invalidate_balance_cache(self, token_id: str) -> None:
        """Drop the cached balance for token_id (call after SELL completes)."""
        self._balance_cache.pop(token_id, None)

    async def warm_sell_signature(self, token_id: str, shares: float,
                                  expected_price: float, fee_rate: float = DEFAULT_FEE_RATE) -> None:
        """Pre-sign a SELL FOK order in the background.

        Called by main.py during HOLD ticks when holding_edge is close to
        scalp threshold. Eliminates ~150ms of ECDSA sign latency from the
        scalp's hot path: when PRE-SCALP fires, _submit_fok_order finds the
        pre-signed order and only POSTs (skipping the create_market_order step).

        Idempotent: re-running within TTL refreshes the cached signature with
        current parameters, since the SELL price drifts every tick.
        """
        if not token_id or shares <= 0 or expected_price <= 0:
            return
        existing = self._sell_warmups.get(token_id)
        if existing is not None:
            # Only re-sign if the existing warmup is stale or params drifted.
            age = time.time() - existing["ts"]
            price_drift = abs(existing["price"] - expected_price)
            size_drift = abs(existing["amount"] - shares) / max(shares, 1e-6)
            if age < 1.5 and price_drift < 0.005 and size_drift < 0.02:
                return  # still good
        try:
            mo = MarketOrderArgs(
                token_id=token_id, amount=shares, side=SELL, price=expected_price,
            )
            loop = asyncio.get_running_loop()
            signed = await loop.run_in_executor(
                self._sign_executor, self.client.create_market_order, mo
            )
            self._sell_warmups[token_id] = {
                "order": signed,
                "amount": shares,
                "price": expected_price,
                "ts": time.time(),
            }
        except Exception as e:
            # Pre-signing is best-effort: failure just means PRE-SCALP will
            # pay the normal sign cost. Don't propagate.
            logger.debug("warm_sell_signature failed: %s", e)

    async def warm_buy_signature(self, token_id: str, size_usdc: float,
                                 expected_price: float, fee_rate: float = DEFAULT_FEE_RATE) -> None:
        if not token_id or size_usdc <= 0 or expected_price <= 0:
            return
        existing = self._buy_warmups.get(token_id)
        if existing is not None:
            age = time.time() - existing["ts"]
            price_drift = abs(existing["price"] - expected_price)
            size_drift = abs(existing["amount"] - size_usdc) / max(size_usdc, 1e-6)
            if age < 1.5 and price_drift < 0.005 and size_drift < 0.02:
                return
        try:
            mo = MarketOrderArgs(
                token_id=token_id, amount=size_usdc, side=BUY, price=expected_price,
            )
            loop = asyncio.get_running_loop()
            signed = await loop.run_in_executor(
                self._sign_executor, self.client.create_market_order, mo
            )
            self._buy_warmups[token_id] = {
                "order": signed,
                "amount": size_usdc,
                "price": expected_price,
                "ts": time.time(),
            }
        except Exception as e:
            logger.debug("warm_buy_signature failed: %s", e)

    def _take_buy_warmup(self, token_id: str, size_usdc: float,
                         expected_price: float) -> dict | None:
        entry = self._buy_warmups.pop(token_id, None)
        if entry is None:
            return None
        age = time.time() - entry["ts"]
        if age > self._BUY_WARMUP_TTL_S:
            return None
        if abs(entry["price"] - expected_price) > 0.01:
            return None
        if abs(entry["amount"] - size_usdc) / max(size_usdc, 1e-6) > 0.05:
            return None
        return entry["order"]

    def _take_sell_warmup(self, token_id: str, shares: float,
                          expected_price: float) -> dict | None:
        """Consume a pre-signed SELL order if it matches current parameters.

        Returns the signed-order dict or None if no usable warmup exists.
        Always drops the cache entry to prevent stale reuse — _submit_fok_order
        will re-issue via warm_sell_signature on the next tick if needed.
        """
        entry = self._sell_warmups.pop(token_id, None)
        if entry is None:
            return None
        age = time.time() - entry["ts"]
        if age > self._SELL_WARMUP_TTL_S:
            return None
        # Parameters must match closely. Price drift > 1 cent or size drift >
        # 5% means the SELL conditions changed enough that the signature
        # references the wrong amount — re-sign rather than risk a bad fill.
        if abs(entry["price"] - expected_price) > 0.01:
            return None
        if abs(entry["amount"] - shares) / max(shares, 1e-6) > 0.05:
            return None
        return entry["order"]

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
            approx_val = residual * (float(ref_price) if ref_price else 0.5)
            logger.warning(
                "Dust detected: %.2f shares (~$%.2f) — sweeping",
                residual, approx_val,
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
                logger.info("Dust swept: %.2f shares @ $%.2f", residual, safe_price)
            else:
                logger.warning(
                    "Dust sweep didn't match (status=%s) — shares left to resolve at expiry",
                    resp.get("status"),
                )
        except Exception as e:
            msg = str(e)
            if "not enough balance" in msg or "allowance" in msg:
                short = "balance too low for order size after fees"
            else:
                short = msg.split("\n")[0][:80]
            logger.warning("Dust sweep failed: %s — leaving as orphaned dust", short)

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

    _POSITIONS_API_URL = "https://data-api.polymarket.com/positions"
    _ORPHAN_MIN_SHARES = 1.0    # ignore < $0.50 dust (avoids flagging old swept-but-not-zeroed positions)
    _ORPHAN_LOOKBACK_HOURS = 2  # how far back to scan closed positions for known token_ids

    async def detect_orphan_positions(self, db: Database,
                                      allow_orphans: bool = False) -> int:
        """Query Polymarket data API for all on-chain positions and refuse to
        start if any chain position is not referenced by an open / recently-
        closed DB row.

        Strict mode (allow_orphans=False) raises ``OrphanPositionError`` on:
          - any non-dust chain position not referenced by an open/pending DB row
            (or a row closed in the last _ORPHAN_LOOKBACK_HOURS — sweep may
            not have completed before restart)
          - DB read failure (can't determine known token_ids → fail closed)
          - data-API failure (can't enumerate chain → fail closed)

        Lenient mode (--allow-orphans) logs CRITICAL but proceeds. Detected
        orphan details are persisted to ``memory/orphan_positions.json`` for
        operator review either way.

        Returns the count of orphans detected.
        """
        funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
        if not funder:
            msg = "Orphan detection: POLYMARKET_FUNDER env var not set — cannot enumerate chain positions"
            if allow_orphans:
                logger.warning("%s (continuing due to --allow-orphans)", msg)
                return 0
            raise OrphanPositionError(msg)

        # 1) Collect DB-known token_ids: open + pending + recently closed
        # (recently-closed catches the case where the close JUST happened but
        # the operator restarted before the bankroll/dust sweep ran).
        known_tokens: set[str] = set()
        try:
            cursor = await db.conn.execute(
                "SELECT indicator_snapshot FROM positions "
                "WHERE status IN ('open', 'pending_resolution') "
                "OR (status='closed' AND exit_timestamp >= datetime('now', ?))",
                (f"-{self._ORPHAN_LOOKBACK_HOURS} hours",),
            )
            rows = await cursor.fetchall()
            for row in rows:
                try:
                    snap = _json.loads(row[0] or "{}")
                    ctx = snap.get("trade_context", {}) if isinstance(snap, dict) else {}
                    for key in ("token_id_up", "token_id_down"):
                        tok = ctx.get(key)
                        if tok:
                            known_tokens.add(str(tok))
                except Exception:
                    continue
        except Exception as e:
            msg = f"Orphan detection: DB read failed: {e}"
            if allow_orphans:
                logger.warning("%s (continuing due to --allow-orphans)", msg)
                return 0
            raise OrphanPositionError(msg) from e

        # 2) Fetch chain positions from Polymarket's public data API
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.get(self._POSITIONS_API_URL, params={"user": funder})
                resp.raise_for_status()
                chain_positions = resp.json()
        except Exception as e:
            msg = f"Orphan detection: Polymarket positions API failed: {e}"
            if allow_orphans:
                logger.warning("%s (continuing due to --allow-orphans)", msg)
                return 0
            raise OrphanPositionError(
                f"{msg} — cannot verify chain state. Pass --allow-orphans to bypass."
            ) from e

        if not isinstance(chain_positions, list):
            msg = f"Orphan detection: unexpected positions API response type {type(chain_positions).__name__}"
            if allow_orphans:
                logger.warning("%s (continuing due to --allow-orphans)", msg)
                return 0
            raise OrphanPositionError(msg)

        # 3) Compare
        non_dust_chain = 0
        orphans: list[dict[str, Any]] = []
        for pos in chain_positions:
            if not isinstance(pos, dict):
                continue
            try:
                tok = str(pos.get("asset") or pos.get("token_id") or "")
                shares = float(pos.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            if not tok or shares < self._ORPHAN_MIN_SHARES:
                continue
            non_dust_chain += 1
            if tok not in known_tokens:
                orphans.append({
                    "token_id": tok,
                    "shares": round(shares, 4),
                    "outcome": str(pos.get("outcome") or ""),
                    "title": str(pos.get("title") or "")[:80],
                    "conditionId": str(pos.get("conditionId") or ""),
                })

        # 4) Persist details for operator review
        try:
            orphan_path = ORPHAN_POSITIONS_PATH
            orphan_path.parent.mkdir(parents=True, exist_ok=True)
            orphan_path.write_text(_json.dumps({
                "checked_at": _dt.now(_tz.utc).isoformat(),
                "funder": funder,
                "non_dust_chain_positions": non_dust_chain,
                "db_known_tokens": len(known_tokens),
                "orphans_detected": len(orphans),
                "orphans": orphans,
                "allow_orphans_flag": allow_orphans,
            }, indent=2))
        except Exception as e:
            logger.debug("Could not persist orphan_positions.json: %s", e)

        if not orphans:
            logger.info(
                "Orphan detection: %d non-dust chain position(s), all known to DB",
                non_dust_chain,
            )
            return 0

        # 5) Surface details and decide
        for o in orphans:
            logger.critical(
                "ORPHAN POSITION DETECTED: token=%s shares=%.2f outcome=%s title=%s",
                o["token_id"], o["shares"], o["outcome"], o["title"],
            )

        if allow_orphans:
            logger.critical(
                "ORPHAN DETECTION: %d orphan(s) — continuing due to --allow-orphans. "
                "These shares are NOT managed by the trading loop and will not appear "
                "in the pipeline pool. Sweep / resolve manually if needed.",
                len(orphans),
            )
            return len(orphans)

        raise OrphanPositionError(
            f"{len(orphans)} on-chain position(s) not known to DB. "
            "See memory/state/orphan_positions.json for details. "
            "After manual review, re-run with --allow-orphans to proceed."
        )

    async def reconcile_open(self, db: Database,
                             outcome_reviewer: Any = None,
                             signal_engine: Any = None) -> int:
        """Reconcile DB-open positions against on-chain balances.

        Two recovery paths:
          1) chain≤dust && db>dust → close-was-missed. Reconstruct exit_price
             best-effort (current CLOB mid → 1.0/0.0 if at extremes, else
             entry_price as zero-PnL fallback) and route through close_position
             so trade_history gets a real row + outcome_reviewer.record_outcome
             writes the per-trade JSON. exit_reason="reconcile_recovery" so the
             pipeline pool sees these as recovered rows (operator can choose to
             quarantine post-hoc by filtering on that reason).
          2) chain>dust && |chain-db|>0.5 → shares-held drifted. Update DB shares
             to chain truth, no close.

        The bankroll has already been synced to chain via set_bankroll(live_balance)
        before this method runs, so close_position is called without bankroll_delta
        to avoid double-counting.
        """
        changed = 0
        try:
            positions = await db.get_open_positions()
        except Exception as e:
            logger.warning("Reconcile open: DB read failed: %s", e)
            return 0
        for pos in positions:
            try:
                snap = _json.loads(pos.get("indicator_snapshot") or "{}")
                ctx = snap.get("trade_context", {}) if isinstance(snap, dict) else {}
                tok = ctx.get("token_id_up") if pos.get("side") == "Up" else ctx.get("token_id_down")
                if not tok:
                    continue
                chain_shares = await self._get_token_balance(tok)
                db_shares = float(pos.get("shares_held") or 0.0)
                if chain_shares <= _DUST_THRESHOLD_SHARES and db_shares > _DUST_THRESHOLD_SHARES:
                    await self._recover_missed_close(
                        db, pos, tok, db_shares,
                        outcome_reviewer=outcome_reviewer,
                        signal_engine=signal_engine,
                    )
                    changed += 1
                elif chain_shares > _DUST_THRESHOLD_SHARES and abs(chain_shares - db_shares) > 0.5:
                    logger.warning(
                        "Reconcile: position %d (%s %s) chain=%.4f db=%.4f - updating shares to match chain",
                        pos["id"], pos.get("market_id"), pos.get("side"), chain_shares, db_shares,
                    )
                    await db.conn.execute(
                        "UPDATE positions SET shares_held=? WHERE id=?",
                        (chain_shares, pos["id"]),
                    )
                    await db.conn.commit()
                    changed += 1
            except Exception as e:
                logger.debug("Reconcile open row %s skipped: %s", pos.get("id"), e)
                continue
        if changed:
            logger.warning("Reconcile open: %d position(s) synced to chain", changed)
        else:
            logger.info("Reconcile open: all positions match chain")
        return changed

    async def _recover_missed_close(self, db: Database, pos: dict, token_id: str,
                                    db_shares: float, outcome_reviewer: Any = None,
                                    signal_engine: Any = None) -> None:
        """Reconstruct a missed close: compute best-effort exit_price + PnL,
        persist via close_position (no bankroll delta — already synced from chain),
        and write the outcome JSON so the pipeline pool gets the recovered row.
        """
        entry_price = float(pos.get("entry_price") or 0.0)
        if entry_price <= 0:
            logger.warning(
                "Reconcile recovery: position %d has invalid entry_price, skipping outcome reconstruction",
                pos["id"],
            )
            return
        size_usdc = float(pos.get("size") or 0.0)
        fee_rate = float(pos.get("fee_rate") or DEFAULT_FEE_RATE)
        # --- Best-effort exit_price reconstruction ---
        exit_price, recovery_label = self._infer_recovery_exit_price(token_id, entry_price)
        # --- Replicate base.close_trade PnL math against the reconstructed price ---
        lr = log_return(entry_price, exit_price)
        fee_usdc = exit_fee_usdc(db_shares, exit_price, fee_rate)
        revenue = db_shares * exit_price - fee_usdc
        entry_fee_usd = _entry_fee_usd_from_position(pos, db_shares)
        pnl = revenue - size_usdc
        total_fees = entry_fee_usd + fee_usdc
        logger.warning(
            "Reconcile recovery: position %d (%s %s) — chain=0 db=%.4f → exit_price=%.4f (%s), pnl=%+.4f",
            pos["id"], pos.get("market_id"), pos.get("side"),
            db_shares, exit_price, recovery_label, pnl,
        )
        exit_reason = f"reconcile_recovery_{recovery_label}"
        # close_position writes trade_history + flips status='closed' atomically.
        # NO bankroll_delta — set_bankroll(live_balance) ran before reconcile and
        # already reflects the on-chain truth; adding revenue here would double-count.
        try:
            await db.close_position(
                pos["id"], exit_price=exit_price,
                pnl=pnl, fees=total_fees, exit_reason=exit_reason,
            )
        except Exception as e:
            logger.error(
                "Reconcile recovery: db.close_position failed for position %d: %s — falling back to status-only close",
                pos["id"], e,
            )
            try:
                await db.conn.execute(
                    "UPDATE positions SET status='closed', exit_price=?, exit_timestamp=? WHERE id=?",
                    (exit_price, _dt.now(_tz.utc).isoformat(), pos["id"]),
                )
                await db.conn.commit()
            except Exception as inner:
                logger.error("Reconcile recovery: even fallback close failed: %s", inner)
            return
        # Write the per-trade outcome JSON so the pipeline can see the recovered
        # row. Best-effort: if outcome_reviewer isn't wired in this restart,
        # the trade_history row still exists and the pipeline has fallback paths.
        if outcome_reviewer is None:
            logger.debug("Reconcile recovery: outcome_reviewer not provided, skipping JSON write")
            return
        try:
            snap = _json.loads(pos.get("indicator_snapshot") or "{}")
            ctx = snap.get("trade_context", {}) if isinstance(snap, dict) else {}
            signal_score = float(ctx.get("model_probability") or 0.5)
            profitable = pnl > 0
            outcome_reviewer.record_outcome(
                position_id=pos["id"],
                market_id=pos.get("market_id", ""),
                question=pos.get("question", ""),
                side=pos.get("side", ""),
                signal_score=signal_score,
                profitable=profitable,
                entry_price=entry_price,
                exit_price=exit_price,
                log_return=lr,
                indicator_snapshot=snap,
                exit_reason=exit_reason,
                size=size_usdc,
                pnl=pnl,
                fees=total_fees,
                exit_timestamp=_dt.now(_tz.utc).isoformat(),
                seconds_remaining_at_exit=0.0,
                edge_decay=None,
            )
        except Exception as e:
            logger.debug("Reconcile recovery: outcome JSON write failed for position %d: %s", pos["id"], e)

    def _infer_recovery_exit_price(self, token_id: str, entry_price: float) -> tuple[float, str]:
        """Pick a best-effort exit_price for a missed close.

        Returns (price, label). Label is appended to exit_reason so the operator
        can tell which inference path fired:
          - "resolution_win"  : CLOB mid > 0.90, position resolved at $1
          - "resolution_loss" : CLOB mid < 0.10, position resolved at $0
          - "mid"             : CLOB has a quote in [0.10, 0.90], use mid
          - "unknown"         : No quote; fall back to entry_price (zero PnL).
        """
        if self._clob_ws is None or not hasattr(self._clob_ws, "best_bid_ask"):
            return entry_price, "unknown"
        bba = self._clob_ws.best_bid_ask.get(token_id, {}) if hasattr(self._clob_ws, "best_bid_ask") else {}
        try:
            bid = float(bba.get("best_bid") or 0)
            ask = float(bba.get("best_ask") or 0)
        except (TypeError, ValueError):
            return entry_price, "unknown"
        if bid <= 0 or ask <= 0:
            return entry_price, "unknown"
        mid = (bid + ask) / 2.0
        if mid > 0.90:
            return 1.0, "resolution_win"
        if mid < 0.10:
            return 0.0, "resolution_loss"
        return round(mid, 4), "mid"

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
            reason = f"Order ${notional_usd:.2f} below ${_MIN_ORDER_USD:.2f} CLOB minimum"
            _update_fill_stats(filled=False, side=side, reason=reason)
            return FillResult(filled=False, reason=reason)

        # Order-book pre-check: simulate FOK walk against the current book.
        # Avoids burning ~770ms × 3 retries on orders that would reject anyway
        # because the book has already moved past our limit. Best-effort: skip
        # the precheck if we don't have a fresh book snapshot (let the FOK try).
        if self._clob_ws is not None and hasattr(self._clob_ws, "get_book"):
            book = self._clob_ws.get_book(token_id) or {}
            book_ts = float(book.get("ts", 0) or 0)
            book_age = time.time() - book_ts if book_ts else 999
            if book_age <= 5.0:  # only trust very fresh book for pre-check
                walks = self._estimate_fok_walk(book, side, amount, expected_price)
                if walks is False:
                    reason = "pre-check: book walk would exceed limit (would reject in CLOB)"
                    _update_fill_stats(filled=False, side=side, reason=reason)
                    return FillResult(filled=False, reason=reason)

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
        if side == SELL:
            presigned = self._take_sell_warmup(token_id, amount, expected_price)
        else:
            presigned = self._take_buy_warmup(token_id, amount, expected_price)
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                if attempt == 1 and presigned is not None:
                    # POST the pre-signed order directly, skip the sign step.
                    def _post_presigned() -> tuple[dict, float, float]:
                        _p0 = time.perf_counter()
                        r = self.client.post_order(presigned, OrderType.FOK)
                        return r, 0.0, time.perf_counter() - _p0
                    _lat_t0 = time.perf_counter()
                    resp, sign_s, post_s = await loop.run_in_executor(
                        self._sign_executor, _post_presigned)
                    _record_submit_latency(time.perf_counter() - _lat_t0, sign_s, post_s)
                else:
                    mo = MarketOrderArgs(token_id=token_id, amount=amount, side=side, price=expected_price)
                    # Sign and post in one thread dispatch on the dedicated executor,
                    # with per-leg timing so the post-distribution shape is observable.
                    def _sign_and_post(order_args: MarketOrderArgs) -> tuple[dict, float, float]:
                        _s0 = time.perf_counter()
                        signed = self.client.create_market_order(order_args)
                        _s1 = time.perf_counter()
                        r = self.client.post_order(signed, OrderType.FOK)
                        return r, _s1 - _s0, time.perf_counter() - _s1
                    _lat_t0 = time.perf_counter()
                    resp, sign_s, post_s = await loop.run_in_executor(
                        self._sign_executor, _sign_and_post, mo)
                    _record_submit_latency(time.perf_counter() - _lat_t0, sign_s, post_s)

                if not resp.get("success"):
                    error_msg = resp.get("errorMsg", "unknown error")
                    if _looks_like_auth_error(error_msg):
                        logger.error("AUTH FAILURE — Polymarket rejected order: %s", error_msg)
                        raise AuthError(error_msg)
                    # Non-retryable errors bail immediately
                    if any(code in error_msg for code in _NON_RETRYABLE_ERRORS):
                        logger.error("Order rejected (non-retryable): %s", error_msg)
                        _update_fill_stats(filled=False, side=side, reason=f"non-retryable: {error_msg}")
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
                                        # Return gross VWAP. base.py applies the fee uniformly
                                        # (entry_fee_shares on gross_shares) and records the
                                        # correct net_shares. The earlier net-shares synthetic
                                        # price caused base.py to deduct the fee a second time,
                                        # leaving shares_held under-reported by ~0.45% (the
                                        # difference showed up as on-chain dust).
                                        fill_price = gross_vwap
                                        logger.debug(
                                            "BUY WS-derived VWAP: %d trade(s), "
                                            "gross_shares=%.4f @ %.4f",
                                            len(candidates), gross_shares, gross_vwap,
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
                                    # delta is net_shares (post-fee chain balance change). We
                                    # need gross_vwap = amount / gross_shares so base.py can
                                    # apply the fee correctly. Solve via 2 fixed-point steps:
                                    #   gross_shares ≈ delta / (1 - fee_rate * p * (1-p))
                                    # Converges in 1-2 iterations for fee_rate=0.018, p≈0.5.
                                    p_est = amount / delta
                                    for _ in range(2):
                                        fee_frac = fee_rate * p_est * (1.0 - p_est)
                                        gross_shares = delta / max(1.0 - fee_frac, 1e-6)
                                        p_est = amount / gross_shares
                                    fill_price = p_est
                                    logger.debug(
                                        "BUY balance-delta VWAP fallback: net=%.4f -> "
                                        "gross_vwap=%.4f (before=%.4f after=%.4f notional=%.2f)",
                                        delta, fill_price, balance_before, balance_after, amount,
                                    )
                    if fill_price is None:
                        fill_price = await self._get_fill_price(order_id, expected_price)
                    order_short = (f"{order_id[:6]}…{order_id[-4:]}"
                                   if isinstance(order_id, str) and len(order_id) > 12
                                   else order_id)
                    # py-clob amount semantics: USDC notional on BUY, shares on SELL.
                    qty_str = (f"notional=${amount:.2f}" if side == BUY
                               else f"shares={amount:.2f}")
                    logger.info(
                        "FOK %s filled: order=%s, price=$%.2f, %s",
                        side, order_short, fill_price, qty_str,
                    )
                    _update_fill_stats(filled=True, side=side)
                    # Fire-and-forget: the recheck only logs a warning when allowance
                    # drops below threshold; blocking the FOK return path on it cost
                    # 30-100ms every 10th submit for zero trading-logic benefit.
                    asyncio.create_task(self._maybe_recheck_allowance())
                    if side == BUY:
                        # Background prefetch of post-BUY chain balance — primes
                        # the cache so the eventual SELL's _sellable_shares can
                        # skip the ~300ms /balance-allowance REST roundtrip.
                        asyncio.create_task(self._cache_post_buy_balance(token_id))
                    else:
                        # Sell just succeeded — cached balance is now stale.
                        self._invalidate_balance_cache(token_id)
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
        # Bucket by what actually happened last — the in-loop catch overwrites
        # last_error with each attempt's specific failure (price_moved vs
        # network vs other), so the final value is the truest signal of cause.
        _update_fill_stats(filled=False, side=side, reason=last_error or "price moved")
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
                # py-clob occasionally returns None on transient REST hiccups; guard
                # so .get() doesn't blow up the retry loop. Falls through to retry.
                if order is None:
                    if attempt < _FILL_PRICE_LOOKUP_RETRIES - 1:
                        await asyncio.sleep(_FILL_PRICE_LOOKUP_DELAY)
                        continue
                    return fallback_price
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
