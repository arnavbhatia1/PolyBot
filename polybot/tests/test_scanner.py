import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.core.scanner import MarketScanner
from polybot.core.filters import MarketFilter

@pytest.fixture
def mock_filter():
    return MarketFilter(min_volume_24h=1000, min_liquidity=500, min_days_to_expiry=2,
        max_days_to_expiry=60, max_spread=0.05, category_whitelist=[], category_blacklist=[])

SAMPLE_CLOB_MARKETS = [
    {
        "condition_id": "0xabc123",
        "question": "Will BTC hit 100k?",
        "tokens": [
            {"token_id": "tok_yes", "outcome": "Yes", "price": 0.55},
            {"token_id": "tok_no", "outcome": "No", "price": 0.45},
        ],
        "end_date_iso": "2026-05-01T00:00:00Z",
        "volume_num_fmt": "5000",
        "liquidity_num_fmt": "2000",
        "spread": "0.02",
        "category": "crypto",
        "active": True,
        "closed": False,
    },
]

@pytest.mark.asyncio
async def test_fetch_markets_returns_normalized_data(mock_filter):
    scanner = MarketScanner(filter=mock_filter)
    scanner._fetch_raw_markets = AsyncMock(return_value=SAMPLE_CLOB_MARKETS)
    markets = await scanner.fetch_and_filter()
    assert len(markets) >= 0

@pytest.mark.asyncio
async def test_normalize_market_extracts_fields(mock_filter):
    scanner = MarketScanner(filter=mock_filter)
    raw = SAMPLE_CLOB_MARKETS[0]
    normalized = scanner.normalize_market(raw)
    assert normalized["condition_id"] == "0xabc123"
    assert normalized["question"] == "Will BTC hit 100k?"
    assert "price_yes" in normalized
    assert "volume_24h" in normalized
    assert "liquidity" in normalized
    assert "spread" in normalized
    assert "days_to_expiry" in normalized

@pytest.mark.asyncio
async def test_fetch_and_filter_applies_filter(mock_filter):
    scanner = MarketScanner(filter=mock_filter)
    good_market = SAMPLE_CLOB_MARKETS[0].copy()
    bad_market = SAMPLE_CLOB_MARKETS[0].copy()
    bad_market["condition_id"] = "0xbad"
    bad_market["volume_num_fmt"] = "100"
    scanner._fetch_raw_markets = AsyncMock(return_value=[good_market, bad_market])
    markets = await scanner.fetch_and_filter()
    condition_ids = [m["condition_id"] for m in markets]
    assert "0xbad" not in condition_ids
