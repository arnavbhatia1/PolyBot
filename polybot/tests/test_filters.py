import pytest
from polybot.core.filters import MarketFilter

@pytest.fixture
def default_filter():
    return MarketFilter(
        min_volume_24h=1000, min_liquidity=500, min_days_to_expiry=2,
        max_days_to_expiry=60, max_spread=0.05, category_whitelist=[], category_blacklist=[],
    )

def _make_market(**overrides):
    base = {
        "condition_id": "market_123", "question": "Will X happen?",
        "tokens": [{"price": 0.55}, {"price": 0.45}],
        "volume_24h": 5000.0, "liquidity": 2000.0, "days_to_expiry": 15,
        "spread": 0.02, "category": "politics",
    }
    base.update(overrides)
    return base

def test_good_market_passes(default_filter):
    assert default_filter.passes(_make_market()) is True

def test_low_volume_fails(default_filter):
    assert default_filter.passes(_make_market(volume_24h=500)) is False

def test_low_liquidity_fails(default_filter):
    assert default_filter.passes(_make_market(liquidity=200)) is False

def test_too_short_expiry_fails(default_filter):
    assert default_filter.passes(_make_market(days_to_expiry=1)) is False

def test_too_long_expiry_fails(default_filter):
    assert default_filter.passes(_make_market(days_to_expiry=90)) is False

def test_wide_spread_fails(default_filter):
    assert default_filter.passes(_make_market(spread=0.08)) is False

def test_category_blacklist():
    f = MarketFilter(min_volume_24h=1000, min_liquidity=500, min_days_to_expiry=2,
        max_days_to_expiry=60, max_spread=0.05, category_whitelist=[], category_blacklist=["celebrity"])
    assert f.passes(_make_market(category="celebrity")) is False
    assert f.passes(_make_market(category="politics")) is True

def test_category_whitelist():
    f = MarketFilter(min_volume_24h=1000, min_liquidity=500, min_days_to_expiry=2,
        max_days_to_expiry=60, max_spread=0.05, category_whitelist=["politics", "crypto"], category_blacklist=[])
    assert f.passes(_make_market(category="politics")) is True
    assert f.passes(_make_market(category="sports")) is False

def test_filter_batch(default_filter):
    markets = [_make_market(condition_id="good"), _make_market(condition_id="bad_vol", volume_24h=100), _make_market(condition_id="bad_liq", liquidity=50)]
    result = default_filter.filter_batch(markets)
    assert len(result) == 1
    assert result[0]["condition_id"] == "good"

def test_update_filter_param(default_filter):
    default_filter.update("min_volume_24h", 5000)
    assert default_filter.min_volume_24h == 5000
    assert default_filter.passes(_make_market(volume_24h=3000)) is False
