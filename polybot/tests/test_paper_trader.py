import pytest
import pytest_asyncio
from polybot.execution.base import TradeResult
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
    return PaperTrader(db=db, max_slippage=0.02, max_bankroll_deployed=0.80, max_concurrent_positions=5)


@pytest.mark.asyncio
async def test_open_trade_returns_success(trader):
    result = await trader.open_trade(
        market_id="market_123", question="Will X happen?", side="YES",
        price=0.55, size=10.0, claude_probability=0.72, claude_confidence="high",
        ev_at_entry=0.17, exit_target=0.68, stop_loss=0.47, prompt_version="v001",
    )
    assert result.success is True
    assert result.position_id is not None


@pytest.mark.asyncio
async def test_open_trade_reduces_bankroll(trader, db):
    await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, claude_probability=0.72, claude_confidence="high", ev_at_entry=0.17,
        exit_target=0.68, stop_loss=0.47, prompt_version="v001",
    )
    bankroll = await db.get_bankroll()
    assert bankroll == pytest.approx(90.0, abs=0.01)


@pytest.mark.asyncio
async def test_rejects_duplicate_market(trader):
    await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, claude_probability=0.72, claude_confidence="high", ev_at_entry=0.17,
        exit_target=0.68, stop_loss=0.47, prompt_version="v001",
    )
    result = await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, claude_probability=0.72, claude_confidence="high", ev_at_entry=0.17,
        exit_target=0.68, stop_loss=0.47, prompt_version="v001",
    )
    assert result.success is False
    assert "duplicate" in result.reason.lower()


@pytest.mark.asyncio
async def test_rejects_when_max_positions_reached(trader, db):
    for i in range(5):
        await trader.open_trade(
            market_id=f"market_{i}", question="Q?", side="YES", price=0.55,
            size=5.0, claude_probability=0.72, claude_confidence="high", ev_at_entry=0.17,
            exit_target=0.68, stop_loss=0.47, prompt_version="v001",
        )
    result = await trader.open_trade(
        market_id="market_6", question="Q?", side="YES", price=0.55,
        size=5.0, claude_probability=0.72, claude_confidence="high", ev_at_entry=0.17,
        exit_target=0.68, stop_loss=0.47, prompt_version="v001",
    )
    assert result.success is False
    assert "max positions" in result.reason.lower()


@pytest.mark.asyncio
async def test_rejects_when_bankroll_exceeded(trader, db):
    result = await trader.open_trade(
        market_id="market_big", question="Q?", side="YES", price=0.55,
        size=85.0, claude_probability=0.72, claude_confidence="high", ev_at_entry=0.17,
        exit_target=0.68, stop_loss=0.47, prompt_version="v001",
    )
    assert result.success is False
    assert "bankroll" in result.reason.lower()


@pytest.mark.asyncio
async def test_close_trade_updates_bankroll(trader, db):
    result = await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, claude_probability=0.72, claude_confidence="high", ev_at_entry=0.17,
        exit_target=0.68, stop_loss=0.47, prompt_version="v001",
    )
    close_result = await trader.close_trade(position_id=result.position_id, exit_price=0.68)
    assert close_result.success is True
    bankroll = await db.get_bankroll()
    assert bankroll > 100.0
