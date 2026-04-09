import sys
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from polybot.execution.base import TradeResult
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
    mock.get_balance_allowance.return_value = {"balance": "10000000"}  # 10 USDC in wei (6 decimals)
    return mock


@pytest.mark.asyncio
async def test_init_creates_client_and_derives_creds(db):
    # Pop cached module FIRST so the fresh import picks up the patch
    sys.modules.pop("polybot.execution.live_trader", None)
    with patch.dict("os.environ", {
        "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
        "POLYMARKET_FUNDER": "0x863DB57D4a54fA306091D53B4Fe19f1611221Be8",
    }):
        with patch("py_clob_client.client.ClobClient", return_value=_mock_clob_client()) as MockClient:
            # Re-import after patching so LiveTrader sees the mock
            sys.modules.pop("polybot.execution.live_trader", None)
            from polybot.execution.live_trader import LiveTrader
            trader = LiveTrader(db=db)
            MockClient.assert_called_once()
            trader.client.create_or_derive_api_creds.assert_called_once()
            trader.client.set_api_creds.assert_called_once()


@pytest.mark.asyncio
async def test_init_raises_without_private_key(db):
    with patch.dict("os.environ", {}, clear=True):
        # Remove cached module so we get a fresh import with cleared env
        sys.modules.pop("polybot.execution.live_trader", None)
        from polybot.execution.live_trader import LiveTrader
        with pytest.raises(ValueError, match="POLYMARKET_PRIVATE_KEY"):
            LiveTrader(db=db)


# ---------------------------------------------------------------------------
# open_trade tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def trader(db):
    """Create a LiveTrader with a mocked SDK client."""
    import sys
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


def _setup_successful_fill(trader, fill_price="0.55", fill_size="10.0"):
    """Wire up mock client to simulate a successful GTC fill."""
    signed_order = {"order": "signed-payload"}
    trader.client.create_order.return_value = signed_order
    trader.client.post_order.return_value = {"orderID": "order-123"}
    trader.client.get_order.return_value = {
        "status": "MATCHED",
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


@pytest.mark.asyncio
async def test_open_trade_success(trader):
    _setup_successful_fill(trader)
    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is True
    assert result.position_id is not None

    # Bankroll should be debited by size (10.0): 100 - 10 = 90
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_open_trade_rejects_duplicate_market(trader):
    _setup_successful_fill(trader)
    # First trade succeeds
    r1 = await trader.open_trade(**_TRADE_KWARGS)
    assert r1.success is True

    # Second trade on same market is rejected
    r2 = await trader.open_trade(**_TRADE_KWARGS)
    assert r2.success is False
    assert "Duplicate" in r2.reason


@pytest.mark.asyncio
async def test_open_trade_rejects_bankroll_exceeded(trader):
    _setup_successful_fill(trader)
    # Bankroll is 100, max deployed = 80%. A size of 85 should be rejected.
    kwargs = {**_TRADE_KWARGS, "size": 85.0}
    result = await trader.open_trade(**kwargs)

    assert result.success is False
    assert "Bankroll" in result.reason or "bankroll" in result.reason.lower()


@pytest.mark.asyncio
async def test_open_trade_handles_unfilled_order(trader, monkeypatch):
    # Patch timeouts to make test fast
    import polybot.execution.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "_FILL_TIMEOUT", 0.1)
    monkeypatch.setattr(lt_mod, "_FILL_POLL_INTERVAL", 0.01)

    signed_order = {"order": "signed-payload"}
    trader.client.create_order.return_value = signed_order
    trader.client.post_order.return_value = {"orderID": "order-456"}
    # Order stays LIVE — never fills
    trader.client.get_order.return_value = {"status": "LIVE"}
    trader.client.cancel.return_value = None

    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is False
    assert "fill" in result.reason.lower() or "timeout" in result.reason.lower()
    trader.client.cancel.assert_called_once_with("order-456")

    # Bankroll unchanged
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_open_trade_stores_correct_shares(trader):
    _setup_successful_fill(trader, fill_price="0.55", fill_size="10.0")
    result = await trader.open_trade(**_TRADE_KWARGS)
    assert result.success is True

    positions = await trader.db.get_open_positions()
    assert len(positions) == 1
    pos = positions[0]

    # shares_ordered = fill_size / fill_price = 10.0 / 0.55
    shares_ordered = 10.0 / 0.55
    from polybot.execution.paper_trader import entry_fee_shares
    fee_in_shares = entry_fee_shares(shares_ordered, 0.55, 0.018)
    expected_shares = shares_ordered - fee_in_shares

    assert pos["shares_held"] == pytest.approx(expected_shares, rel=1e-4)


# ---------------------------------------------------------------------------
# close_trade tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_trade_success(trader):
    # Open a position first
    _setup_successful_fill(trader, fill_price="0.55", fill_size="10.0")
    open_result = await trader.open_trade(**_TRADE_KWARGS)
    assert open_result.success is True
    pos_id = open_result.position_id

    # Reconfigure mock for the sell side
    _setup_successful_fill(trader, fill_price="0.68", fill_size="10.0")

    result = await trader.close_trade(pos_id, exit_price=0.68, token_id="tok-up-123")

    assert result.success is True
    assert result.log_return is not None
    # Sold at 0.68 vs bought at 0.55 — should profit. Bankroll was 90 after open.
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
    # Open a position at price=0.50, size=50.0
    _setup_successful_fill(trader, fill_price="0.50", fill_size="50.0")
    kwargs = {**_TRADE_KWARGS, "price": 0.50, "size": 50.0}
    open_result = await trader.open_trade(**kwargs)
    assert open_result.success is True
    pos_id = open_result.position_id

    # Figure out actual shares held from DB
    positions = await trader.db.get_open_positions()
    shares_held = positions[0]["shares_held"]

    # Mock balance to reflect winnings: remaining bankroll (50) + shares_held * $1
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
    # Open a position at price=0.50, size=50.0
    _setup_successful_fill(trader, fill_price="0.50", fill_size="50.0")
    kwargs = {**_TRADE_KWARGS, "price": 0.50, "size": 50.0, "market_id": "mkt-loser"}
    open_result = await trader.open_trade(**kwargs)
    assert open_result.success is True
    pos_id = open_result.position_id

    # Mock balance to reflect loss: just remaining bankroll (50), shares are worthless
    trader.client.get_balance_allowance.return_value = {
        "balance": str(int(50.0 * 1e6))
    }

    result = await trader.resolve_position(pos_id, exit_price=0.0)

    assert result.success is True
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(50.0, rel=1e-4)
