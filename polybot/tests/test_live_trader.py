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
async def test_open_trade_fok_kill_is_definitive_no_fill(trader):
    """A 4xx from the exchange ("fully filled or killed") is a DEFINITIVE kill —
    the normal sniper miss when the ask reprices first, not an ambiguous POST:
    no resubmit, no settle wait, clean no-fill reason."""
    import httpx as _httpx
    from py_clob_client_v2.exceptions import PolyApiException

    trader.client.create_market_order.return_value = {"order": "signed-payload"}
    trader.client.post_order.side_effect = PolyApiException(
        resp=_httpx.Response(400, json={
            "error": "order couldn't be fully filled. FOK orders are fully filled or killed.",
            "orderID": "0xdead"}))
    result = await trader.open_trade(**_TRADE_KWARGS)

    assert result.success is False
    assert trader.client.post_order.call_count == 1
    assert "no fill" in result.reason.lower()
    assert "double fill" not in result.reason


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
    # cancel via the real py-clob-client-v2 API (cancel_orders(list)); there is no
    # bare client.cancel — the old assertion passed only against a phantom MagicMock attr.
    trader.client.cancel_orders.assert_called_once_with(["order-delayed"])


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
    shares_held = (await trader.db.get_open_positions())[0]["shares_held"]

    # Tokens still in the funder wallet — redeem hasn't fired: resolve_position
    # must report pending without closing the position.
    held = {"shares": shares_held}

    async def fake_chain_shares(token_id):
        return held["shares"]

    trader._chain_token_shares = fake_chain_shares
    trader.client.get_balance_allowance.return_value = {"balance": str(int(50.0 * 1e6))}
    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.success is False
    assert result.pending is True
    assert len(await trader.db.get_open_positions()) == 1

    # Redeem lands: tokens cleared on-chain; balance = remaining 50 + payout.
    held["shares"] = 0.0
    winning_balance = 50.0 + shares_held
    trader.client.get_balance_allowance.return_value = {"balance": str(int(winning_balance * 1e6))}
    trader._redeem_pending[pos_id]["next_check"] = 0.0  # bypass the 10s rate limit
    result = await trader.resolve_position(pos_id, exit_price=1.0)

    assert result.success is True
    trader.client.update_balance_allowance.assert_called()  # CLOB cache busted pre-book
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(winning_balance, rel=1e-4)


@pytest.mark.asyncio
async def test_resolve_position_winner_redeemed_before_first_check(trader):
    """07-04 live bug: the auto-redeem credited BEFORE the first balance
    snapshot, so a balance-delta wait hid the payout inside its baseline and
    spun forever. Token-absence is the authority — already-cleared tokens must
    book on the FIRST resolve call."""
    _setup_successful_fill(trader, fill_price="0.50", fill_size="100.0")
    kwargs = {**_TRADE_KWARGS, "price": 0.50, "size": 50.0, "market_id": "mkt-race"}
    open_result = await trader.open_trade(**kwargs)
    pos_id = open_result.position_id
    shares_held = (await trader.db.get_open_positions())[0]["shares_held"]

    async def fake_chain_shares(token_id):
        return 0.0  # tokens already burned — redeem landed before we looked

    trader._chain_token_shares = fake_chain_shares
    winning_balance = 50.0 + shares_held
    trader.client.get_balance_allowance.return_value = {"balance": str(int(winning_balance * 1e6))}

    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.success is True
    assert (await trader.db.get_bankroll()) == pytest.approx(winning_balance, rel=1e-4)
    assert pos_id not in trader._redeem_pending


@pytest.mark.asyncio
async def test_resolve_position_winner_deadline_stays_pending_not_booked(trader):
    """If the redeem never fires, the deadline must NOT book the un-redeemed
    balance — that silently drops the winner's payout and strands the tokens
    on-chain. The position stays PENDING (CRITICAL fires once, operator can
    redeem manually), and a late-landing redeem still resolves."""
    _setup_successful_fill(trader, fill_price="0.50", fill_size="100.0")
    kwargs = {**_TRADE_KWARGS, "price": 0.50, "size": 50.0, "market_id": "mkt-deadline"}
    open_result = await trader.open_trade(**kwargs)
    pos_id = open_result.position_id
    shares_held = (await trader.db.get_open_positions())[0]["shares_held"]

    held = {"shares": shares_held}

    async def fake_chain_shares(token_id):
        return held["shares"]

    trader._chain_token_shares = fake_chain_shares
    trader.client.get_balance_allowance.return_value = {"balance": str(int(50.0 * 1e6))}
    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.pending is True

    # Deadline passes, tokens STILL held → must remain pending, NOT book the
    # raw 50.0 (which would lose the winnings). Position stays open.
    trader._redeem_pending[pos_id]["deadline"] = 0.0
    trader._redeem_pending[pos_id]["next_check"] = 0.0
    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.pending is True
    assert len(await trader.db.get_open_positions()) == 1
    assert trader._redeem_pending[pos_id]["alerted"] is True

    # The redeem finally lands late → resolves correctly to the winning balance.
    held["shares"] = 0.0
    winning_balance = 50.0 + shares_held
    trader.client.get_balance_allowance.return_value = {"balance": str(int(winning_balance * 1e6))}
    trader._redeem_pending[pos_id]["next_check"] = 0.0
    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.success is True
    bankroll = await trader.db.get_bankroll()
    assert bankroll == pytest.approx(winning_balance, rel=1e-4)


@pytest.mark.asyncio
async def test_fill_audit_corrects_entry_to_exchange_price(trader, monkeypatch):
    """07-07 live finding: the WS-tape VWAP fell back to the padded limit (0.93
    booked, 0.88 real per the exchange). The post-fill audit must correct
    entry_price + shares to the chain's avgPrice for single-fill positions."""
    _setup_successful_fill(trader, fill_price="0.93", fill_size="5.10")
    kwargs = {**_TRADE_KWARGS, "price": 0.93, "size": 4.74, "market_id": "mkt-audit"}
    open_result = await trader.open_trade(**kwargs)
    pos_id = open_result.position_id

    true_price = 0.88
    chain = [{"asset": _TRADE_KWARGS["token_id"], "size": 4.74 / true_price,
              "avgPrice": true_price}]

    async def fake_wallet():
        return chain

    monkeypatch.setattr(trader, "_fetch_wallet_positions", fake_wallet)
    import polybot.execution.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "_FILL_AUDIT_DELAY_S", 0.0)
    await trader._audit_entry_fill(pos_id, _TRADE_KWARGS["token_id"], 0.93, 4.74, 0.07)

    row = await (await trader.db.conn.execute(
        "SELECT entry_price, shares_held FROM positions WHERE id=?", (pos_id,))).fetchone()
    assert row[0] == pytest.approx(true_price, abs=1e-4)
    assert row[1] == pytest.approx((4.74 / true_price) * (1 - 0.07 * true_price * (1 - true_price) / true_price),
                                   rel=0.05)  # shares net of entry fee, loose bound

    # Mixed position (chain size far from this order's implied shares) → untouched.
    chain[0]["size"] = 25.0
    await trader._audit_entry_fill(pos_id, _TRADE_KWARGS["token_id"], 0.93, 4.74, 0.07)
    row2 = await (await trader.db.conn.execute(
        "SELECT entry_price FROM positions WHERE id=?", (pos_id,))).fetchone()
    assert row2[0] == pytest.approx(true_price, abs=1e-4)


@pytest.mark.asyncio
async def test_reconcile_dust_skips_resolved_markets(trader, monkeypatch):
    """Leftover shares of a RESOLVED market are worthless loser tokens (or a
    winner's auto-redeeming shares) — the CLOB rejects orders on closed markets,
    so the startup sweep must not even attempt them."""
    import json as _j
    import time as _t
    expired_ts = int(_t.time()) - 3600  # window closed an hour ago
    snap = _j.dumps({"trade_context": {"token_id_up": "tok-resolved-up",
                                       "token_id_down": "tok-resolved-down"}})
    await trader.db.conn.execute(
        "INSERT INTO positions (market_id, question, side, entry_price, size, "
        "signal_score, status, entry_timestamp, exit_price, exit_timestamp, "
        "indicator_snapshot, shares_held) "
        "VALUES (?, 'q', 'Up', 0.5, 5.0, 0.6, 'closed', datetime('now'), 0.0, "
        "datetime('now'), ?, 10.0)",
        (f"btc-updown-5m-{expired_ts}", snap),
    )
    await trader.db.conn.commit()

    attempted = []

    async def fake_sweep(token_id, ref_price):
        attempted.append(token_id)
        return True

    monkeypatch.setattr(trader, "_sweep_residual", fake_sweep)
    swept = await trader.reconcile_dust(trader.db)
    assert swept == 0
    assert attempted == []  # resolved market: no sweep attempt at all


@pytest.mark.asyncio
async def test_resolve_position_chain_api_failure_stays_pending(trader):
    """Data API unreachable → redemption unverifiable → stays pending, never books."""
    _setup_successful_fill(trader, fill_price="0.50", fill_size="100.0")
    kwargs = {**_TRADE_KWARGS, "price": 0.50, "size": 50.0, "market_id": "mkt-apifail"}
    open_result = await trader.open_trade(**kwargs)
    pos_id = open_result.position_id

    async def fake_chain_shares(token_id):
        return None

    trader._chain_token_shares = fake_chain_shares
    result = await trader.resolve_position(pos_id, exit_price=1.0)
    assert result.pending is True
    assert len(await trader.db.get_open_positions()) == 1


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
# Phase 1 live passive exit — GTD resting SELL (parity with paper)
# ---------------------------------------------------------------------------

async def _open_one(trader):
    _setup_successful_fill(trader, fill_price="0.50", fill_size="100.0")
    await trader.open_trade(**{**_TRADE_KWARGS, "price": 0.50, "size": 50.0})
    return (await trader.db.get_open_positions())[0]


@pytest.mark.asyncio
async def test_post_resting_sell_places_gtd_and_tracks(trader, monkeypatch):
    from py_clob_client_v2.clob_types import OrderType
    from py_clob_client_v2.order_builder.constants import SELL
    pos = await _open_one(trader)
    async def _sellable(token_id, fallback): return 100.0
    monkeypatch.setattr(trader, "_sellable_shares", _sellable)
    trader.client.create_order.return_value = {"order": "signed"}
    trader.client.post_order.return_value = {"success": True, "orderID": "rest-1"}

    ok = await trader.post_resting_sell(pos, "tok-up-123", level=0.60, timeout_s=10)
    assert ok is True
    assert trader._resting[pos["id"]]["order_id"] == "rest-1"
    # A GTD LIMIT order (create_order), not a market FOK, at the level, ~maker-headroom size.
    oa = trader.client.create_order.call_args[0][0]
    assert oa.price == 0.60 and oa.side == SELL and oa.size == pytest.approx(100.0 * 0.995)
    assert trader.client.post_order.call_args[0][1] == OrderType.GTD


@pytest.mark.asyncio
async def test_post_resting_sell_rejected_falls_back_to_fok(trader, monkeypatch):
    pos = await _open_one(trader)
    async def _sellable(token_id, fallback): return 100.0
    monkeypatch.setattr(trader, "_sellable_shares", _sellable)
    trader.client.create_order.return_value = {"order": "signed"}
    trader.client.post_order.return_value = {"success": False, "errorMsg": "rejected"}

    ok = await trader.post_resting_sell(pos, "tok-up-123", level=0.60, timeout_s=10)
    assert ok is False  # caller goes straight to FOK
    assert pos["id"] not in trader._resting


@pytest.mark.asyncio
async def test_post_resting_sell_below_min_falls_back(trader, monkeypatch):
    pos = await _open_one(trader)
    async def _sellable(token_id, fallback): return 1.0  # 1 share * 0.60 = $0.60 < $1
    monkeypatch.setattr(trader, "_sellable_shares", _sellable)
    ok = await trader.post_resting_sell(pos, "tok-up-123", level=0.60, timeout_s=10)
    assert ok is False
    trader.client.create_order.assert_not_called()  # no GTD limit order signed below min
    assert pos["id"] not in trader._resting


@pytest.mark.asyncio
async def test_poll_resting_fill_detects_full_fill(trader):
    pos = await _open_one(trader)
    trader._resting[pos["id"]] = {"order_id": "rest-1", "token_id": "tok-up-123", "shares": 99.5, "level": 0.60}
    trader.client.get_order.return_value = {"status": "matched"}
    assert await trader.poll_resting_fill(pos) is True
    assert pos["id"] not in trader._resting  # popped on fill


@pytest.mark.asyncio
async def test_poll_resting_fill_still_open(trader):
    pos = await _open_one(trader)
    trader._resting[pos["id"]] = {"order_id": "rest-1", "token_id": "tok-up-123", "shares": 99.5, "level": 0.60}
    trader.client.get_order.return_value = {"status": "live", "size_matched": "0", "original_size": "99.5"}
    assert await trader.poll_resting_fill(pos) is False
    assert pos["id"] in trader._resting  # still resting


@pytest.mark.asyncio
async def test_cancel_resting_clean(trader):
    pos = await _open_one(trader)
    trader._resting[pos["id"]] = {"order_id": "rest-1", "token_id": "tok-up-123", "shares": 99.5, "level": 0.60}
    trader.client.get_order.return_value = {"status": "cancelled", "size_matched": "0", "original_size": "99.5"}
    raced = await trader.cancel_resting(pos)
    assert raced is False
    trader.client.cancel_orders.assert_called_once_with(["rest-1"])
    assert pos["id"] not in trader._resting


@pytest.mark.asyncio
async def test_cancel_resting_race_fill_reported(trader):
    """Cancel can lose to a fill — must report it so the caller records the maker
    close instead of FOK-ing on already-sold shares (double-sell guard)."""
    pos = await _open_one(trader)
    trader._resting[pos["id"]] = {"order_id": "rest-1", "token_id": "tok-up-123", "shares": 99.5, "level": 0.60}
    trader.client.get_order.return_value = {"status": "matched"}  # filled in the cancel race
    raced = await trader.cancel_resting(pos)
    assert raced is True


def test_order_fully_filled_parsing():
    from polybot.execution.live_trader import LiveTrader as LT
    assert LT._order_fully_filled({"status": "matched"}, 99.5) is True   # status only (no size) → trust it
    assert LT._order_fully_filled({"status": "live", "size_matched": "0", "original_size": "99.5"}, 99.5) is False
    assert LT._order_fully_filled({"size_matched": "99.5", "original_size": "99.5"}, 99.5) is True
    assert LT._order_fully_filled({"size_matched": "40", "original_size": "99.5"}, 99.5) is False  # partial
    # SIZE beats status: a 'matched' status that's actually a partial must NOT read as full.
    assert LT._order_fully_filled({"status": "matched", "size_matched": "40", "original_size": "99.5"}, 99.5) is False
    assert LT._order_fully_filled(None, 99.5) is False


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
    """Chain has an UNRESOLVED token DB doesn't know about → strict mode raises."""
    from polybot.execution.live_trader import OrphanPositionError
    chain = [{"asset": "tok-UNKNOWN", "size": 50.0, "outcome": "Yes",
              "title": "Other market", "redeemable": False}]
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(payload=chain)):
        with pytest.raises(OrphanPositionError):
            await trader.detect_orphan_positions(trader.db, allow_orphans=False)


@pytest.mark.asyncio
async def test_detect_orphan_positions_lenient_proceeds(trader):
    """Same unresolved orphan but allow_orphans=True → returns count, does not raise."""
    chain = [{"asset": "tok-UNKNOWN", "size": 50.0, "outcome": "Yes", "title": "x",
              "redeemable": False}]
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(payload=chain)):
        count = await trader.detect_orphan_positions(trader.db, allow_orphans=True)
    assert count == 1


@pytest.mark.asyncio
async def test_resolved_dust_does_not_block(trader):
    """A RESOLVED unknown position (redeemable=true) is settled dust — strict mode
    must NOT raise and must NOT count it as an orphan (the live-boot bug fix)."""
    chain = [{"asset": "tok-RESOLVED", "size": 10.97, "outcome": "Down",
              "title": "Bitcoin Up or Down - May 12", "redeemable": True,
              "currentValue": 0.0}]
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(payload=chain)):
        count = await trader.detect_orphan_positions(trader.db, allow_orphans=False)
    assert count == 0


@pytest.mark.asyncio
async def test_resolved_dust_and_unresolved_orphan_mixed(trader):
    """Resolved dust is skipped; a co-present UNRESOLVED unknown still fail-closes."""
    from polybot.execution.live_trader import OrphanPositionError
    chain = [
        {"asset": "tok-RESOLVED", "size": 9.0, "outcome": "Down", "title": "May dust",
         "redeemable": True, "currentValue": 0.0},
        {"asset": "tok-LIVE-LOST", "size": 20.0, "outcome": "Up", "title": "open window",
         "redeemable": False},
    ]
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(payload=chain)):
        with pytest.raises(OrphanPositionError):
            await trader.detect_orphan_positions(trader.db, allow_orphans=False)


@pytest.mark.asyncio
async def test_missing_redeemable_fails_closed(trader):
    """Absent redeemable field is treated as unresolved (fail-closed) — an API
    schema change can't silently disarm the gate."""
    from polybot.execution.live_trader import OrphanPositionError
    chain = [{"asset": "tok-NOFIELD", "size": 15.0, "outcome": "Up", "title": "no redeemable key"}]
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(payload=chain)):
        with pytest.raises(OrphanPositionError):
            await trader.detect_orphan_positions(trader.db, allow_orphans=False)


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


# ---------------------------------------------------------------------------
# Order-submission latency infra
# ---------------------------------------------------------------------------

def test_clob_http_singleton_tuned_for_warm_orders():
    """The py-clob-client HTTP/2 singleton is replaced with a warm-keepalive +
    bounded-connect config so order POSTs ride a pooled connection (~135ms warm
    vs ~300ms cold) and a dead keepalive reconnect fails fast, not after ~20s."""
    import importlib
    import polybot.execution.live_trader  # noqa: F401 — ensures the singleton swap ran
    importlib.reload(polybot.execution.live_trader)
    from py_clob_client_v2.http_helpers import helpers
    client = helpers._http_client
    # keepalive must outlive the 5s ping so the order connection never lapses
    assert client._transport._pool._keepalive_expiry == 60.0
    # connect timeout bounded well under the 20s blanket default
    assert client.timeout.connect == 5.0


@pytest.mark.asyncio
async def test_prewarm_http_warms_version_cache(trader):
    """prewarm_http resolves the contract version off the hot path so the first
    order of the process skips the one-time get_version() RTT that
    create_market_order.__resolve_version would otherwise pay."""
    await trader.prewarm_http()
    # the name-mangled private resolver is invoked best-effort during prewarm
    trader.client._ClobClient__resolve_version.assert_called()


@pytest.mark.asyncio
async def test_prewarm_http_survives_missing_resolver(trader):
    """If the client lacks the private version resolver (SDK internals changed),
    prewarm must not raise — getattr(..., None) skips it and the first order pays
    the one-time RTT instead."""
    del trader.client._ClobClient__resolve_version
    await trader.prewarm_http()  # no exception; resolver simply skipped
