import pytest
import pytest_asyncio
from polybot.execution.base import taker_fee, entry_fee_shares, exit_fee_usdc
from polybot.execution.paper_trader import PaperTrader
from polybot.db.models import Database


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def trader(db):
    # Tests assert deterministic fill behavior — disable the realism randomness
    # (latency + network fail sim) that only matters in live paper runs.
    return PaperTrader(
        db=db,
        max_slippage=0.02,
        max_bankroll_deployed=0.80,
        max_concurrent_positions=5,
        paper_latency_mean_s=0.0,
        paper_latency_jitter_s=0.0,
        paper_network_fail_rate=0.0,
    )


@pytest.mark.asyncio
async def test_open_trade_returns_success(trader):
    result = await trader.open_trade(
        market_id="market_123", question="Will X happen?", side="YES",
        price=0.55, size=10.0, signal_score=0.72,
    )
    assert result.success is True
    assert result.position_id is not None


@pytest.mark.asyncio
async def test_open_trade_reduces_bankroll(trader, db):
    await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72,
    )
    bankroll = await db.get_bankroll()
    # Entry fee collected in shares (not USDC) — bankroll only decreases by size
    expected = 100.0 - 10.0
    assert bankroll == pytest.approx(expected, abs=0.01)


@pytest.mark.asyncio
async def test_rejects_duplicate_market(trader):
    await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72,
    )
    result = await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72,
    )
    assert result.success is False
    assert "duplicate" in result.reason.lower()


@pytest.mark.asyncio
async def test_rejects_when_max_positions_reached(trader, db):
    for i in range(5):
        await trader.open_trade(
            market_id=f"market_{i}", question="Q?", side="YES", price=0.55,
            size=5.0, signal_score=0.72,
        )
    result = await trader.open_trade(
        market_id="market_6", question="Q?", side="YES", price=0.55,
        size=5.0, signal_score=0.72,
    )
    assert result.success is False
    assert "max positions" in result.reason.lower()


@pytest.mark.asyncio
async def test_rejects_when_bankroll_exceeded(trader, db):
    result = await trader.open_trade(
        market_id="market_big", question="Q?", side="YES", price=0.55,
        size=85.0, signal_score=0.72,
    )
    assert result.success is False
    assert "bankroll" in result.reason.lower()


@pytest.mark.asyncio
async def test_close_trade_updates_bankroll(trader, db):
    result = await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72,
    )
    close_result = await trader.close_trade(position_id=result.position_id, exit_price=0.68)
    assert close_result.success is True
    bankroll = await db.get_bankroll()
    assert bankroll > 100.0


# --- Fee model tests ---

def test_taker_fee_formula():
    # fee = feeRate × shares × p × (1-p)
    fee = taker_fee(100, 0.50, 0.072)
    assert fee == pytest.approx(0.072 * 100 * 0.50 * 0.50, abs=0.001)

def test_taker_fee_zero_at_extremes():
    assert taker_fee(100, 1.0, 0.072) == 0.0
    assert taker_fee(100, 0.0, 0.072) == 0.0

def test_entry_fee_in_shares():
    # 100 shares at 0.50, crypto rate 0.072
    fee_shares = entry_fee_shares(100, 0.50, 0.072)
    fee_dollars = taker_fee(100, 0.50, 0.072)
    # fee_shares = fee_dollars / price
    assert fee_shares == pytest.approx(fee_dollars / 0.50, abs=0.001)

def test_exit_fee_in_usdc():
    fee = exit_fee_usdc(100, 0.60, 0.072)
    assert fee == pytest.approx(0.072 * 100 * 0.60 * 0.40, abs=0.001)


@pytest.mark.asyncio
async def test_shares_held_stored_correctly(trader, db):
    await trader.open_trade(
        market_id="m_shares", question="Q?", side="YES", price=0.50,
        size=50.0, signal_score=0.72, fee_rate=0.072,
    )
    positions = await db.get_open_positions()
    pos = positions[0]
    shares_ordered = 50.0 / 0.50  # 100 shares
    fee_in_shares = entry_fee_shares(shares_ordered, 0.50, 0.072)
    expected_shares = shares_ordered - fee_in_shares
    assert pos["shares_held"] == pytest.approx(expected_shares, abs=0.01)
    assert pos["fee_rate"] == 0.072


@pytest.mark.asyncio
async def test_pnl_realistic_with_fee_in_shares(trader, db):
    """Win at resolution: shares-based entry fee means fewer shares → less payout."""
    result = await trader.open_trade(
        market_id="m_pnl", question="Q?", side="YES", price=0.50,
        size=50.0, signal_score=0.72, fee_rate=0.072,
    )
    # Bankroll after open = 100 - 50 = 50 (fee is in shares, not USDC)
    assert await db.get_bankroll() == pytest.approx(50.0, abs=0.01)

    # Win at resolution ($1.00 — exit fee is $0 at extremes)
    await trader.resolve_position(result.position_id, 1.0)
    bankroll = await db.get_bankroll()
    # Revenue = shares_held × 1.0 - exit_fee(~$0)
    shares_ordered = 50.0 / 0.50
    fee_sh = entry_fee_shares(shares_ordered, 0.50, 0.072)
    shares_held = shares_ordered - fee_sh
    # Bankroll = 50 + shares_held × 1.0
    assert bankroll == pytest.approx(50.0 + shares_held, abs=0.01)
    # Should be less than 150 because of fee deduction
    assert bankroll < 150.0


@pytest.mark.asyncio
async def test_custom_fee_rate_passed_through(trader, db):
    """Sports markets use 0.03 fee rate."""
    await trader.open_trade(
        market_id="m_sports", question="Q?", side="YES", price=0.50,
        size=10.0, signal_score=0.72, fee_rate=0.03,
    )
    positions = await db.get_open_positions()
    pos = positions[0]
    assert pos["fee_rate"] == 0.03
    # Shares held should be more than with crypto rate
    shares_ordered = 10.0 / 0.50
    fee_sh = entry_fee_shares(shares_ordered, 0.50, 0.03)
    assert pos["shares_held"] == pytest.approx(shares_ordered - fee_sh, abs=0.01)
