import pytest
from polybot.core.market_scanner import BTCMarketScanner

SAMPLE = {"condition_id": "0xbtc5min", "question": "Will BTC be above $65,000 at 12:05 UTC?",
          "tokens": [{"token_id": "tok_yes", "outcome": "Yes", "price": 0.55},
                     {"token_id": "tok_no", "outcome": "No", "price": 0.45}],
          "end_date_iso": "2026-03-30T12:05:00Z", "active": True, "closed": False, "category": "crypto"}

def test_is_btc_5min_market():
    assert BTCMarketScanner().is_btc_5min_market(SAMPLE) is True

def test_is_not_btc_5min_market():
    non_btc = SAMPLE.copy()
    non_btc["question"] = "Will the election happen?"
    non_btc["category"] = "politics"
    assert BTCMarketScanner().is_btc_5min_market(non_btc) is False

def test_parse_contract():
    c = BTCMarketScanner().parse_contract(SAMPLE)
    assert c["condition_id"] == "0xbtc5min" and c["price_yes"] == 0.55

def test_in_entry_window_true():
    assert BTCMarketScanner(entry_window_seconds=120).in_entry_window(seconds_remaining=240) is True

def test_in_entry_window_false():
    assert BTCMarketScanner(entry_window_seconds=120).in_entry_window(seconds_remaining=60) is False
