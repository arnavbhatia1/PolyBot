import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from polybot.execution.live_trader import LiveTrader
from polybot.execution.base import TradeResult


@pytest_asyncio.fixture
async def db():
    from polybot.db.models import Database
    d = Database(":memory:")
    await d.initialize()
    await d.set_bankroll(1000.0)
    return d


@pytest.fixture
def mock_clob():
    clob = MagicMock()
    clob.get_balance_allowance.return_value = {"balance": "500000000"}  # 500 USDC (6 decimals)
    clob.create_market_order.return_value = {"signed": True}
    clob.post_order.return_value = {
        "status": "matched",
        "trades": [{"price": "0.50", "size": "100"}],
    }
    return clob


@pytest.fixture
def trader(db, mock_clob):
    return LiveTrader(db=db, clob=mock_clob, max_slippage=0.02,
                      max_bankroll_deployed=0.80, max_concurrent_positions=1)


@pytest.mark.asyncio
async def test_open_trade_success(trader, mock_clob):
    result = await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token123")
    assert result.success
    assert result.position_id is not None
    mock_clob.create_market_order.assert_called_once()
    mock_clob.post_order.assert_called_once()


@pytest.mark.asyncio
async def test_open_trade_no_token_id(trader):
    result = await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="")
    assert not result.success
    assert "token_id" in result.reason


@pytest.mark.asyncio
async def test_open_trade_order_not_filled(trader, mock_clob):
    mock_clob.post_order.return_value = {"status": "expired"}
    result = await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token123")
    assert not result.success
    assert "not filled" in result.reason


@pytest.mark.asyncio
async def test_open_trade_clob_error(trader, mock_clob):
    mock_clob.create_market_order.side_effect = Exception("network error")
    result = await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token123")
    assert not result.success
    assert "CLOB error" in result.reason


@pytest.mark.asyncio
async def test_open_trade_duplicate_market(trader, mock_clob):
    # First trade succeeds
    await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token123")
    # Second trade on same market fails
    result = await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token123")
    assert not result.success
    assert "Duplicate" in result.reason


@pytest.mark.asyncio
async def test_open_trade_max_positions(trader, mock_clob):
    await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token123")
    result = await trader.open_trade(
        market_id="0xdef", question="BTC Down?", side="Down", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token456")
    assert not result.success
    assert "Max positions" in result.reason


@pytest.mark.asyncio
async def test_open_trade_no_balance(trader, mock_clob):
    mock_clob.get_balance_allowance.return_value = {"balance": "0"}
    result = await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token123")
    assert not result.success
    assert "USDC balance" in result.reason


@pytest.mark.asyncio
async def test_close_trade_success(trader, mock_clob):
    mock_clob.post_order.return_value = {
        "status": "matched",
        "trades": [{"price": "0.80", "size": "200"}],
    }
    # Open first
    await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token123")
    # Close
    result = await trader.close_trade(1, 0.80, token_id="token123")
    assert result.success
    assert result.log_return is not None


@pytest.mark.asyncio
async def test_close_trade_no_token_id(trader, mock_clob):
    await trader.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="token123")
    result = await trader.close_trade(1, 0.80, token_id="")
    assert not result.success
    assert "token_id" in result.reason


@pytest.mark.asyncio
async def test_close_trade_not_found(trader):
    result = await trader.close_trade(999, 0.80, token_id="token123")
    assert not result.success
    assert "not found" in result.reason


@pytest.mark.asyncio
async def test_get_usdc_balance(trader, mock_clob):
    balance = await trader.get_usdc_balance()
    assert balance == 500.0  # 500_000_000 / 1e6


@pytest.mark.asyncio
async def test_get_usdc_balance_error(trader, mock_clob):
    mock_clob.get_balance_allowance.side_effect = Exception("timeout")
    balance = await trader.get_usdc_balance()
    assert balance == 0.0


def test_extract_fill_price_from_trades(trader):
    resp = {"trades": [{"price": "0.48", "size": "50"}, {"price": "0.52", "size": "50"}]}
    assert trader._extract_fill_price(resp, 0.50) == 0.50  # weighted avg


def test_extract_fill_price_fallback(trader):
    assert trader._extract_fill_price({}, 0.55) == 0.55


@pytest.mark.asyncio
async def test_paper_trader_ignores_token_id():
    """PaperTrader accepts token_id kwarg without breaking."""
    from polybot.db.models import Database
    from polybot.execution.paper_trader import PaperTrader
    d = Database(":memory:")
    await d.initialize()
    await d.set_bankroll(1000.0)
    pt = PaperTrader(db=d)
    result = await pt.open_trade(
        market_id="0xabc", question="BTC Up?", side="Up", price=0.50,
        size=100, signal_score=0.75, signal_strength="edge=25%",
        ev_at_entry=0.25, exit_target=1.0, stop_loss=0.0,
        weight_version="v1", token_id="ignored")
    assert result.success
    result2 = await pt.close_trade(1, 1.0, token_id="also_ignored")
    assert result2.success
