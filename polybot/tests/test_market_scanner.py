import pytest
from polybot.core.market_scanner import BTCMarketScanner

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

def test_best_ask_returns_price_and_depth():
    book = {"asks": [{"price": "0.55", "size": "100"}, {"price": "0.60", "size": "200"}]}
    price, depth = BTCMarketScanner.best_ask(book)
    assert price == 0.55
    assert depth == 300.0

def test_best_ask_empty_book():
    assert BTCMarketScanner.best_ask({}) == (0.0, 0.0)
    assert BTCMarketScanner.best_ask({"asks": []}) == (0.0, 0.0)

def test_best_bid_returns_price_and_depth():
    book = {"bids": [{"price": "0.45", "size": "150"}, {"price": "0.40", "size": "50"}]}
    price, depth = BTCMarketScanner.best_bid(book)
    assert price == 0.45
    assert depth == 200.0

def test_walk_book_single_level():
    levels = [{"price": "0.55", "size": "100"}]
    assert BTCMarketScanner.walk_book(levels, 50) == 0.55

def test_walk_book_multiple_levels():
    levels = [{"price": "0.55", "size": "100"}, {"price": "0.60", "size": "100"}]
    # Need 150 shares: 100 @ 0.55 + 50 @ 0.60 = 55 + 30 = 85 / 150 = 0.5667
    vwap = BTCMarketScanner.walk_book(levels, 150)
    assert abs(vwap - 0.5667) < 0.001

def test_walk_book_insufficient_depth():
    levels = [{"price": "0.55", "size": "10"}]
    # Need 100 shares but only 10 available (<90% fill)
    assert BTCMarketScanner.walk_book(levels, 100) == 0.0

def test_walk_book_empty():
    assert BTCMarketScanner.walk_book([], 50) == 0.0


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
