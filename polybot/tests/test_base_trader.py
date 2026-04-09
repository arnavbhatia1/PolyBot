"""Tests for BaseTrader ABC via a StubTrader concrete subclass.

Covers: rejection gates, open/close/resolve flows, fee math, bankroll updates,
FillResult propagation, and edge cases (unfilled orders, missing positions).
"""

import pytest
import pytest_asyncio

from polybot.db.models import Database
from polybot.execution.base import (
    DEFAULT_FEE_RATE,
    BaseTrader,
    FillResult,
    TradeResult,
    entry_fee_shares,
    exit_fee_usdc,
    taker_fee,
)


# ---------------------------------------------------------------------------
# StubTrader — minimal concrete implementation for testing shared behavior
# ---------------------------------------------------------------------------

class StubTrader(BaseTrader):
    """Concrete BaseTrader that fills at the requested price (paper-like).

    Control fill behavior via `buy_fill` and `sell_fill` overrides.
    """

    def __init__(self, db, **kwargs):
        super().__init__(db, **kwargs)
        # Override these to simulate fill failures, slippage, etc.
        self.buy_fill: FillResult | None = None
        self.sell_fill: FillResult | None = None

    async def _execute_buy(self, token_id: str, price: float, size: float) -> FillResult:
        if self.buy_fill is not None:
            return self.buy_fill
        # Default: instant fill at requested price and size
        return FillResult(filled=True, fill_price=price, fill_size=size)

    async def _execute_sell(self, token_id: str, shares: float, price: float) -> FillResult:
        if self.sell_fill is not None:
            return self.sell_fill
        return FillResult(filled=True, fill_price=price, fill_size=shares * price)

    async def _resolve_bankroll(self, position: dict, exit_price: float) -> float:
        """Paper-style resolution: revenue = shares * exit_price - exit_fee."""
        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE
        fee_usdc = exit_fee_usdc(shares, exit_price, fee_rate)
        revenue = shares * exit_price - fee_usdc
        bankroll = await self.db.get_bankroll()
        return bankroll + revenue


# ---------------------------------------------------------------------------
# Shared trade params helper
# ---------------------------------------------------------------------------

def _trade_params(**overrides):
    """Returns a dict of default open_trade kwargs. Override any key."""
    defaults = dict(
        market_id="market_1",
        question="Will BTC go up?",
        side="YES",
        price=0.50,
        size=10.0,
        signal_score=0.72,
        signal_strength="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.40,
        weight_version="v001",
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def trader(db):
    return StubTrader(db=db, max_concurrent_positions=5)


@pytest_asyncio.fixture
async def single_pos_trader(db):
    """Trader with max_concurrent_positions=1 for limit tests."""
    return StubTrader(db=db, max_concurrent_positions=1)


# ---------------------------------------------------------------------------
# FillResult dataclass
# ---------------------------------------------------------------------------

class TestFillResult:
    def test_defaults(self):
        fr = FillResult(filled=False)
        assert fr.filled is False
        assert fr.fill_price == 0.0
        assert fr.fill_size == 0.0
        assert fr.reason == ""

    def test_filled(self):
        fr = FillResult(filled=True, fill_price=0.55, fill_size=10.0)
        assert fr.filled is True
        assert fr.fill_price == 0.55
        assert fr.fill_size == 10.0

    def test_with_reason(self):
        fr = FillResult(filled=False, reason="Timeout")
        assert fr.reason == "Timeout"


# ---------------------------------------------------------------------------
# Fee math (module-level functions)
# ---------------------------------------------------------------------------

class TestFeeMath:
    def test_taker_fee_formula(self):
        fee = taker_fee(100, 0.50, 0.072)
        assert fee == pytest.approx(0.072 * 100 * 0.50 * 0.50, abs=0.001)

    def test_taker_fee_zero_at_extremes(self):
        assert taker_fee(100, 1.0) == 0.0
        assert taker_fee(100, 0.0) == 0.0

    def test_taker_fee_default_rate(self):
        fee = taker_fee(100, 0.50)
        assert fee == pytest.approx(DEFAULT_FEE_RATE * 100 * 0.50 * 0.50, abs=0.001)

    def test_entry_fee_in_shares(self):
        fee_shares = entry_fee_shares(100, 0.50, 0.072)
        fee_dollars = taker_fee(100, 0.50, 0.072)
        assert fee_shares == pytest.approx(fee_dollars / 0.50, abs=0.001)

    def test_entry_fee_zero_price(self):
        assert entry_fee_shares(100, 0.0) == 0.0

    def test_exit_fee_in_usdc(self):
        fee = exit_fee_usdc(100, 0.60, 0.072)
        assert fee == pytest.approx(0.072 * 100 * 0.60 * 0.40, abs=0.001)


# ---------------------------------------------------------------------------
# Rejection gates
# ---------------------------------------------------------------------------

class TestRejectionGates:
    @pytest.mark.asyncio
    async def test_rejects_duplicate_market(self, trader):
        await trader.open_trade(**_trade_params())
        result = await trader.open_trade(**_trade_params())
        assert result.success is False
        assert "duplicate" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_rejects_max_positions(self, single_pos_trader):
        await single_pos_trader.open_trade(**_trade_params())
        result = await single_pos_trader.open_trade(**_trade_params(market_id="market_2"))
        assert result.success is False
        assert "max positions" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_rejects_bankroll_limit(self, trader):
        # Bankroll=100, max_deployed=80%. Requesting 85 exceeds limit.
        result = await trader.open_trade(**_trade_params(size=85.0))
        assert result.success is False
        assert "bankroll" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_bankroll_limit_accounts_for_deployed(self, trader):
        # First trade uses 40 USDC. Second trade of 45 would exceed 80% of 100.
        await trader.open_trade(**_trade_params(size=40.0))
        result = await trader.open_trade(**_trade_params(market_id="m2", size=45.0))
        assert result.success is False
        assert "bankroll" in result.reason.lower()


# ---------------------------------------------------------------------------
# open_trade
# ---------------------------------------------------------------------------

class TestOpenTrade:
    @pytest.mark.asyncio
    async def test_success(self, trader):
        result = await trader.open_trade(**_trade_params())
        assert result.success is True
        assert result.position_id is not None

    @pytest.mark.asyncio
    async def test_reduces_bankroll_by_fill_size(self, trader, db):
        await trader.open_trade(**_trade_params(size=10.0))
        bankroll = await db.get_bankroll()
        assert bankroll == pytest.approx(90.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_stores_shares_held_after_fee(self, trader, db):
        await trader.open_trade(**_trade_params(price=0.50, size=50.0, fee_rate=0.072))
        positions = await db.get_open_positions()
        pos = positions[0]
        shares_ordered = 50.0 / 0.50
        fee_sh = entry_fee_shares(shares_ordered, 0.50, 0.072)
        expected_shares = shares_ordered - fee_sh
        assert pos["shares_held"] == pytest.approx(expected_shares, abs=0.01)
        assert pos["fee_rate"] == 0.072

    @pytest.mark.asyncio
    async def test_uses_fill_price_not_requested_price(self, trader, db):
        """If _execute_buy returns a different price (slippage), DB stores fill price."""
        trader.buy_fill = FillResult(filled=True, fill_price=0.52, fill_size=10.0)
        await trader.open_trade(**_trade_params(price=0.50, size=10.0))
        positions = await db.get_open_positions()
        assert positions[0]["entry_price"] == pytest.approx(0.52, abs=0.001)

    @pytest.mark.asyncio
    async def test_buy_not_filled_returns_failure(self, trader):
        trader.buy_fill = FillResult(filled=False, reason="Order timed out")
        result = await trader.open_trade(**_trade_params())
        assert result.success is False
        assert "timed out" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_buy_not_filled_no_bankroll_change(self, trader, db):
        trader.buy_fill = FillResult(filled=False, reason="No fill")
        await trader.open_trade(**_trade_params())
        bankroll = await db.get_bankroll()
        assert bankroll == pytest.approx(100.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_default_fee_rate(self, trader, db):
        await trader.open_trade(**_trade_params())
        positions = await db.get_open_positions()
        assert positions[0]["fee_rate"] == DEFAULT_FEE_RATE

    @pytest.mark.asyncio
    async def test_custom_fee_rate(self, trader, db):
        await trader.open_trade(**_trade_params(fee_rate=0.03))
        positions = await db.get_open_positions()
        assert positions[0]["fee_rate"] == 0.03

    @pytest.mark.asyncio
    async def test_indicator_snapshot_persisted(self, trader, db):
        await trader.open_trade(**_trade_params(indicator_snapshot='{"rsi": 55}'))
        positions = await db.get_open_positions()
        assert positions[0]["indicator_snapshot"] == '{"rsi": 55}'


# ---------------------------------------------------------------------------
# close_trade
# ---------------------------------------------------------------------------

class TestCloseTrade:
    @pytest.mark.asyncio
    async def test_success(self, trader):
        open_res = await trader.open_trade(**_trade_params())
        result = await trader.close_trade(open_res.position_id, exit_price=0.68)
        assert result.success is True
        assert result.position_id == open_res.position_id
        assert result.log_return is not None

    @pytest.mark.asyncio
    async def test_bankroll_increases_on_win(self, trader, db):
        open_res = await trader.open_trade(**_trade_params(price=0.50, size=10.0))
        await trader.close_trade(open_res.position_id, exit_price=0.70)
        bankroll = await db.get_bankroll()
        # Bankroll should be > initial 100 (90 after open + revenue from close)
        assert bankroll > 90.0

    @pytest.mark.asyncio
    async def test_position_closed_in_db(self, trader, db):
        open_res = await trader.open_trade(**_trade_params())
        await trader.close_trade(open_res.position_id, exit_price=0.68)
        open_positions = await db.get_open_positions()
        assert len(open_positions) == 0

    @pytest.mark.asyncio
    async def test_not_found_returns_failure(self, trader):
        result = await trader.close_trade(position_id=9999, exit_price=0.50)
        assert result.success is False
        assert "not found" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_sell_not_filled_returns_failure(self, trader):
        open_res = await trader.open_trade(**_trade_params())
        trader.sell_fill = FillResult(filled=False, reason="Rejected by exchange")
        result = await trader.close_trade(open_res.position_id, exit_price=0.68)
        assert result.success is False
        assert "rejected" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_sell_not_filled_position_stays_open(self, trader, db):
        open_res = await trader.open_trade(**_trade_params())
        trader.sell_fill = FillResult(filled=False, reason="Nope")
        await trader.close_trade(open_res.position_id, exit_price=0.68)
        positions = await db.get_open_positions()
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_uses_fill_price_for_revenue(self, trader, db):
        """If _execute_sell fills at a different price, revenue uses fill price."""
        open_res = await trader.open_trade(**_trade_params(price=0.50, size=10.0))
        bankroll_after_open = await db.get_bankroll()
        # Sell fills at 0.60 instead of requested 0.68
        trader.sell_fill = FillResult(filled=True, fill_price=0.60, fill_size=12.0)
        await trader.close_trade(open_res.position_id, exit_price=0.68)
        bankroll = await db.get_bankroll()
        # Revenue should be based on fill_price=0.60
        positions_data = await db.get_trade_history()
        assert positions_data[0]["exit_price"] == pytest.approx(0.60, abs=0.001)

    @pytest.mark.asyncio
    async def test_exit_fee_deducted(self, trader, db):
        """Exit fee in USDC is subtracted from revenue."""
        open_res = await trader.open_trade(**_trade_params(price=0.50, size=50.0, fee_rate=0.072))
        bankroll_after_open = await db.get_bankroll()

        await trader.close_trade(open_res.position_id, exit_price=0.60)
        bankroll_after_close = await db.get_bankroll()

        # Manually compute expected revenue
        positions = await db.get_trade_history()
        pos = positions[0]
        shares_ordered = 50.0 / 0.50
        fee_sh = entry_fee_shares(shares_ordered, 0.50, 0.072)
        shares = shares_ordered - fee_sh
        fee_usdc = exit_fee_usdc(shares, 0.60, 0.072)
        revenue = shares * 0.60 - fee_usdc
        assert bankroll_after_close == pytest.approx(bankroll_after_open + revenue, abs=0.01)

    @pytest.mark.asyncio
    async def test_fallback_shares_from_size(self, trader, db):
        """When shares_held is not set, falls back to size/entry_price."""
        # Open a trade normally, then manually null out shares_held in DB
        open_res = await trader.open_trade(**_trade_params(price=0.50, size=10.0))
        await db.conn.execute(
            "UPDATE positions SET shares_held = NULL WHERE id = ?",
            (open_res.position_id,),
        )
        await db.conn.commit()

        result = await trader.close_trade(open_res.position_id, exit_price=0.60)
        assert result.success is True


# ---------------------------------------------------------------------------
# resolve_position
# ---------------------------------------------------------------------------

class TestResolvePosition:
    @pytest.mark.asyncio
    async def test_win_at_resolution(self, trader, db):
        open_res = await trader.open_trade(**_trade_params(price=0.50, size=50.0, fee_rate=0.072))
        bankroll_after_open = await db.get_bankroll()
        assert bankroll_after_open == pytest.approx(50.0, abs=0.01)

        result = await trader.resolve_position(open_res.position_id, exit_price=1.0)
        assert result.success is True

        bankroll = await db.get_bankroll()
        # Revenue = shares * 1.0 - fee (fee is ~0 at price=1.0)
        shares_ordered = 50.0 / 0.50
        fee_sh = entry_fee_shares(shares_ordered, 0.50, 0.072)
        shares = shares_ordered - fee_sh
        assert bankroll == pytest.approx(50.0 + shares, abs=0.01)
        assert bankroll < 150.0  # Less than 150 because of entry fee in shares

    @pytest.mark.asyncio
    async def test_loss_at_resolution(self, trader, db):
        open_res = await trader.open_trade(**_trade_params(price=0.50, size=10.0))
        result = await trader.resolve_position(open_res.position_id, exit_price=0.0)
        assert result.success is True
        bankroll = await db.get_bankroll()
        # Lost everything: revenue = shares * 0 - 0 = 0. Bankroll = 90 + 0 = 90.
        assert bankroll == pytest.approx(90.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_position_closed_in_db(self, trader, db):
        open_res = await trader.open_trade(**_trade_params())
        await trader.resolve_position(open_res.position_id, exit_price=1.0)
        open_positions = await db.get_open_positions()
        assert len(open_positions) == 0

    @pytest.mark.asyncio
    async def test_log_return_correct(self, trader):
        open_res = await trader.open_trade(**_trade_params(price=0.50, size=10.0))
        result = await trader.resolve_position(open_res.position_id, exit_price=1.0)
        import math
        expected_lr = math.log(1.0 / 0.50)
        assert result.log_return == pytest.approx(expected_lr, abs=0.001)

    @pytest.mark.asyncio
    async def test_not_found_returns_failure(self, trader):
        result = await trader.resolve_position(position_id=9999, exit_price=1.0)
        assert result.success is False
        assert "not found" in result.reason.lower()


# ---------------------------------------------------------------------------
# _get_deployed_capital
# ---------------------------------------------------------------------------

class TestDeployedCapital:
    @pytest.mark.asyncio
    async def test_no_positions(self, trader):
        deployed = await trader._get_deployed_capital()
        assert deployed == 0.0

    @pytest.mark.asyncio
    async def test_sums_open_position_sizes(self, trader):
        await trader.open_trade(**_trade_params(market_id="m1", size=10.0))
        await trader.open_trade(**_trade_params(market_id="m2", size=15.0))
        deployed = await trader._get_deployed_capital()
        assert deployed == pytest.approx(25.0, abs=0.01)


# ---------------------------------------------------------------------------
# BaseTrader is abstract — cannot be instantiated directly
# ---------------------------------------------------------------------------

class TestAbstractEnforcement:
    def test_cannot_instantiate_base(self):
        with pytest.raises(TypeError):
            BaseTrader(db=None)
