"""Live trader stub — polymarket.com CLOB trading requires EIP-712 signed orders.

Paper mode works with real CLOB order book prices. Live .com trading is future work.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IMPLEMENTATION BLUEPRINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# LiveTrader is a DROP-IN replacement for PaperTrader. Same method
# signatures, same TradeResult return type. Everything upstream
# (signal engine, sizing, gates) and downstream (outcome recording,
# circuit breaker, learning pipeline) stays UNTOUCHED.
#
# The boundary contract is TradeResult — as long as live returns the
# same shape, the entire system works. main.py should only need to
# swap `PaperTrader(db, ...)` for `LiveTrader(db, ...)` based on
# the --mode flag.
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. EIP-712 ORDER SIGNING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Polymarket CLOB requires EIP-712 typed-data signatures for every
# order. The domain and type schema are defined by Polymarket's
# Exchange contract on Polygon.
#
# Order struct fields:
#   salt          — random uint256 (prevent replay)
#   maker         — wallet address (checksummed)
#   signer        — same as maker for EOA, or delegate for smart wallet
#   taker         — 0x0 (any taker) or specific address
#   tokenId       — CLOB token_id (the outcome token being traded)
#   makerAmount   — amount of outcome tokens (in wei-like units)
#   takerAmount   — amount of USDC (in USDC units, 6 decimals)
#   expiration    — unix timestamp (set to window end + buffer)
#   nonce         — auto-incrementing per wallet
#   feeRateBps    — fee rate in basis points (from GET /fee-rate)
#   side          — "BUY" or "SELL"
#   signatureType — 0 for EOA (eth_signTypedData)
#
# Signing flow:
#   1. Load private key from .env (POLYMARKET_PRIVATE_KEY)
#   2. Build EIP-712 domain: {name, version, chainId, verifyingContract}
#   3. Build order struct with all fields above
#   4. Sign with eth_account.messages.encode_typed_data + key.sign_message
#   5. Attach signature to order payload
#
# Libraries: eth-account (py), ethers.js (if node sidecar)
# Keep the key HOT in memory — signing is ~10-20ms, don't add I/O.
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. ORDER SUBMISSION & FILL HANDLING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Submit: POST {CLOB_API}/orders with signed order JSON.
# Response: {orderID, status, ...}
#
# Fill confirmation options (choose one):
#   A. Poll GET /orders/{orderID} until status = "FILLED" or "EXPIRED"
#   B. Listen on CLOB WebSocket for order_fill events (if supported)
#   C. Optimistic: assume fill if POST returns 200 + orderID, verify
#      later via balance check
#
# Recommended: Option A with timeout. Poll every 500ms, max 5s.
# If not filled in 5s, cancel and log as rejected.
#
# Fill-or-kill (FOK) semantics:
#   Paper trader uses FOK — 100% fill or reject. Live should match.
#   If Polymarket supports FOK order type, use it. Otherwise, set
#   expiration to ~10s from now to simulate FOK.
#
# Partial fills:
#   If FOK is not available and partial fills occur:
#   - DB insert with actual filled size (not requested size)
#   - Bankroll debit = actual cost, not estimated cost
#   - shares_held = actual shares received
#   - Do NOT resubmit the remainder — one clean fill per signal
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. open_trade() TRANSLATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Paper:                           Live:
#   Instant mock fill at price       Build order (price as limit)
#   shares = size / price            Sign EIP-712
#   fee deducted in shares           POST /orders
#   DB insert immediately            Wait for fill confirmation
#   bankroll -= size                 Parse actual fill:
#                                      fill_price (may differ)
#                                      fill_size (may be partial)
#                                      actual_fee
#                                    DB insert with ACTUAL fill data
#                                    bankroll -= actual cost
#
# INVARIANT TO PRESERVE:
#   - Entry fee is collected in SHARES (fewer shares received)
#   - Bankroll is debited by USDC spent only
#   - shares_held in DB = actual shares after fee deduction
#   - All 3 rejection gates (duplicate, max positions, bankroll cap)
#     must run BEFORE submitting the order to the exchange
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. close_trade() TRANSLATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Paper:                           Live:
#   Instant mock sell at price       Build SELL order
#   revenue = shares * exit - fee    Sign EIP-712
#   bankroll += revenue              POST /orders (SELL side)
#                                    Wait for fill confirmation
#                                    revenue = actual proceeds - fee
#                                    bankroll += actual revenue
#
# INVARIANT TO PRESERVE:
#   - Exit fee is collected in USDC (subtracted from proceeds)
#   - revenue = shares * actual_fill_price - exit_fee_usdc
#   - Bankroll credited by revenue, not raw proceeds
#
# EDGE CASE: If the sell order doesn't fill before contract expiry,
# the position resolves on-chain at $0 or $1. Detect this via the
# CLOB WS market_resolved event and fall through to resolve_position().
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. resolve_position() TRANSLATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Paper:                           Live:
#   exit_price = 1.0 or 0.0         On-chain resolution event
#   fee = $0 (formula gives 0)      Call redeemPositions() on
#   revenue = shares * exit_price      ConditionalTokens contract
#   bankroll += revenue              Actual USDC received (- gas)
#                                    bankroll += actual payout
#
# Resolution mechanics:
#   - Polymarket resolves via UMA oracle → CTF (ConditionalTokens)
#   - Winner tokens redeemable 1:1 for USDC via redeemPositions()
#   - Loser tokens worth $0 (no action needed, or redeem for $0)
#   - Gas cost on Polygon: ~$0.01-0.05 (negligible)
#   - Fee formula gives $0 at p=0 or p=1 extremes (by design)
#
# Detection: CLOB WS fires market_resolved event. Also check Gamma
# API closed=True + outcomePrices. Use whichever fires first.
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. BANKROLL & BALANCE MANAGEMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Paper: SQLite bankroll table is the single source of truth.
#
# Live:
#   - On startup: fetch USDC balance from Polymarket API, sync to DB
#   - After each trade: update DB with actual cost/revenue
#   - Periodic reconciliation (every N minutes): compare DB bankroll
#     vs on-chain USDC balance. Log divergence. If > 1% drift, warn
#     via Discord and pause trading until manually resolved.
#   - Handle external deposits/withdrawals: reconciliation catches
#     these as positive drift (deposit) or negative (withdrawal).
#
# API for balance: Polymarket provides wallet balance endpoints, or
# query the USDC contract on Polygon directly via RPC.
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. NONCE & REPLAY PROTECTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Each order requires a unique nonce. Options:
#   A. Incrementing counter (persist to DB, load on startup)
#   B. Timestamp-based (milliseconds since epoch — good enough for
#      1 order at a time)
#   C. Query Polymarket API for next valid nonce
#
# Since max_concurrent_positions = 1, nonce collisions are unlikely.
# Use option B with a fallback increment on 409 conflict.
#
# The salt field is separate — use os.urandom(32) for each order.
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. SLIPPAGE: PAPER vs LIVE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Paper: Convex slippage model estimates market impact.
# Live:  Actual fill price comes from the exchange — no simulation.
#
# BUT the convex model is still useful in live mode:
#   - Pre-trade: estimate slippage for net-edge gate (reject if edge
#     doesn't survive estimated cost). This runs in main.py, not here.
#   - Post-trade: compare estimated vs actual slippage. Log the delta.
#     If the model consistently under/overestimates, adjust
#     slippage_impact_pct in settings.yaml.
#
# Calibration metric: track (estimated_fill - actual_fill) per trade.
# Mean should be near zero. Persistent bias → update impact_factor.
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9. ERROR HANDLING & SAFETY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Order rejected by CLOB:
#   → Return TradeResult(success=False, reason=error_message)
#   → Do NOT retry automatically. Signal may be stale by now.
#   → Log rejection reason for post-session analysis.
#
# Network timeout during order submission:
#   → DANGER: order may or may not have been received.
#   → Query GET /orders?market=X to check if order exists.
#   → If order exists and filled: update DB with actual fill.
#   → If order exists and pending: cancel it (POST /cancel).
#   → If no order found: safe to retry once.
#
# Network timeout during fill polling:
#   → Less dangerous. Order is on the exchange either way.
#   → Keep polling with backoff. Don't assume failure.
#
# Private key compromise:
#   → If .env is exposed, attacker can sign orders as our wallet.
#   → Mitigation: use a dedicated trading wallet with limited funds.
#   → Never store the main wallet key. Fund the trading wallet with
#     only what you're willing to risk.
#
# Stale position after crash:
#   → On startup, query Polymarket for open orders and token balances.
#   → Reconcile with DB open positions. Close any DB positions whose
#     tokens are no longer held (already sold or resolved on-chain).
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 10. NEW COMPONENTS NEEDED
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# These can live in this file or in execution/ submodules:
#
# EIP712Signer:
#   - __init__(private_key: str)  — load key, precompute domain
#   - sign_order(order: dict) -> str  — returns hex signature
#   - Keep key in memory, never log it, never serialize it
#
# OrderManager:
#   - submit_order(signed_order) -> str  — POST /orders, return orderID
#   - poll_fill(order_id, timeout=5.0) -> dict  — poll until filled/expired
#   - cancel_order(order_id) -> bool  — POST /cancel
#   - Uses persistent aiohttp session (connection pooling)
#
# BalanceManager:
#   - fetch_usdc_balance() -> float  — query Polymarket or Polygon RPC
#   - reconcile(db_bankroll: float) -> float  — compare, log drift
#   - Called on startup and periodically during session
#
# NonceTracker:
#   - next_nonce() -> int  — timestamp-based with collision fallback
#   - Lightweight, no DB needed for single-position mode
#
# FillValidator:
#   - compare(estimated_price, actual_price, estimated_slip, size)
#   - Logs divergence for slippage model calibration
#   - Accumulates stats for periodic reporting
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 11. LATENCY OPTIMIZATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Paper trader fills in ~2ms. Real execution is ~200-500ms. On 5-min
# contracts, that gap can erase your edge. Target: <120ms signal-to-fill.
#
# --- BIGGEST WINS (save 100-200ms) ---
#
# 1. PRE-SIGN ORDERS
#    EIP-712 signing is pure CPU. Keep the signing key hot in memory
#    and pre-compute partial signatures. Don't do crypto ops after
#    the signal fires.
#
# 2. PERSISTENT HTTP CONNECTIONS
#    Don't open a new TCP+TLS handshake per order. Use aiohttp
#    connection pooling with a keep-alive session to Polymarket's
#    CLOB endpoint. Saves ~80-150ms per request.
#
# 3. PRE-BUILD ORDER TEMPLATES
#    Have the order JSON 90% assembled before the signal. When signal
#    fires, just slot in price/size/side and send.
#
# --- MEDIUM WINS (save 30-80ms) ---
#
# 4. COLOCATE NEAR POLYMARKET
#    A small VPS in the same AWS region as Polymarket (likely us-east-1)
#    cuts network round-trip vs running from a home PC.
#
# 5. OPTIMISTIC ORDER PREP
#    If Layer 1 (CDF) alone shows a big edge, start preparing the
#    order while Layers 2-4 compute. Cancel if they disagree.
#
# 6. SKIP /price HTTP CALL AT ENTRY
#    You already have the book from WebSocket. Compute the walk-through
#    price locally from in-memory book state instead of another HTTP
#    round-trip. Save that ~100-200ms.
#
# --- SMALLER WINS ---
#
# 7. Check if Polymarket supports WebSocket order submission (avoids
#    HTTP overhead entirely for order placement).
#
# 8. Ensure no pre-checks (tick size, fee rate, min order size) trigger
#    a synchronous HTTP fetch at order time — they're already cached.
#
# --- TARGET ARCHITECTURE ---
#
#   WebSocket update arrives
#     → signal_engine computes edge (~5ms)
#     → if edge passes gates:
#         → grab pre-built order template (0ms)
#         → slot in price/size from local book state (no HTTP)
#         → sign with hot key (~10-20ms)
#         → fire on persistent connection (~50-100ms to server)
#     → realistic floor: ~60-120ms signal-to-fill
#
# The bot's 10%+ min-edge requirement helps here — you're not chasing
# tiny arbs that vanish in milliseconds. Structural mispricings from
# the fat-tail model persist longer than speed arbs. But order-flow
# signals (Layer 3) decay fast, so speed still matters.
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 12. ENV VARIABLES NEEDED
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# POLYMARKET_PRIVATE_KEY  — Polygon wallet private key (hex, no 0x prefix)
# POLYMARKET_WALLET_ADDR  — Corresponding checksummed wallet address
# POLYGON_RPC_URL         — Polygon RPC for balance queries / tx submission
#                           (e.g., https://polygon-rpc.com or Alchemy/Infura)
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 13. DEPENDENCIES TO ADD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# eth-account     — EIP-712 signing (eth_account.messages.encode_typed_data)
# web3            — Polygon RPC queries (balance, tx submission, gas)
# aiohttp         — persistent connection pooling for CLOB order endpoint
#                   (already used elsewhere — reuse the session)
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import logging
from polybot.db.models import Database
from polybot.execution.base import TradeResult

logger = logging.getLogger(__name__)


class LiveTrader:
    """Drop-in replacement for PaperTrader. Same interface, real CLOB orders.

    Method signatures MUST match PaperTrader exactly — main.py calls the
    same methods regardless of mode. The TradeResult dataclass is the
    contract boundary.
    """

    def __init__(self, db: Database, **kwargs):
        raise NotImplementedError(
            "Live trading on polymarket.com requires EIP-712 signed orders. "
            "Use --mode paper. Live .com trader is future work."
        )

    async def open_trade(
        self, market_id: str, question: str, side: str, price: float,
        size: float, signal_score: float, signal_strength: str,
        ev_at_entry: float, exit_target: float, stop_loss: float,
        weight_version: str, indicator_snapshot: str = "",
        token_id: str = "", fee_rate: float = 0.018,
    ) -> TradeResult:
        # Implementation flow:
        #   1. Run same 3 rejection gates as PaperTrader (duplicate, max pos, bankroll)
        #   2. Build EIP-712 order struct (BUY side, token_id, size, price as limit)
        #   3. Sign with hot key
        #   4. POST /orders
        #   5. Poll for fill (5s timeout)
        #   6. On fill: compute actual shares (after fee), insert DB, debit bankroll
        #   7. On reject/timeout: return TradeResult(success=False, reason=...)
        raise NotImplementedError

    async def close_trade(
        self, position_id: int, exit_price: float, token_id: str = "",
    ) -> TradeResult:
        # Implementation flow:
        #   1. Fetch position from DB
        #   2. Build EIP-712 order struct (SELL side, shares_held as size)
        #   3. Sign and submit
        #   4. Poll for fill (5s timeout)
        #   5. On fill: compute revenue (actual_fill * shares - exit_fee), credit bankroll
        #   6. If not filled before expiry: fall through to resolve_position()
        raise NotImplementedError

    async def resolve_position(self, position_id: int, exit_price: float) -> TradeResult:
        # Implementation flow:
        #   1. Fetch position from DB
        #   2. If exit_price == 1.0 (winner): call redeemPositions() on CTF contract
        #   3. If exit_price == 0.0 (loser): no redemption needed, just close DB
        #   4. Credit bankroll with actual USDC received (payout - gas)
        #   5. Close position in DB
        raise NotImplementedError
