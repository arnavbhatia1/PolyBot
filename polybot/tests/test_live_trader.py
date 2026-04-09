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
