# Abstract Base Trader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify PaperTrader and LiveTrader behind an abstract base class so all trading logic (rejection gates, fee math, DB operations) is shared. The ONLY difference between paper and live is order execution.

**Architecture:** BaseTrader ABC in base.py owns all shared logic. PaperTrader implements instant fills. LiveTrader implements FOK market orders via py-clob-client SDK with retry. Fee functions move from paper_trader.py to base.py since both traders and main.py use them.

**Tech Stack:** Python 3.14, asyncio, py-clob-client SDK, SQLite (aiosqlite), pytest + pytest-asyncio

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `polybot/execution/base.py` | **Rewrite** | TradeResult, FillResult, fee functions, BaseTrader ABC |
| `polybot/execution/paper_trader.py` | **Rewrite** | PaperTrader(BaseTrader) — 3 abstract method implementations only |
| `polybot/execution/live_trader.py` | **Rewrite** | LiveTrader(BaseTrader) — FOK market orders + retry, verify_auth helper |
| `polybot/main.py` | **Modify** (1 import line) | Update fee function import path |
| `polybot/tests/test_paper_trader.py` | **Modify** (1 import line) | Update fee function import path |
| `polybot/tests/test_live_trader.py` | **Rewrite** | Update mocks for FOK (no more GTC polling) |

## Key SDK Facts (from py-clob-client research)

- `OrderArgs.size` = **number of shares** (NOT USDC). Current code has bugs passing USDC and `shares * price`.
- `MarketOrderArgs.amount` = **USDC for BUY**, **shares for SELL**.
- `create_market_order(mo)` fetches order book internally, computes market price, returns signed order.
- `post_order(signed, OrderType.FOK)` fills immediately or fails entirely. No polling needed.
- Response: `{"success": true, "status": "matched", "orderID": "0x...", "tradeIDs": [...]}`.
- Non-retryable errors: `INVALID_ORDER_NOT_ENOUGH_BALANCE`, `MARKET_NOT_READY`.
- `get_order(orderID)` returns `associate_trades` array with `{price, size}` per fill (size = shares).

---

### Task 1: Rewrite base.py with FillResult and BaseTrader ABC

**Files:**
- Rewrite: `polybot/execution/base.py`

- [ ] **Step 1: Write a test for BaseTrader shared behavior**

Create a minimal test that instantiates a concrete subclass and verifies the rejection gates work identically to the current PaperTrader. This test will break if the base class API changes in the future.

```python
# polybot/tests/test_base_trader.py
import pytest
import pytest_asyncio
from polybot.execution.base import BaseTrader, FillResult, TradeResult, DEFAULT_FEE_RATE
from polybot.db.models import Database


class StubTrader(BaseTrader):
    """Minimal concrete subclass for testing shared base logic."""

    async def _execute_buy(self, token_id, price, size):
        return FillResult(filled=True, fill_price=price, fill_size=size)

    async def _execute_sell(self, token_id, shares, price):
        return FillResult(filled=True, fill_price=price)

    async def _resolve_bankroll(self, position, exit_price):
        from polybot.execution.base import exit_fee_usdc
        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE
        fee = exit_fee_usdc(shares, exit_price, fee_rate)
        revenue = shares * exit_price - fee
        bankroll = await self.db.get_bankroll()
        return bankroll + revenue


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def trader(db):
    return StubTrader(db=db, max_slippage=0.02, max_bankroll_deployed=0.80, max_concurrent_positions=5)


@pytest.mark.asyncio
async def test_open_trade_success(trader):
    result = await trader.open_trade(
        market_id="m1", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72, signal_strength="high",
        ev_at_entry=0.17, exit_target=0.68, stop_loss=0.47, weight_version="v001",
    )
    assert result.success is True
    assert result.position_id is not None


@pytest.mark.asyncio
async def test_rejects_duplicate_market(trader):
    await trader.open_trade(
        market_id="m1", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72, signal_strength="high",
        ev_at_entry=0.17, exit_target=0.68, stop_loss=0.47, weight_version="v001",
    )
    result = await trader.open_trade(
        market_id="m1", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72, signal_strength="high",
        ev_at_entry=0.17, exit_target=0.68, stop_loss=0.47, weight_version="v001",
    )
    assert result.success is False
    assert "duplicate" in result.reason.lower()


@pytest.mark.asyncio
async def test_rejects_max_positions(trader, db):
    for i in range(5):
        await trader.open_trade(
            market_id=f"m_{i}", question="Q?", side="YES", price=0.55,
            size=5.0, signal_score=0.72, signal_strength="high",
            ev_at_entry=0.17, exit_target=0.68, stop_loss=0.47, weight_version="v001",
        )
    result = await trader.open_trade(
        market_id="m_6", question="Q?", side="YES", price=0.55,
        size=5.0, signal_score=0.72, signal_strength="high",
        ev_at_entry=0.17, exit_target=0.68, stop_loss=0.47, weight_version="v001",
    )
    assert result.success is False
    assert "max positions" in result.reason.lower()


@pytest.mark.asyncio
async def test_rejects_bankroll_exceeded(trader):
    result = await trader.open_trade(
        market_id="m_big", question="Q?", side="YES", price=0.55,
        size=85.0, signal_score=0.72, signal_strength="high",
        ev_at_entry=0.17, exit_target=0.68, stop_loss=0.47, weight_version="v001",
    )
    assert result.success is False
    assert "bankroll" in result.reason.lower()


@pytest.mark.asyncio
async def test_close_trade_updates_bankroll(trader, db):
    r = await trader.open_trade(
        market_id="m1", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72, signal_strength="high",
        ev_at_entry=0.17, exit_target=0.68, stop_loss=0.47, weight_version="v001",
    )
    close = await trader.close_trade(r.position_id, exit_price=0.68)
    assert close.success is True
    bankroll = await db.get_bankroll()
    assert bankroll > 100.0


@pytest.mark.asyncio
async def test_resolve_position_win(trader, db):
    r = await trader.open_trade(
        market_id="m1", question="Q?", side="YES", price=0.50,
        size=50.0, signal_score=0.72, signal_strength="high",
        ev_at_entry=0.17, exit_target=1.0, stop_loss=0.0, weight_version="v001",
    )
    resolve = await trader.resolve_position(r.position_id, exit_price=1.0)
    assert resolve.success is True
    bankroll = await db.get_bankroll()
    # Won: bankroll should be > starting 100
    assert bankroll > 100.0


@pytest.mark.asyncio
async def test_resolve_position_loss(trader, db):
    r = await trader.open_trade(
        market_id="m1", question="Q?", side="YES", price=0.50,
        size=50.0, signal_score=0.72, signal_strength="high",
        ev_at_entry=0.17, exit_target=1.0, stop_loss=0.0, weight_version="v001",
    )
    resolve = await trader.resolve_position(r.position_id, exit_price=0.0)
    assert resolve.success is True
    bankroll = await db.get_bankroll()
    # Lost: bankroll = 100 - 50 + 0 = 50
    assert bankroll == pytest.approx(50.0, abs=0.01)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest polybot/tests/test_base_trader.py -v`
Expected: ImportError — `BaseTrader` and `FillResult` don't exist yet.

- [ ] **Step 3: Write the full base.py**

Replace `polybot/execution/base.py` with:

```python
"""Shared trading infrastructure — fee math, rejection gates, DB operations.

BaseTrader is the abstract base class for PaperTrader and LiveTrader.
Subclasses implement only _execute_buy, _execute_sell, and _resolve_bankroll.
Everything else (gates, fees, DB writes, bankroll tracking) is shared.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging

from polybot.db.models import Database
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    success: bool
    position_id: int | None = None
    reason: str = ""
    log_return: float | None = None


@dataclass
class FillResult:
    """Result of an order execution attempt.

    Attributes:
        filled: Whether the order was filled.
        fill_price: Actual execution price.
        fill_size: USDC spent (buys only — sells don't use this).
        reason: Human-readable failure reason if not filled.
    """
    filled: bool
    fill_price: float = 0.0
    fill_size: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Fee model — Polymarket dynamic taker-fee
# ---------------------------------------------------------------------------

DEFAULT_FEE_RATE = 0.018  # Crypto taker fee: 1.8% peak (March 2026)


def taker_fee(shares: float, price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """Polymarket fee: feeRate * shares * p * (1-p). Zero at extremes, max at p=0.50."""
    return round(fee_rate * shares * price * (1.0 - price), 6)


def entry_fee_shares(shares_ordered: float, price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """On buys, Polymarket collects fee in shares. Returns shares deducted."""
    fee_dollars = taker_fee(shares_ordered, price, fee_rate)
    return fee_dollars / price if price > 0 else 0.0


def exit_fee_usdc(shares: float, price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """On sells, Polymarket collects fee in USDC. Returns USDC deducted."""
    return taker_fee(shares, price, fee_rate)


# ---------------------------------------------------------------------------
# Abstract base trader
# ---------------------------------------------------------------------------

class BaseTrader(ABC):
    """Shared trading logic. Subclasses implement only order execution."""

    def __init__(self, db: Database, max_slippage: float = 0.02,
                 max_bankroll_deployed: float = 0.80, max_concurrent_positions: int = 1):
        self.db = db
        self.max_slippage = max_slippage
        self.max_bankroll_deployed = max_bankroll_deployed
        self.max_concurrent_positions = max_concurrent_positions

    async def _get_deployed_capital(self) -> float:
        positions = await self.db.get_open_positions()
        return sum(p["size"] for p in positions)

    # ------------------------------------------------------------------
    # open_trade — shared gates, then abstract execution
    # ------------------------------------------------------------------

    async def open_trade(self, market_id, question, side, price, size, signal_score,
                         signal_strength, ev_at_entry, exit_target, stop_loss, weight_version,
                         indicator_snapshot: str = "", token_id: str = "",
                         fee_rate: float = DEFAULT_FEE_RATE) -> TradeResult:
        # --- Rejection gates (shared) ---
        if await self.db.has_position_for_market(market_id):
            return TradeResult(success=False, reason="Duplicate market — already have position")
        if await self.db.get_open_position_count() >= self.max_concurrent_positions:
            return TradeResult(success=False, reason="Max positions reached")
        bankroll = await self.db.get_bankroll()
        deployed = await self._get_deployed_capital()
        max_deployable = bankroll * self.max_bankroll_deployed
        if deployed + size > max_deployable:
            return TradeResult(success=False, reason=f"Bankroll limit — deployed {deployed:.2f}, max {max_deployable:.2f}")

        # --- Execute buy (abstract — paper simulates, live submits to CLOB) ---
        fill = await self._execute_buy(token_id, price, size)
        if not fill.filled:
            return TradeResult(success=False, reason=fill.reason)

        # --- Fee math (shared) ---
        # Entry fee collected in SHARES: you pay fill_size USDC, receive fewer shares
        shares_ordered = fill.fill_size / fill.fill_price
        fee_in_shares = entry_fee_shares(shares_ordered, fill.fill_price, fee_rate)
        shares_received = shares_ordered - fee_in_shares

        # --- Persist to DB (shared) ---
        pos_id = await self.db.open_position(
            market_id=market_id, question=question, side=side,
            entry_price=fill.fill_price, size=fill.fill_size,
            signal_score=signal_score, signal_strength=signal_strength,
            ev_at_entry=ev_at_entry, exit_target=exit_target,
            stop_loss=stop_loss, weight_version=weight_version,
            indicator_snapshot=indicator_snapshot,
            fee_rate=fee_rate, shares_held=shares_received,
        )
        await self.db.set_bankroll(bankroll - fill.fill_size)
        return TradeResult(success=True, position_id=pos_id)

    # ------------------------------------------------------------------
    # close_trade — shared lookup + fee math, then abstract execution
    # ------------------------------------------------------------------

    async def close_trade(self, position_id: int, exit_price: float, token_id: str = "") -> TradeResult:
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False, reason=f"Position {position_id} not found or already closed")

        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE

        # --- Execute sell (abstract) ---
        fill = await self._execute_sell(token_id, shares, exit_price)
        if not fill.filled:
            return TradeResult(success=False, reason=fill.reason)

        # --- Revenue and DB (shared) ---
        lr = log_return(position["entry_price"], fill.fill_price)
        fee_usdc = exit_fee_usdc(shares, fill.fill_price, fee_rate)
        revenue = shares * fill.fill_price - fee_usdc

        await self.db.close_position(position_id, exit_price=fill.fill_price, log_return=lr)
        bankroll = await self.db.get_bankroll()
        await self.db.set_bankroll(bankroll + revenue)
        return TradeResult(success=True, position_id=position_id, log_return=lr)

    # ------------------------------------------------------------------
    # resolve_position — no CLOB interaction, just DB + bankroll
    # ------------------------------------------------------------------

    async def resolve_position(self, position_id: int, exit_price: float) -> TradeResult:
        """Resolution: contract expired, Polymarket pays $1 or $0."""
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False, reason=f"Position {position_id} not found or already closed")

        lr = log_return(position["entry_price"], exit_price)
        await self.db.close_position(position_id, exit_price=exit_price, log_return=lr)

        new_bankroll = await self._resolve_bankroll(position, exit_price)
        await self.db.set_bankroll(new_bankroll)
        return TradeResult(success=True, position_id=position_id, log_return=lr)

    # ------------------------------------------------------------------
    # Abstract methods — the ONLY things subclasses implement
    # ------------------------------------------------------------------

    @abstractmethod
    async def _execute_buy(self, token_id: str, price: float, size: float) -> FillResult:
        """Execute a buy order.

        Args:
            token_id: CLOB token ID for the order.
            price: Expected fill price (paper uses as-is, live uses as fallback).
            size: USDC amount to spend.
        Returns:
            FillResult with fill_price and fill_size (USDC spent).
        """

    @abstractmethod
    async def _execute_sell(self, token_id: str, shares: float, price: float) -> FillResult:
        """Execute a sell order.

        Args:
            token_id: CLOB token ID for the order.
            shares: Number of shares to sell.
            price: Expected fill price.
        Returns:
            FillResult with fill_price (fill_size unused for sells).
        """

    @abstractmethod
    async def _resolve_bankroll(self, position: dict, exit_price: float) -> float:
        """Compute new bankroll after market resolution.

        Paper: current bankroll + revenue (shares * exit_price - fee).
        Live: fetch real USDC balance from Polymarket.

        Returns:
            New bankroll value to set in DB.
        """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest polybot/tests/test_base_trader.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add polybot/execution/base.py polybot/tests/test_base_trader.py
git commit -m "feat(execution): add BaseTrader ABC with shared trading logic

Introduces FillResult dataclass, moves fee functions from paper_trader
to base, and creates BaseTrader abstract class that owns rejection gates,
fee math, and DB operations. Subclasses only implement order execution."
```

---

### Task 2: Refactor PaperTrader to extend BaseTrader

**Files:**
- Rewrite: `polybot/execution/paper_trader.py`
- Modify: `polybot/tests/test_paper_trader.py` (import path only)
- Modify: `polybot/main.py:11` (import path only)

- [ ] **Step 1: Rewrite paper_trader.py**

Replace `polybot/execution/paper_trader.py` with:

```python
"""Paper trader — simulated fills with instant execution."""
from polybot.execution.base import BaseTrader, FillResult, DEFAULT_FEE_RATE, exit_fee_usdc


class PaperTrader(BaseTrader):
    """Simulated trading. Same logic as LiveTrader, instant fills."""

    async def _execute_buy(self, token_id: str, price: float, size: float) -> FillResult:
        """Instant fill at the given price."""
        return FillResult(filled=True, fill_price=price, fill_size=size)

    async def _execute_sell(self, token_id: str, shares: float, price: float) -> FillResult:
        """Instant fill at the given price."""
        return FillResult(filled=True, fill_price=price)

    async def _resolve_bankroll(self, position: dict, exit_price: float) -> float:
        """Compute revenue from shares. Fee is $0 at resolution extremes."""
        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE
        fee_usdc = exit_fee_usdc(shares, exit_price, fee_rate)
        revenue = shares * exit_price - fee_usdc
        bankroll = await self.db.get_bankroll()
        return bankroll + revenue
```

- [ ] **Step 2: Update import in test_paper_trader.py**

Change line 4 from:
```python
from polybot.execution.paper_trader import PaperTrader, taker_fee, entry_fee_shares, exit_fee_usdc, DEFAULT_FEE_RATE
```
to:
```python
from polybot.execution.base import taker_fee, entry_fee_shares, exit_fee_usdc, DEFAULT_FEE_RATE
from polybot.execution.paper_trader import PaperTrader
```

- [ ] **Step 3: Update import in main.py**

Change line 11 from:
```python
from polybot.execution.paper_trader import taker_fee, entry_fee_shares, exit_fee_usdc, DEFAULT_FEE_RATE
```
to:
```python
from polybot.execution.base import taker_fee, entry_fee_shares, exit_fee_usdc, DEFAULT_FEE_RATE
```

- [ ] **Step 4: Run existing paper trader tests**

Run: `pytest polybot/tests/test_paper_trader.py -v`
Expected: All 11 tests PASS (identical behavior, just different internal structure).

- [ ] **Step 5: Commit**

```bash
git add polybot/execution/paper_trader.py polybot/tests/test_paper_trader.py polybot/main.py
git commit -m "refactor(paper_trader): extend BaseTrader, remove duplicated logic

PaperTrader now implements only _execute_buy (instant fill),
_execute_sell (instant fill), and _resolve_bankroll (compute revenue).
All gates, fee math, and DB operations come from BaseTrader."
```

---

### Task 3: Refactor LiveTrader with FOK market orders and retry

**Files:**
- Rewrite: `polybot/execution/live_trader.py`

- [ ] **Step 1: Write tests for FOK fill and retry logic**

These tests go in test_live_trader.py but we write them first. Add at the bottom of the existing test file (the full rewrite happens in Task 4 — these are the NEW tests):

```python
# --- NEW: retry logic tests ---

@pytest.mark.asyncio
async def test_open_trade_retries_on_transient_failure(trader, monkeypatch):
    """FOK fails once, succeeds on retry."""
    import polybot.execution.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "_RETRY_BASE_DELAY", 0.01)

    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order

    # First call: transient failure. Second call: success.
    trader.client.post_order.side_effect = [
        {"success": False, "errorMsg": "FOK_ORDER_NOT_FILLED_ERROR"},
        {"success": True, "status": "matched", "orderID": "order-retry"},
    ]
    trader.client.get_order.return_value = {
        "associate_trades": [{"price": "0.55", "size": "18.18"}],
    }

    result = await trader.open_trade(**_TRADE_KWARGS)
    assert result.success is True
    assert trader.client.post_order.call_count == 2


@pytest.mark.asyncio
async def test_open_trade_no_retry_on_balance_error(trader):
    """Non-retryable error stops immediately."""
    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order
    trader.client.post_order.return_value = {
        "success": False, "errorMsg": "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    }

    result = await trader.open_trade(**_TRADE_KWARGS)
    assert result.success is False
    assert "BALANCE" in result.reason
    # Only one attempt — no retry
    assert trader.client.post_order.call_count == 1
```

- [ ] **Step 2: Rewrite live_trader.py**

Replace `polybot/execution/live_trader.py` with:

```python
"""Live trader — real Polymarket CLOB orders via py-clob-client SDK.

Extends BaseTrader with FOK market orders. All shared logic (rejection gates,
fee math, DB operations) lives in BaseTrader. This module implements only
_execute_buy, _execute_sell, _resolve_bankroll, plus SDK helpers.
"""
import asyncio
import logging
import os

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

# Retry config
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.5  # seconds, doubles each attempt

# Errors that should NOT be retried (structural, not transient)
_NON_RETRYABLE_ERRORS = frozenset({
    "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    "MARKET_NOT_READY",
    "INVALID_ORDER_EXPIRATION",
})


# ---------------------------------------------------------------------------
# SDK helpers (module-level, not part of the trader class)
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
        funder=funder,
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
    """Real Polymarket CLOB trading. Same interface as PaperTrader."""

    def __init__(self, db: Database, **kwargs):
        super().__init__(
            db=db,
            max_slippage=kwargs.get("max_slippage", 0.02),
            max_bankroll_deployed=kwargs.get("max_bankroll_deployed", 0.80),
            max_concurrent_positions=kwargs.get("max_concurrent_positions", 1),
        )
        self.client = _create_clob_client()
        logger.info("LiveTrader authenticated with Polymarket CLOB")

    async def get_balance(self) -> float:
        """Fetch USDC balance from Polymarket."""
        return _get_balance_usd(self.client)

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    async def _execute_buy(self, token_id: str, price: float, size: float) -> FillResult:
        """FOK market buy for `size` USDC."""
        return await self._submit_fok_order(token_id, BUY, size, price)

    async def _execute_sell(self, token_id: str, shares: float, price: float) -> FillResult:
        """FOK market sell for `shares` shares."""
        return await self._submit_fok_order(token_id, SELL, shares, price)

    async def _resolve_bankroll(self, position: dict, exit_price: float) -> float:
        """Sync bankroll with real Polymarket balance (auto-credited on resolution)."""
        real_balance = await self.get_balance()
        logger.info("Resolution bankroll sync: real balance=%.2f", real_balance)
        return real_balance

    # ------------------------------------------------------------------
    # FOK order submission with retry
    # ------------------------------------------------------------------

    async def _submit_fok_order(self, token_id: str, side: str, amount: float,
                                expected_price: float) -> FillResult:
        """Submit FOK market order with exponential-backoff retry.

        Args:
            token_id: CLOB token ID.
            side: BUY or SELL.
            amount: USDC to spend (BUY) or shares to sell (SELL).
            expected_price: Expected fill price (used as fallback if fill details unavailable).
        """
        last_error = ""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                mo = MarketOrderArgs(token_id=token_id, amount=amount, side=side)
                signed = self.client.create_market_order(mo)
                resp = self.client.post_order(signed, OrderType.FOK)

                if not resp.get("success"):
                    error_msg = resp.get("errorMsg", "unknown error")
                    if any(code in error_msg for code in _NON_RETRYABLE_ERRORS):
                        logger.error("Order rejected (non-retryable): %s", error_msg)
                        return FillResult(filled=False, reason=error_msg)
                    last_error = error_msg
                    logger.warning("FOK attempt %d/%d failed: %s", attempt, _MAX_RETRIES, error_msg)
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    continue

                if resp.get("status") == "matched":
                    order_id = resp.get("orderID", "")
                    fill_price = self._get_fill_price(order_id, expected_price)
                    logger.info("FOK %s filled: order=%s, price=%.4f, amount=%.4f",
                                side, order_id, fill_price, amount)
                    return FillResult(
                        filled=True,
                        fill_price=fill_price,
                        fill_size=amount if side == BUY else 0.0,
                    )

                # Unexpected status (e.g., "delayed" for sports markets)
                last_error = f"Unexpected status: {resp.get('status')}"
                logger.warning("FOK attempt %d/%d: %s", attempt, _MAX_RETRIES, last_error)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))

            except Exception as e:
                last_error = str(e)
                logger.warning("FOK attempt %d/%d exception: %s", attempt, _MAX_RETRIES, e)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))

        return FillResult(filled=False, reason=f"Failed after {_MAX_RETRIES} attempts: {last_error}")

    def _get_fill_price(self, order_id: str, fallback_price: float) -> float:
        """Fetch actual fill price from order details via VWAP. Falls back to expected price."""
        try:
            order = self.client.get_order(order_id)
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
```

- [ ] **Step 3: Run base + paper tests to verify no regression**

Run: `pytest polybot/tests/test_base_trader.py polybot/tests/test_paper_trader.py -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add polybot/execution/live_trader.py
git commit -m "refactor(live_trader): extend BaseTrader, switch to FOK, add retry

BREAKING CHANGES from old LiveTrader:
- Uses FOK market orders via MarketOrderArgs (was GTC limit + polling)
- Fixes OrderArgs.size bug (was passing USDC, SDK expects shares)
- Fixes sell order size bug (was shares*price, should be shares)
- Adds exponential-backoff retry (3 attempts, non-retryable errors bail)
- All shared logic now in BaseTrader (gates, fees, DB ops)"
```

---

### Task 4: Rewrite test_live_trader.py for FOK + retry

**Files:**
- Rewrite: `polybot/tests/test_live_trader.py`

- [ ] **Step 1: Rewrite test_live_trader.py**

The mock patterns change completely — no more GTC polling. Replace entire file:

```python
import sys
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch
from polybot.execution.base import TradeResult, entry_fee_shares
from polybot.db.models import Database


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()


def _mock_clob_client():
    """Return a mock ClobClient that passes init checks."""
    mock = MagicMock()
    mock.create_or_derive_api_creds.return_value = {
        "apiKey": "test-key",
        "secret": "test-secret",
        "passphrase": "test-pass",
    }
    mock.get_balance_allowance.return_value = {"balance": "10000000"}  # 10 USDC
    return mock


# ---------------------------------------------------------------------------
# Init tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_creates_client_and_derives_creds(db):
    sys.modules.pop("polybot.execution.live_trader", None)
    with patch.dict("os.environ", {
        "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
        "POLYMARKET_FUNDER": "0x863DB57D4a54fA306091D53B4Fe19f1611221Be8",
    }):
        with patch("py_clob_client.client.ClobClient", return_value=_mock_clob_client()) as MockClient:
            sys.modules.pop("polybot.execution.live_trader", None)
            from polybot.execution.live_trader import LiveTrader
            trader = LiveTrader(db=db)
            MockClient.assert_called_once()
            trader.client.create_or_derive_api_creds.assert_called_once()
            trader.client.set_api_creds.assert_called_once()


@pytest.mark.asyncio
async def test_init_raises_without_private_key(db):
    with patch.dict("os.environ", {}, clear=True):
        sys.modules.pop("polybot.execution.live_trader", None)
        from polybot.execution.live_trader import LiveTrader
        with pytest.raises(ValueError, match="POLYMARKET_PRIVATE_KEY"):
            LiveTrader(db=db)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def trader(db):
    """Create a LiveTrader with a mocked SDK client."""
    sys.modules.pop("polybot.execution.live_trader", None)
    with patch("py_clob_client.client.ClobClient", return_value=_mock_clob_client()):
        with patch.dict("os.environ", {
            "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
            "POLYMARKET_FUNDER": "0xdeadbeef",
        }):
            from polybot.execution.live_trader import LiveTrader
            t = LiveTrader(db=db)
            yield t
    sys.modules.pop("polybot.execution.live_trader", None)


def _setup_successful_fill(trader, fill_price="0.55", fill_size="18.18"):
    """Wire up mock client to simulate a successful FOK fill.

    fill_size in associate_trades = shares (CLOB convention).
    """
    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order
    trader.client.post_order.return_value = {
        "success": True,
        "status": "matched",
        "orderID": "order-123",
    }
    trader.client.get_order.return_value = {
        "associate_trades": [{"price": fill_price, "size": fill_size}],
    }


_TRADE_KWARGS = dict(
    market_id="mkt-abc",
    question="Will BTC go up?",
    side="Up",
    price=0.55,
    size=10.0,
    signal_score=0.70,
    signal_strength="strong",
    ev_at_entry=0.15,
    exit_target=1.0,
    stop_loss=0.0,
    weight_version="v1",
    indicator_snapshot="{}",
    token_id="tok-up-123",
    fee_rate=0.018,
)


# ---------------------------------------------------------------------------
# open_trade tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_trade_success(trader):
    _setup_successful_fill(trader)
    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is True
    assert result.position_id is not None

    # Bankroll debited by size (10 USDC): 100 - 10 = 90
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_open_trade_rejects_duplicate_market(trader):
    _setup_successful_fill(trader)
    r1 = await trader.open_trade(**_TRADE_KWARGS)
    assert r1.success is True

    r2 = await trader.open_trade(**_TRADE_KWARGS)
    assert r2.success is False
    assert "Duplicate" in r2.reason


@pytest.mark.asyncio
async def test_open_trade_rejects_bankroll_exceeded(trader):
    _setup_successful_fill(trader)
    kwargs = {**_TRADE_KWARGS, "size": 85.0}
    result = await trader.open_trade(**kwargs)

    assert result.success is False
    assert "bankroll" in result.reason.lower()


@pytest.mark.asyncio
async def test_open_trade_handles_fok_failure(trader):
    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order
    # All attempts fail with non-retryable error
    trader.client.post_order.return_value = {
        "success": False, "errorMsg": "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    }

    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is False
    assert "BALANCE" in result.reason
    # Bankroll unchanged
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_open_trade_stores_correct_shares(trader):
    _setup_successful_fill(trader, fill_price="0.55", fill_size="18.18")
    result = await trader.open_trade(**_TRADE_KWARGS)
    assert result.success is True

    positions = await trader.db.get_open_positions()
    assert len(positions) == 1
    pos = positions[0]

    # fill_size in _execute_buy = size USDC (10.0)
    # fill_price from VWAP = 0.55
    # shares_ordered = 10.0 / 0.55
    shares_ordered = 10.0 / 0.55
    fee_in_shares = entry_fee_shares(shares_ordered, 0.55, 0.018)
    expected_shares = shares_ordered - fee_in_shares

    assert pos["shares_held"] == pytest.approx(expected_shares, rel=1e-4)


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_trade_retries_on_transient_failure(trader, monkeypatch):
    """FOK fails once (transient), succeeds on retry."""
    import polybot.execution.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "_RETRY_BASE_DELAY", 0.01)

    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order

    # First call: transient FOK failure. Second call: success.
    trader.client.post_order.side_effect = [
        {"success": False, "errorMsg": "FOK_ORDER_NOT_FILLED_ERROR"},
        {"success": True, "status": "matched", "orderID": "order-retry"},
    ]
    trader.client.get_order.return_value = {
        "associate_trades": [{"price": "0.55", "size": "18.18"}],
    }

    result = await trader.open_trade(**_TRADE_KWARGS)
    assert result.success is True
    assert trader.client.post_order.call_count == 2


@pytest.mark.asyncio
async def test_open_trade_no_retry_on_balance_error(trader):
    """Non-retryable error stops immediately — no retry."""
    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order
    trader.client.post_order.return_value = {
        "success": False, "errorMsg": "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    }

    result = await trader.open_trade(**_TRADE_KWARGS)
    assert result.success is False
    assert "BALANCE" in result.reason
    assert trader.client.post_order.call_count == 1


@pytest.mark.asyncio
async def test_open_trade_retries_on_exception(trader, monkeypatch):
    """Network exception triggers retry."""
    import polybot.execution.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "_RETRY_BASE_DELAY", 0.01)

    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order

    # First call: network error. Second call: success.
    trader.client.post_order.side_effect = [
        ConnectionError("network timeout"),
        {"success": True, "status": "matched", "orderID": "order-retry"},
    ]
    trader.client.get_order.return_value = {
        "associate_trades": [{"price": "0.55", "size": "18.18"}],
    }

    result = await trader.open_trade(**_TRADE_KWARGS)
    assert result.success is True
    assert trader.client.post_order.call_count == 2


# ---------------------------------------------------------------------------
# close_trade tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_trade_success(trader):
    _setup_successful_fill(trader, fill_price="0.55", fill_size="18.18")
    open_result = await trader.open_trade(**_TRADE_KWARGS)
    assert open_result.success is True
    pos_id = open_result.position_id

    # Reconfigure mock for sell side
    _setup_successful_fill(trader, fill_price="0.68", fill_size="18.18")

    result = await trader.close_trade(pos_id, exit_price=0.68, token_id="tok-up-123")

    assert result.success is True
    assert result.log_return is not None
    bankroll = await trader.db.get_bankroll()
    assert bankroll > 90.0


@pytest.mark.asyncio
async def test_close_trade_not_found(trader):
    result = await trader.close_trade(position_id=999, exit_price=0.60)

    assert result.success is False
    assert "not found" in result.reason.lower()


# ---------------------------------------------------------------------------
# resolve_position tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_position_winner(trader):
    _setup_successful_fill(trader, fill_price="0.50", fill_size="100.0")
    kwargs = {**_TRADE_KWARGS, "price": 0.50, "size": 50.0}
    open_result = await trader.open_trade(**kwargs)
    assert open_result.success is True
    pos_id = open_result.position_id

    positions = await trader.db.get_open_positions()
    shares_held = positions[0]["shares_held"]

    # Mock balance to reflect winnings: remaining bankroll (50) + shares * $1
    winning_balance = 50.0 + shares_held
    trader.client.get_balance_allowance.return_value = {
        "balance": str(int(winning_balance * 1e6))
    }

    result = await trader.resolve_position(pos_id, exit_price=1.0)

    assert result.success is True
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(winning_balance, rel=1e-4)


@pytest.mark.asyncio
async def test_resolve_position_loser(trader):
    _setup_successful_fill(trader, fill_price="0.50", fill_size="100.0")
    kwargs = {**_TRADE_KWARGS, "price": 0.50, "size": 50.0, "market_id": "mkt-loser"}
    open_result = await trader.open_trade(**kwargs)
    assert open_result.success is True
    pos_id = open_result.position_id

    # Mock balance: just remaining bankroll (50), shares worthless
    trader.client.get_balance_allowance.return_value = {
        "balance": str(int(50.0 * 1e6))
    }

    result = await trader.resolve_position(pos_id, exit_price=0.0)

    assert result.success is True
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(50.0, rel=1e-4)
```

- [ ] **Step 2: Run full test suite**

Run: `pytest polybot/tests/test_base_trader.py polybot/tests/test_paper_trader.py polybot/tests/test_live_trader.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Run the complete project test suite**

Run: `pytest polybot/tests/ -v`
Expected: All 249+ tests PASS (no regressions in other modules).

- [ ] **Step 4: Commit**

```bash
git add polybot/tests/test_live_trader.py polybot/tests/test_base_trader.py
git commit -m "test(execution): rewrite live trader tests for FOK + retry

- Mocks now use create_market_order + FOK response (was create_order + GTC poll)
- Adds retry tests: transient failure, balance error, network exception
- Adds base trader tests for shared rejection gates and resolution
- All tests verify identical behavior between paper and live base logic"
```

---

### Task 5: Update CLAUDE.md

**Files:**
- Modify: `polybot/CLAUDE.md`

- [ ] **Step 1: Update the execution section in CLAUDE.md**

In the Project Structure section, update the execution entries:

From:
```
  execution/
    base.py                  # TradeResult dataclass
    paper_trader.py          # Simulated trades (paper mode)
    live_trader.py           # Real Polymarket CLOB orders via py-clob-client SDK
    circuit_breaker.py       # Streak-based Kelly reduction
```

To:
```
  execution/
    base.py                  # BaseTrader ABC, TradeResult, FillResult, fee functions
    paper_trader.py          # PaperTrader(BaseTrader) — instant simulated fills
    live_trader.py           # LiveTrader(BaseTrader) — FOK market orders via py-clob-client SDK
    circuit_breaker.py       # Streak-based Kelly reduction
```

In the "Paper -> Live: What Changes, What Doesn't" section, update:

From:
```
**CHANGES (inside LiveTrader only):**
- `open_trade()`: mock fill → EIP-712 sign → POST /orders → poll fill → DB with actuals
- `close_trade()`: mock sell → EIP-712 sign → POST /orders (SELL) → poll fill → DB with actuals
```

To:
```
**CHANGES (inside LiveTrader only):**
- `_execute_buy()`: instant fill → FOK market order via create_market_order + post_order(FOK) with retry
- `_execute_sell()`: instant fill → FOK market order via create_market_order + post_order(FOK) with retry
```

In the "What NOT to Change" section, verify the FOK rule is present:
```
- Don't use limit orders in LiveTrader — FOK market orders for 5-min contract speed.
```

- [ ] **Step 2: Commit**

```bash
git add polybot/CLAUDE.md
git commit -m "docs: update CLAUDE.md for BaseTrader architecture and FOK orders"
```

---

## Summary of bugs fixed

| Bug | Location | Fix |
|-----|----------|-----|
| BUY size wrong | `live_trader.py:128` | Was `OrderArgs(size=size)` (USDC). Now `MarketOrderArgs(amount=size)` (USDC for BUY is correct) |
| SELL size wrong | `live_trader.py:209` | Was `OrderArgs(size=shares*exit_price)`. Now `MarketOrderArgs(amount=shares)` (shares for SELL) |
| GTC instead of FOK | `live_trader.py:131` | Was `OrderType.GTC` + polling loop. Now `OrderType.FOK` — fills immediately or fails |
| No retry logic | `live_trader.py` | Added exponential backoff (3 attempts), non-retryable error short-circuit |
| No abstract base | `base.py` | BaseTrader ABC with shared gates, fees, DB ops |
| Duplicated logic | Both traders | Eliminated — both extend BaseTrader, implement only 3 abstract methods |
| No slippage on fills | `live_trader.py` | FOK fills at SDK-computed market price or fails — natural slippage protection |

## Invariants preserved

- Entry fee collected in SHARES (fewer shares received, not extra USDC)
- Exit fee collected in USDC (subtracted from proceeds)
- Bankroll debited by `size` USDC on entry, credited by `revenue` on exit
- All 3 rejection gates run BEFORE any exchange interaction
- TradeResult is the contract boundary — same shape regardless of mode
- Separate DB files for paper and live (no cross-contamination)
