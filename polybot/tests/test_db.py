import pytest
import pytest_asyncio
from polybot.db.models import Database


_POS_KWARGS = dict(
    market_id="market_123",
    question="Will X happen?",
    side="YES",
    entry_price=0.55,
    size=10.0,
    signal_score=0.72,
)


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_initialize_creates_tables(db):
    cursor = await db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in await cursor.fetchall()}
    assert "positions" in tables
    assert "trade_history" in tables


@pytest.mark.asyncio
async def test_open_position_and_debit_bankroll(db):
    await db.set_bankroll(100.0)
    pos_id = await db.open_position_and_debit_bankroll(new_bankroll=90.0, **_POS_KWARGS)
    assert pos_id == 1
    assert await db.get_bankroll() == 90.0


@pytest.mark.asyncio
async def test_get_open_positions(db):
    await db.set_bankroll(100.0)
    await db.open_position_and_debit_bankroll(new_bankroll=90.0, **_POS_KWARGS)
    positions = await db.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["market_id"] == "market_123"
    assert positions[0]["status"] == "open"


@pytest.mark.asyncio
async def test_close_position(db):
    await db.set_bankroll(100.0)
    pos_id = await db.open_position_and_debit_bankroll(new_bankroll=90.0, **_POS_KWARGS)
    await db.close_position(pos_id, exit_price=0.68)
    positions = await db.get_open_positions()
    assert len(positions) == 0
    history = await db.get_trade_history(limit=10)
    assert len(history) == 1
    assert history[0]["exit_price"] == 0.68


@pytest.mark.asyncio
async def test_has_position_for_market(db):
    assert await db.has_position_for_market("market_123") is False
    await db.set_bankroll(100.0)
    await db.open_position_and_debit_bankroll(new_bankroll=90.0, **_POS_KWARGS)
    assert await db.has_position_for_market("market_123") is True


@pytest.mark.asyncio
async def test_get_open_position_count(db):
    assert await db.get_open_position_count() == 0
    await db.set_bankroll(100.0)
    await db.open_position_and_debit_bankroll(new_bankroll=90.0, **_POS_KWARGS)
    assert await db.get_open_position_count() == 1


@pytest.mark.asyncio
async def test_update_bankroll(db):
    await db.set_bankroll(100.0)
    assert await db.get_bankroll() == 100.0
    await db.set_bankroll(95.50)
    assert await db.get_bankroll() == 95.50


@pytest.mark.asyncio
async def test_close_position_writes_position_id_link(db):
    """trade_history.position_id must carry the true link to positions — the
    implicit id pairing breaks whenever the two AUTOINCREMENT sequences drift."""
    await db.set_bankroll(100.0)
    pos_id = await db.open_position_and_debit_bankroll(new_bankroll=90.0, **_POS_KWARGS)
    # drift the trade_history sequence ahead of positions (a decoy row with a
    # high explicit id bumps the AUTOINCREMENT high-water mark)
    await db.conn.execute(
        "INSERT INTO trade_history (id, side, entry_price, exit_price, size, "
        "exit_timestamp) VALUES (?, 'Up', 0.5, 0.5, 1.0, '2026-01-01T00:00:00')",
        (pos_id + 100,))
    await db.close_position(pos_id, exit_price=0.7)
    row = await (await db.conn.execute(
        "SELECT id, position_id FROM trade_history ORDER BY id DESC LIMIT 1")).fetchone()
    assert row[1] == pos_id
    assert row[0] != pos_id  # the drift is real, and the link survives it
