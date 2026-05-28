import pytest
import json
import numpy as np
from polybot.indicators.engine import IndicatorEngine
from polybot.feeds.binance_feed import Candle, CandleBuffer

def _make_trending_buffer(direction="up", size=60):
    """
    Build a buffer that produces a net-positive (up) or net-negative (down) aggregate
    indicator score. Pattern: strong initial trend raises VWAP, followed by a sharp
    counter-move below/above VWAP (driving RSI/stochastic oversold/overbought), then
    a recovery in the last 10 candles (positive OBV+price slope). This creates a
    mean-reversion buy (up) or sell (down) signal consistent with how RSI, stochastic,
    and VWAP are implemented as oscillators.
    """
    buf = CandleBuffer(max_size=200)
    prices = []
    vols = []
    if direction == "up":
        p = 50000.0
        for i in range(30):      # uptrend to push VWAP high
            p += 60
            prices.append(p)
            vols.append(120.0)
        for i in range(20):      # sharp pullback below VWAP, RSI drops oversold
            p -= 120
            prices.append(p)
            vols.append(100.0)
        for i in range(10):      # recovery: OBV slope up, price slope up
            p += 30
            prices.append(p)
            vols.append(140.0)
    else:
        p = 55000.0
        for i in range(30):      # downtrend to push VWAP low
            p -= 60
            prices.append(p)
            vols.append(120.0)
        for i in range(20):      # sharp bounce above VWAP, RSI rises overbought
            p += 120
            prices.append(p)
            vols.append(100.0)
        for i in range(10):      # decline: OBV slope down, price slope down
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

def test_compute_all_returns_7_indicators(engine):
    result = engine.compute_all(_make_trending_buffer("up", 50))
    for key in ["rsi", "macd", "stochastic", "ema", "obv", "vwap", "atr"]:
        assert key in result

def test_get_snapshot_serializable(engine):
    indicators = engine.compute_all(_make_trending_buffer("up", 50))
    json.dumps(engine.get_snapshot(indicators))

def test_default_weights(engine):
    w = engine.get_weights()
    assert w["rsi"] == 0.20 and w["macd"] == 0.25

def test_set_weights_updates_in_place(engine):
    engine.set_weights({"rsi": 0.30, "macd": 0.30, "stochastic": 0.15, "obv": 0.10, "vwap": 0.15})
    w = engine.get_weights()
    assert w["rsi"] == 0.30 and w["macd"] == 0.30


# IndicatorNormalizer removed in Pillar 2.3 — indicator scores are already
# bounded [-1, 1] by their compute_*_signal functions, so L4 reads them raw.
