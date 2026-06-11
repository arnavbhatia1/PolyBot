import pytest
import json
from polybot.indicators.engine import IndicatorEngine
from polybot.feeds.binance_feed import Candle, CandleBuffer

def _make_trending_buffer(direction="up", size=60):
    """Build a 60-candle buffer with real high/low ranges — enough history for
    the ATR computation and its low-vol percentile gate."""
    buf = CandleBuffer(max_size=200)
    prices = []
    vols = []
    if direction == "up":
        p = 50000.0
        for i in range(30):
            p += 60
            prices.append(p)
            vols.append(120.0)
        for i in range(20):
            p -= 120
            prices.append(p)
            vols.append(100.0)
        for i in range(10):
            p += 30
            prices.append(p)
            vols.append(140.0)
    else:
        p = 55000.0
        for i in range(30):
            p -= 60
            prices.append(p)
            vols.append(120.0)
        for i in range(20):
            p += 120
            prices.append(p)
            vols.append(100.0)
        for i in range(10):
            p -= 30
            prices.append(p)
            vols.append(140.0)
    for i, (price, vol) in enumerate(zip(prices, vols)):
        buf.add(Candle(timestamp=i*60000, open=price-10, high=price+30, low=price-30,
                       close=price, volume=vol))
    return buf

@pytest.fixture
def engine():
    return IndicatorEngine()

def test_compute_all_returns_atr_only(engine):
    result = engine.compute_all(_make_trending_buffer("up", 50))
    assert set(result.keys()) == {"atr"}
    assert result["atr"]["atr"] > 0
    assert "passes" in result["atr"] and "reason" in result["atr"]

def test_get_snapshot_serializable(engine):
    indicators = engine.compute_all(_make_trending_buffer("up", 50))
    json.dumps(engine.get_snapshot(indicators))
