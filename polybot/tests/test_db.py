import pytest
import pytest_asyncio
from polybot.db.models import Database

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()

@pytest.mark.asyncio
async def test_initialize_creates_tables(db):
    tables = await db.get_tables()
    assert "positions" in tables
    assert "trade_history" in tables

@pytest.mark.asyncio
async def test_open_position(db):
    pos_id = await db.open_position(
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert pos_id == 1

@pytest.mark.asyncio
async def test_get_open_positions(db):
    await db.open_position(
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    positions = await db.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["market_id"] == "market_123"
    assert positions[0]["status"] == "open"

@pytest.mark.asyncio
async def test_close_position(db):
    pos_id = await db.open_position(
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    await db.close_position(pos_id, exit_price=0.68, log_return=0.212)
    positions = await db.get_open_positions()
    assert len(positions) == 0
    history = await db.get_trade_history(limit=10)
    assert len(history) == 1
    assert history[0]["exit_price"] == 0.68

@pytest.mark.asyncio
async def test_has_position_for_market(db):
    assert await db.has_position_for_market("market_123") is False
    await db.open_position(
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert await db.has_position_for_market("market_123") is True

@pytest.mark.asyncio
async def test_get_open_position_count(db):
    assert await db.get_open_position_count() == 0
    await db.open_position(
        market_id="market_123",
        question="Q?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert await db.get_open_position_count() == 1

@pytest.mark.asyncio
async def test_update_bankroll(db):
    await db.set_bankroll(100.0)
    assert await db.get_bankroll() == 100.0
    await db.set_bankroll(95.50)
    assert await db.get_bankroll() == 95.50
