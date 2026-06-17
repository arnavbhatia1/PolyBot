import sys
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch
from polybot.execution.base import entry_fee_shares
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
    creds = {
        "apiKey": "test-key",
        "secret": "test-secret",
        "passphrase": "test-pass",
    }
    mock.derive_api_key.return_value = creds
    mock.create_api_key.return_value = creds
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
        with patch("py_clob_client_v2.client.ClobClient", return_value=_mock_clob_client()) as MockClient:
            sys.modules.pop("polybot.execution.live_trader", None)
            from polybot.execution.live_trader import LiveTrader
            trader = LiveTrader(db=db)
            MockClient.assert_called_once()
            trader.client.derive_api_key.assert_called_once()
            trader.client.set_api_creds.assert_called_once()


@pytest.mark.asyncio
async def test_init_raises_without_private_key(db):
    with patch.dict("os.environ", {}, clear=True):
        sys.modules.pop("polybot.execution.live_trader", None)
        from polybot.execution.live_trader import LiveTrader
        with pytest.raises(ValueError, match="POLYMARKET_PRIVATE_KEY"):
            LiveTrader(db=db)


@pytest.mark.asyncio
async def test_init_raises_without_funder(db):
    """Safe signature type signs against the funder — without it every order
    fails downstream with an opaque signing error, so boot fails loudly."""
    with patch.dict("os.environ", {"POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32}, clear=True):
        sys.modules.pop("polybot.execution.live_trader", None)
        from polybot.execution.live_trader import LiveTrader
        with pytest.raises(ValueError, match="POLYMARKET_FUNDER"):
            LiveTrader(db=db)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def trader(db):
    """Create a LiveTrader with a mocked SDK client."""
    sys.modules.pop("polybot.execution.live_trader", None)
    with patch("py_clob_client_v2.client.ClobClient", return_value=_mock_clob_client()):
        with patch.dict("os.environ", {
            "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
            "POLYMARKET_FUNDER": "0xdeadbeef",
        }):
            from polybot.execution.live_trader import LiveTrader
            t = LiveTrader(db=db)
            yield t
    sys.modules.pop("polybot.execution.live_trader", None)


def _setup_successful_fill(trader, fill_price="0.55", fill_size="18.18"):
    """Wire up mock for successful FOK fill.

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
async def test_open_trade_handles_fok_failure(trader):
    """post_order returns non-retryable INVALID_ORDER_NOT_ENOUGH_BALANCE — fails, bankroll unchanged."""
    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order
    trader.client.post_order.return_value = {
        "success": False,
        "errorMsg": "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    }

    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is False
    assert "INVALID_ORDER_NOT_ENOUGH_BALANCE" in result.reason

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

    # fill_size returned by _submit_fok_order for BUY = amount = size = 10.0
    # fill_price = VWAP from associate_trades = 0.55
    # shares_ordered = fill_size / fill_price = 10.0 / 0.55
    shares_ordered = 10.0 / 0.55
    fee_in_shares = entry_fee_shares(shares_ordered, 0.55, 0.018)
    expected_shares = shares_ordered - fee_in_shares

    assert pos["shares_held"] == pytest.approx(expected_shares, rel=1e-4)


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_trade_retries_on_transient_failure(trader, monkeypatch):
    """post_order fails once with retryable error, then succeeds — call_count == 2."""
    import polybot.execution.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "_RETRY_BASE_DELAY", 0.01)

    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order

    failure_resp = {"success": False, "errorMsg": "TRANSIENT_NETWORK_ERROR"}
    success_resp = {"success": True, "status": "matched", "orderID": "order-retry"}
    trader.client.post_order.side_effect = [failure_resp, success_resp]

    trader.client.get_order.return_value = {
        "associate_trades": [{"price": "0.55", "size": "18.18"}],
    }

    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is True
    assert trader.client.post_order.call_count == 2


@pytest.mark.asyncio
async def test_open_trade_no_retry_on_balance_error(trader):
    """Non-retryable INVALID_ORDER_NOT_ENOUGH_BALANCE — post_order called exactly once."""
    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order
    trader.client.post_order.return_value = {
        "success": False,
        "errorMsg": "INVALID_ORDER_NOT_ENOUGH_BALANCE",
    }

    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is False
    assert trader.client.post_order.call_count == 1


@pytest.mark.asyncio
async def test_open_trade_no_resubmit_on_post_exception(trader, monkeypatch):
    """post_order raising = ambiguous (the order may have reached the exchange).
    The loop must NOT submit a second order — that's the double-fill path."""
    import polybot.execution.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "_RETRY_BASE_DELAY", 0.01)

    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order
    trader.client.post_order.side_effect = ConnectionError("socket closed")

    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is False
    assert "double fill" in result.reason
    assert trader.client.post_order.call_count == 1


@pytest.mark.asyncio
async def test_open_trade_retries_on_sign_exception(trader, monkeypatch):
    """Signing is local — nothing reached the exchange, so a retry is safe.
    create_market_order raises once, then the order succeeds — post_order
    still happens and the trade fills."""
    import polybot.execution.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "_RETRY_BASE_DELAY", 0.01)

    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.side_effect = [
        ConnectionError("sign hiccup"), signed_order,
    ]
    trader.client.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "order-sign-retry",
    }
    trader.client.get_order.return_value = {
        "associate_trades": [{"price": "0.55", "size": "18.18"}],
    }

    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is True
    assert trader.client.create_market_order.call_count == 2
    assert trader.client.post_order.call_count == 1


@pytest.mark.asyncio
async def test_open_trade_unmatched_status_cancels_instead_of_retrying(trader, monkeypatch):
    """An accepted-but-unmatched FOK (e.g. "delayed") is cancelled and settled
    from its trade record — never resubmitted."""
    import polybot.execution.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "_RETRY_BASE_DELAY", 0.01)

    signed_order = {"order": "signed-payload"}
    trader.client.create_market_order.return_value = signed_order
    trader.client.post_order.return_value = {
        "success": True, "status": "delayed", "orderID": "order-delayed",
    }
    # No associated trades → the delayed order never filled.
    trader.client.get_order.return_value = {"associate_trades": []}

    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is False
    assert trader.client.post_order.call_count == 1
    trader.client.cancel.assert_called_once_with("order-delayed")


# ---------------------------------------------------------------------------
# close_trade tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_trade_success(trader):
    # Open a position first
    _setup_successful_fill(trader, fill_price="0.55", fill_size="18.18")
    open_result = await trader.open_trade(**_TRADE_KWARGS)
    assert open_result.success is True
    pos_id = open_result.position_id

    # Reconfigure mock for the sell side at 0.68
    _setup_successful_fill(trader, fill_price="0.68", fill_size="18.18")

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
    _setup_successful_fill(trader, fill_price="0.50", fill_size="100.0")
    kwargs = {**_TRADE_KWARGS, "price": 0.50, "size": 50.0}
    open_result = await trader.open_trade(**kwargs)
    assert open_result.success is True
    pos_id = open_result.position_id

    # Figure out actual shares held from DB
    positions = await trader.db.get_open_positions()
    shares_held = positions[0]["shares_held"]

    # First tick: auto-redeem hasn't landed — balance still pre-redeem.
    # resolve_position must report pending without closing the position.
    trader.client.get_balance_allowance.return_value = {
        "balance": str(int(50.0 * 1e6))
    }
    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.success is False
    assert result.pending is True
    assert len(await trader.db.get_open_positions()) == 1

    # Redeem lands: remaining bankroll (50) + shares_held * $1.
    winning_balance = 50.0 + shares_held
    trader.client.get_balance_allowance.return_value = {
        "balance": str(int(winning_balance * 1e6))
    }
    result = await trader.resolve_position(pos_id, exit_price=1.0)

    assert result.success is True
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(winning_balance, rel=1e-4)


@pytest.mark.asyncio
async def test_resolve_position_winner_deadline_stays_pending_not_booked(trader):
    """06-17 fix: if the auto-redeem never lands, the deadline must NOT book the
    raw (un-redeemed) balance — that silently drops the winner's payout and
    strands the tokens on-chain. The position stays PENDING (loop keeps polling /
    operator can manually redeem), and a late-landing redeem still resolves."""
    _setup_successful_fill(trader, fill_price="0.50", fill_size="100.0")
    kwargs = {**_TRADE_KWARGS, "price": 0.50, "size": 50.0, "market_id": "mkt-deadline"}
    open_result = await trader.open_trade(**kwargs)
    pos_id = open_result.position_id
    shares_held = (await trader.db.get_open_positions())[0]["shares_held"]

    # Auto-redeem hasn't landed: balance still pre-redeem.
    trader.client.get_balance_allowance.return_value = {"balance": str(int(50.0 * 1e6))}
    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.pending is True

    # Deadline passes, redeem STILL not landed → must remain pending, NOT book the
    # raw 50.0 (which would lose the winnings). Position stays open.
    trader._redeem_pending[pos_id]["deadline"] = 0.0
    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.pending is True
    assert len(await trader.db.get_open_positions()) == 1

    # The redeem finally lands late → resolves correctly to the winning balance.
    winning_balance = 50.0 + shares_held
    trader.client.get_balance_allowance.return_value = {"balance": str(int(winning_balance * 1e6))}
    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.success is True
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(winning_balance, rel=1e-4)


@pytest.mark.asyncio
async def test_resolve_position_loser(trader):
    # Open a position at price=0.50, size=50.0
    _setup_successful_fill(trader, fill_price="0.50", fill_size="100.0")
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


# ---------------------------------------------------------------------------
# Orphan position detection — verifies the startup safety gate
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Async context manager mimicking httpx.AsyncClient.get's contract."""
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload if payload is not None else []
        self._raise_exc = raise_exc
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def get(self, url, params=None):
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResponse(self._payload)


@pytest.mark.asyncio
async def test_detect_orphan_positions_no_orphans(trader):
    """Chain shows positions but all known to DB → returns 0, does not raise."""
    # DB knows tokens via an open position's indicator_snapshot
    import json as _j
    snap = _j.dumps({"trade_context": {"token_id_up": "tok-A", "token_id_down": "tok-B"}})
    await trader.db.open_position_and_debit_bankroll(
        new_bankroll=90.0,
        market_id="m1", question="?", side="Up", entry_price=0.5, size=10.0,
        signal_score=0.6, indicator_snapshot=snap,
        fee_rate=0.018, shares_held=20.0,
    )
    chain = [{"asset": "tok-A", "size": 20.0, "outcome": "Yes", "title": "BTC up 5min"}]
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(payload=chain)):
        count = await trader.detect_orphan_positions(trader.db, allow_orphans=False)
    assert count == 0


@pytest.mark.asyncio
async def test_detect_orphan_positions_strict_raises(trader):
    """Chain has a token DB doesn't know about → strict mode raises."""
    from polybot.execution.live_trader import OrphanPositionError
    chain = [{"asset": "tok-UNKNOWN", "size": 50.0, "outcome": "Yes", "title": "Other market"}]
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(payload=chain)):
        with pytest.raises(OrphanPositionError):
            await trader.detect_orphan_positions(trader.db, allow_orphans=False)


@pytest.mark.asyncio
async def test_detect_orphan_positions_lenient_proceeds(trader):
    """Same orphan but allow_orphans=True → returns count, does not raise."""
    chain = [{"asset": "tok-UNKNOWN", "size": 50.0, "outcome": "Yes", "title": "x"}]
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(payload=chain)):
        count = await trader.detect_orphan_positions(trader.db, allow_orphans=True)
    assert count == 1


@pytest.mark.asyncio
async def test_detect_orphan_positions_api_failure_fails_closed(trader):
    """Data API failure + strict mode → raise (fail-closed)."""
    from polybot.execution.live_trader import OrphanPositionError
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(raise_exc=RuntimeError("timeout"))):
        with pytest.raises(OrphanPositionError):
            await trader.detect_orphan_positions(trader.db, allow_orphans=False)


@pytest.mark.asyncio
async def test_detect_orphan_positions_api_failure_lenient_proceeds(trader):
    """Data API failure + --allow-orphans → returns 0, logs warning."""
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(raise_exc=RuntimeError("timeout"))):
        count = await trader.detect_orphan_positions(trader.db, allow_orphans=True)
    assert count == 0


@pytest.mark.asyncio
async def test_detect_orphan_positions_dust_ignored(trader):
    """Chain dust below _ORPHAN_MIN_SHARES is not flagged as orphan."""
    chain = [{"asset": "tok-DUST", "size": 0.3, "outcome": "Yes", "title": "x"}]
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(payload=chain)):
        count = await trader.detect_orphan_positions(trader.db, allow_orphans=False)
    assert count == 0
