import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from polybot.db.models import Database
from polybot.execution.live_trader import LiveTrader, ENTRY_INTENT, EXIT_INTENT


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(1000.0)
    yield database
    await database.close()


@pytest.fixture
def mock_us_client():
    client = AsyncMock()
    client.get_balance = AsyncMock(return_value=1000.0)
    client.get_positions = AsyncMock(return_value=[])
    client.place_order = AsyncMock(return_value={
        "state": "ORDER_STATE_FILLED",
        "orderId": "test-order-123",
        "averagePrice": "0.55",
    })
    client.close_position = AsyncMock(return_value={
        "state": "ORDER_STATE_FILLED",
        "orderId": "test-sell-456",
        "averagePrice": "0.80",
    })
    return client


@pytest.fixture
def trader(db, mock_us_client):
    return LiveTrader(db=db, us_client=mock_us_client)


def test_intent_mapping():
    assert ENTRY_INTENT["Up"] == "ORDER_INTENT_BUY_LONG"
    assert ENTRY_INTENT["Down"] == "ORDER_INTENT_BUY_SHORT"
    assert EXIT_INTENT["Up"] == "ORDER_INTENT_SELL_LONG"
    assert EXIT_INTENT["Down"] == "ORDER_INTENT_SELL_SHORT"


@pytest.mark.asyncio
async def test_open_trade_buy_long(trader, mock_us_client):
    result = await trader.open_trade(
        market_id="btc-updown-5m-123", question="BTC Up?", side="Up",
        price=0.55, size=50.0, signal_score=0.70,
        signal_strength="edge=15%", ev_at_entry=0.15,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    assert result.success
    mock_us_client.place_order.assert_called_once()
    call_kwargs = mock_us_client.place_order.call_args.kwargs
    assert call_kwargs["intent"] == "ORDER_INTENT_BUY_LONG"
    assert call_kwargs["quantity"] == 91  # round(50 / 0.55)


@pytest.mark.asyncio
async def test_open_trade_buy_short(trader, mock_us_client):
    result = await trader.open_trade(
        market_id="btc-updown-5m-456", question="BTC Down?", side="Down",
        price=0.40, size=40.0, signal_score=0.65,
        signal_strength="edge=12%", ev_at_entry=0.12,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    assert result.success
    call_kwargs = mock_us_client.place_order.call_args.kwargs
    assert call_kwargs["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert call_kwargs["quantity"] == 100  # int(40 / 0.40)


@pytest.mark.asyncio
async def test_close_trade_sell_long(trader, mock_us_client, db):
    await trader.open_trade(
        market_id="btc-updown-5m-789", question="BTC Up?", side="Up",
        price=0.55, size=55.0, signal_score=0.70,
        signal_strength="edge=15%", ev_at_entry=0.15,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    positions = await db.get_open_positions()
    result = await trader.close_trade(positions[0]["id"], exit_price=0.80)
    assert result.success
    assert result.log_return > 0
    call_kwargs = mock_us_client.close_position.call_args.kwargs
    assert call_kwargs["intent"] == "ORDER_INTENT_SELL_LONG"


@pytest.mark.asyncio
async def test_close_trade_sell_short(trader, mock_us_client, db):
    mock_us_client.place_order = AsyncMock(return_value={
        "state": "ORDER_STATE_FILLED", "averagePrice": "0.40"})
    await trader.open_trade(
        market_id="btc-updown-5m-short", question="BTC Down?", side="Down",
        price=0.40, size=40.0, signal_score=0.60,
        signal_strength="edge=10%", ev_at_entry=0.10,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    positions = await db.get_open_positions()
    result = await trader.close_trade(positions[0]["id"], exit_price=0.80)
    assert result.success
    call_kwargs = mock_us_client.close_position.call_args.kwargs
    assert call_kwargs["intent"] == "ORDER_INTENT_SELL_SHORT"


@pytest.mark.asyncio
async def test_order_rejected_returns_failure(trader, mock_us_client):
    mock_us_client.place_order = AsyncMock(return_value={
        "state": "ORDER_STATE_REJECTED", "reason": "insufficient funds"})
    result = await trader.open_trade(
        market_id="btc-updown-5m-fail", question="BTC Up?", side="Up",
        price=0.50, size=50.0, signal_score=0.65,
        signal_strength="edge=12%", ev_at_entry=0.12,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    assert not result.success
    assert "not filled" in result.reason.lower()


@pytest.mark.asyncio
async def test_duplicate_market_blocked(trader, mock_us_client):
    await trader.open_trade(
        market_id="btc-updown-5m-dup", question="Q?", side="Up",
        price=0.50, size=50.0, signal_score=0.65,
        signal_strength="e", ev_at_entry=0.10,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    result = await trader.open_trade(
        market_id="btc-updown-5m-dup", question="Q?", side="Up",
        price=0.50, size=50.0, signal_score=0.65,
        signal_strength="e", ev_at_entry=0.10,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    assert not result.success
    assert "duplicate" in result.reason.lower()


@pytest.mark.asyncio
async def test_balance_synced_after_trade(trader, mock_us_client, db):
    mock_us_client.get_balance = AsyncMock(return_value=950.0)
    await trader.open_trade(
        market_id="btc-updown-5m-sync", question="Q?", side="Up",
        price=0.50, size=50.0, signal_score=0.65,
        signal_strength="e", ev_at_entry=0.10,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    bankroll = await db.get_bankroll()
    assert bankroll == 950.0


@pytest.mark.asyncio
async def test_no_balance_returns_failure(trader, mock_us_client):
    mock_us_client.get_balance = AsyncMock(return_value=0.0)
    result = await trader.open_trade(
        market_id="btc-updown-5m-broke", question="Q?", side="Up",
        price=0.50, size=50.0, signal_score=0.65,
        signal_strength="e", ev_at_entry=0.10,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    assert not result.success
    assert "no balance" in result.reason.lower()


@pytest.mark.asyncio
async def test_api_error_retries_once(trader, mock_us_client):
    mock_us_client.place_order = AsyncMock(side_effect=[
        Exception("timeout"),
        {"state": "ORDER_STATE_FILLED", "averagePrice": "0.55"},
    ])
    result = await trader.open_trade(
        market_id="btc-updown-5m-retry", question="Q?", side="Up",
        price=0.55, size=50.0, signal_score=0.70,
        signal_strength="e", ev_at_entry=0.15,
        exit_target=1.0, stop_loss=0.0, weight_version="v001")
    assert result.success
    assert mock_us_client.place_order.call_count == 2
