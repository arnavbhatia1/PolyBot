import asyncio
import json
import pytest
from polybot.feeds.clob_ws import ClobWebSocket


@pytest.fixture
def ws():
    return ClobWebSocket(url="wss://test.example.com/ws/market")


# --- Message parsing (synchronous, no real WS connection needed) ---

def test_handle_book_snapshot(ws):
    msg = json.dumps({
        "event_type": "book",
        "asset_id": "token_up_123",
        "market": "0xabc",
        "bids": [{"price": "0.45", "size": "200"}],
        "asks": [{"price": "0.55", "size": "150"}],
        "timestamp": "1757908892351",
        "hash": "0xdef",
    })
    ws._handle_message(msg)
    book = ws.get_book("token_up_123")
    assert book["bids"] == [{"price": "0.45", "size": "200"}]
    assert book["asks"] == [{"price": "0.55", "size": "150"}]
    assert ws.book_updated.is_set()


def test_handle_book_replaces_previous(ws):
    msg1 = json.dumps({
        "event_type": "book",
        "asset_id": "token_up_123",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.60", "size": "50"}],
    })
    ws._handle_message(msg1)
    assert ws.get_book("token_up_123")["bids"][0]["price"] == "0.40"

    msg2 = json.dumps({
        "event_type": "book",
        "asset_id": "token_up_123",
        "bids": [{"price": "0.45", "size": "200"}],
        "asks": [{"price": "0.55", "size": "150"}],
    })
    ws._handle_message(msg2)
    assert ws.get_book("token_up_123")["bids"][0]["price"] == "0.45"


def test_handle_price_change(ws):
    msg = json.dumps({
        "event_type": "price_change",
        "market": "0xabc",
        "price_changes": [{
            "asset_id": "token_up_123",
            "price": "0.50",
            "size": "200",
            "side": "BUY",
            "best_bid": "0.50",
            "best_ask": "0.55",
        }],
    })
    ws._handle_message(msg)
    bba = ws.best_bid_ask.get("token_up_123")
    assert bba["best_bid"] == "0.50"
    assert bba["best_ask"] == "0.55"
    assert ws.book_updated.is_set()


def test_handle_best_bid_ask(ws):
    msg = json.dumps({
        "event_type": "best_bid_ask",
        "asset_id": "token_up_123",
        "market": "0xabc",
        "best_bid": "0.73",
        "best_ask": "0.77",
        "spread": "0.04",
    })
    ws._handle_message(msg)
    bba = ws.best_bid_ask["token_up_123"]
    assert bba["best_bid"] == "0.73"
    assert bba["best_ask"] == "0.77"
    assert bba["spread"] == "0.04"
    assert ws.book_updated.is_set()


def test_handle_last_trade(ws):
    msg = json.dumps({
        "event_type": "last_trade_price",
        "asset_id": "token_up_123",
        "market": "0xabc",
        "price": "0.456",
        "size": "219.22",
        "side": "BUY",
    })
    ws._handle_message(msg)
    lt = ws.last_trade["token_up_123"]
    assert lt["price"] == "0.456"
    assert lt["size"] == "219.22"
    assert lt["side"] == "BUY"


def test_handle_market_resolved(ws):
    msg = json.dumps({"event_type": "market_resolved"})
    ws._handle_message(msg)
    assert ws.market_resolved.is_set()


def test_handle_pong_ignored(ws):
    ws._handle_message("PONG")
    assert not ws.book_updated.is_set()


def test_handle_invalid_json_ignored(ws):
    ws._handle_message("not json at all")
    assert not ws.book_updated.is_set()


def test_get_book_unknown_token(ws):
    assert ws.get_book("nonexistent") == {}


def test_multiple_tokens_separate_books(ws):
    for tid in ["token_up", "token_down"]:
        msg = json.dumps({
            "event_type": "book",
            "asset_id": tid,
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
        })
        ws._handle_message(msg)
    assert "token_up" in ws.books
    assert "token_down" in ws.books
    assert ws.books["token_up"] is not ws.books["token_down"]


# --- Subscription state ---

@pytest.mark.asyncio
async def test_subscribe_tracks_ids(ws):
    # subscribe without connection just tracks IDs
    await ws.subscribe(["token_a", "token_b"])
    assert "token_a" in ws._subscribed_ids
    assert "token_b" in ws._subscribed_ids


@pytest.mark.asyncio
async def test_subscribe_idempotent(ws):
    await ws.subscribe(["token_a"])
    await ws.subscribe(["token_a"])
    assert ws._subscribed_ids.count("token_a") == 1


@pytest.mark.asyncio
async def test_unsubscribe_clears_state(ws):
    ws.books["token_a"] = {"bids": [], "asks": []}
    ws.best_bid_ask["token_a"] = {"best_bid": "0.5"}
    ws.last_trade["token_a"] = {"price": "0.5"}
    ws._subscribed_ids.append("token_a")

    await ws.unsubscribe(["token_a"])
    assert "token_a" not in ws._subscribed_ids
    assert "token_a" not in ws.books
    assert "token_a" not in ws.best_bid_ask
    assert "token_a" not in ws.last_trade


def test_handle_array_message(ws):
    """Polymarket sends batch messages as JSON arrays."""
    msg = json.dumps([
        {"event_type": "book", "asset_id": "tok1", "bids": [], "asks": [{"price": "0.55", "size": "100"}]},
        {"event_type": "book", "asset_id": "tok2", "bids": [{"price": "0.45", "size": "50"}], "asks": []},
    ])
    ws._handle_message(msg)
    assert "tok1" in ws.books
    assert "tok2" in ws.books
    assert ws.book_updated.is_set()


def test_connected_default_false(ws):
    assert ws.connected is False


# --- Trade buffer tests ---

def test_trade_buffer_accumulates(ws):
    msg = {"asset_id": "token123", "price": "0.55", "size": "100", "side": "BUY"}
    ws._on_last_trade(msg)
    ws._on_last_trade(msg)
    history = ws.get_trade_history("token123")
    assert len(history) == 2
    assert history[0]["price"] == "0.55"
    assert history[0]["side"] == "BUY"
    assert "timestamp" in history[0]


def test_trade_buffer_empty_token(ws):
    assert ws.get_trade_history("nonexistent") == []
