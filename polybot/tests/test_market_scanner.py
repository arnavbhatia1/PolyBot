import pytest
from polybot.feeds.market_scanner import BTCMarketScanner

SAMPLE_EVENT = {
    "title": "Bitcoin Up or Down - March 30, 3:25PM-3:30PM ET",
    "slug": "btc-updown-5m-1774898700",
    "active": True,
    "endDate": "2026-03-30T19:30:00Z",
    "markets": [{
        "conditionId": "0xabc123",
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["0.585", "0.415"]',
        "clobTokenIds": '["token_up_123", "token_down_456"]',
        "negRisk": False,
        "endDate": "2026-03-30T19:30:00Z",
    }],
}

def test_parse_contract_extracts_up_down():
    scanner = BTCMarketScanner()
    c = scanner.parse_contract(SAMPLE_EVENT)
    assert c["price_up"] == 0.585
    assert c["price_down"] == 0.415
    assert c["token_id_up"] == "token_up_123"
    assert c["token_id_down"] == "token_down_456"

def test_parse_contract_extracts_condition_id():
    scanner = BTCMarketScanner()
    c = scanner.parse_contract(SAMPLE_EVENT)
    assert c["condition_id"] == "0xabc123"

def test_parse_contract_extracts_title():
    scanner = BTCMarketScanner()
    c = scanner.parse_contract(SAMPLE_EVENT)
    assert "Bitcoin Up or Down" in c["question"]

def test_in_entry_window_true():
    assert BTCMarketScanner(entry_window_seconds=120).in_entry_window(seconds_remaining=240) is True

def test_in_entry_window_false_too_late():
    assert BTCMarketScanner(entry_window_seconds=120).in_entry_window(seconds_remaining=60) is False

def test_in_entry_window_false_too_early():
    # Just started (0 seconds elapsed, 300 remaining) should be in window
    assert BTCMarketScanner(entry_window_seconds=120).in_entry_window(seconds_remaining=300) is True

def test_make_slug():
    scanner = BTCMarketScanner(symbol="btc")
    assert scanner._make_slug(1774898700) == "btc-updown-5m-1774898700"


# --- clob_best_ask ---

def test_clob_best_ask_single():
    book = {"asks": [{"price": "0.55", "size": "150"}]}
    price, depth = BTCMarketScanner.clob_best_ask(book)
    assert price == 0.55
    assert depth == 150.0

def test_clob_best_ask_multiple_levels():
    book = {"asks": [{"price": "0.55", "size": "100"}, {"price": "0.60", "size": "200"}]}
    price, depth = BTCMarketScanner.clob_best_ask(book)
    assert price == 0.55
    assert depth == 300.0

def test_clob_best_ask_empty_book():
    assert BTCMarketScanner.clob_best_ask({}) == (0.0, 0.0)

def test_clob_best_ask_empty_asks():
    assert BTCMarketScanner.clob_best_ask({"asks": []}) == (0.0, 0.0)


# --- clob_best_bid ---

def test_clob_best_bid_single():
    book = {"bids": [{"price": "0.45", "size": "200"}]}
    price, depth = BTCMarketScanner.clob_best_bid(book)
    assert price == 0.45
    assert depth == 200.0

def test_clob_best_bid_multiple_levels():
    book = {"bids": [{"price": "0.45", "size": "100"}, {"price": "0.40", "size": "50"}]}
    price, depth = BTCMarketScanner.clob_best_bid(book)
    assert price == 0.45
    assert depth == 150.0

def test_clob_best_bid_empty_book():
    assert BTCMarketScanner.clob_best_bid({}) == (0.0, 0.0)

def test_clob_best_bid_empty_bids():
    assert BTCMarketScanner.clob_best_bid({"bids": []}) == (0.0, 0.0)


# --- clob_walk_asks ---

def test_clob_walk_asks_single_level_full_fill():
    book = {"asks": [{"price": "0.55", "size": "100"}]}
    assert BTCMarketScanner.clob_walk_asks(book, 50) == 0.55

def test_clob_walk_asks_multiple_levels_vwap():
    book = {"asks": [{"price": "0.55", "size": "100"}, {"price": "0.60", "size": "100"}]}
    vwap = BTCMarketScanner.clob_walk_asks(book, 150)
    # 100 * 0.55 + 50 * 0.60 = 55 + 30 = 85; 85 / 150 ≈ 0.5667
    assert abs(vwap - 0.5667) < 0.001

def test_clob_walk_asks_insufficient_depth():
    book = {"asks": [{"price": "0.55", "size": "10"}]}
    assert BTCMarketScanner.clob_walk_asks(book, 100) == 0.0

def test_clob_walk_asks_empty_book():
    assert BTCMarketScanner.clob_walk_asks({}, 50) == 0.0

def test_clob_walk_asks_zero_shares():
    book = {"asks": [{"price": "0.55", "size": "100"}]}
    assert BTCMarketScanner.clob_walk_asks(book, 0) == 0.0

def test_clob_walk_asks_fok_semantics():
    # FOK: must fill 100% — 90 out of 100 fails
    book = {"asks": [{"price": "0.55", "size": "90"}]}
    assert BTCMarketScanner.clob_walk_asks(book, 100) == 0.0
    # Exact fill succeeds
    book2 = {"asks": [{"price": "0.55", "size": "100"}]}
    assert BTCMarketScanner.clob_walk_asks(book2, 100) == 0.55


# --- clob_walk_bids ---

def test_clob_walk_bids_single_level_full_fill():
    book = {"bids": [{"price": "0.45", "size": "100"}]}
    assert BTCMarketScanner.clob_walk_bids(book, 50) == 0.45

def test_clob_walk_bids_multiple_levels_vwap():
    book = {"bids": [{"price": "0.45", "size": "100"}, {"price": "0.40", "size": "100"}]}
    vwap = BTCMarketScanner.clob_walk_bids(book, 150)
    # 100 * 0.45 + 50 * 0.40 = 45 + 20 = 65; 65 / 150 ≈ 0.4333
    assert abs(vwap - 0.4333) < 0.001

def test_clob_walk_bids_insufficient_depth():
    book = {"bids": [{"price": "0.45", "size": "10"}]}
    assert BTCMarketScanner.clob_walk_bids(book, 100) == 0.0

def test_clob_walk_bids_empty_book():
    assert BTCMarketScanner.clob_walk_bids({}, 50) == 0.0

def test_clob_walk_bids_zero_shares():
    book = {"bids": [{"price": "0.45", "size": "100"}]}
    assert BTCMarketScanner.clob_walk_bids(book, 0) == 0.0


# --- snap_to_tick ---

def test_snap_to_tick_rounds_down():
    assert BTCMarketScanner.snap_to_tick(0.557, "0.01") == 0.55

def test_snap_to_tick_exact():
    assert BTCMarketScanner.snap_to_tick(0.55, "0.01") == 0.55

def test_snap_to_tick_fine_grain():
    assert BTCMarketScanner.snap_to_tick(0.5567, "0.001") == 0.556

def test_snap_to_tick_clamps_min():
    assert BTCMarketScanner.snap_to_tick(0.001, "0.01") == 0.01

def test_snap_to_tick_clamps_max():
    assert BTCMarketScanner.snap_to_tick(0.999, "0.01") == 0.99


# --- clob_ask_depth / clob_bid_depth ---

def test_clob_ask_depth():
    book = {"asks": [{"price": "0.55", "size": "100"}, {"price": "0.60", "size": "50"}]}
    assert BTCMarketScanner.clob_ask_depth(book) == 150.0

def test_clob_bid_depth():
    book = {"bids": [{"price": "0.45", "size": "200"}, {"price": "0.40", "size": "100"}]}
    assert BTCMarketScanner.clob_bid_depth(book) == 300.0

def test_clob_ask_depth_empty():
    assert BTCMarketScanner.clob_ask_depth({}) == 0.0


# --- Lightweight HTTP helpers (mock responses) ---

from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_get_spread_returns_float():
    scanner = BTCMarketScanner()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"spread": "0.04"}
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    result = await scanner.get_spread("tok123", mock_client)
    assert result == 0.04

@pytest.mark.asyncio
async def test_get_spread_returns_neg_on_error():
    scanner = BTCMarketScanner()
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("timeout")
    result = await scanner.get_spread("tok123", mock_client)
    assert result == -1.0


def test_parse_contract_extracts_event_metadata():
    event = {
        **SAMPLE_EVENT,
        "eventMetadata": {
            "priceToBeat": 72151.5956398658,
            "finalPrice": 72304.1338669154,
        },
    }
    c = BTCMarketScanner().parse_contract(event)
    assert c["event_metadata"]["price_to_beat"] == pytest.approx(72151.60, rel=1e-4)
    assert c["event_metadata"]["final_price"] == pytest.approx(72304.13, rel=1e-4)


def test_parse_contract_event_metadata_null():
    event = {**SAMPLE_EVENT, "eventMetadata": None}
    c = BTCMarketScanner().parse_contract(event)
    assert c["event_metadata"] is None


def test_parse_contract_event_metadata_missing():
    c = BTCMarketScanner().parse_contract(SAMPLE_EVENT)
    assert c["event_metadata"] is None


def test_parse_contract_handles_list_outcomes():
    event = SAMPLE_EVENT.copy()
    event["markets"] = [{
        "conditionId": "0xdef",
        "outcomes": ["Up", "Down"],
        "outcomePrices": ["0.60", "0.40"],
        "clobTokenIds": ["tok_up", "tok_down"],
        "negRisk": False,
    }]
    c = BTCMarketScanner().parse_contract(event)
    assert c["price_up"] == 0.60
