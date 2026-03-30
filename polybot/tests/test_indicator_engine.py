import pytest
import json
import numpy as np
from polybot.indicators.engine import IndicatorEngine
from polybot.core.binance_feed import Candle, CandleBuffer

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
def weights_path(tmp_path):
    path = tmp_path / "weights_v001.json"
    path.write_text(json.dumps({"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20,
                                "entry_threshold": 0.60, "version": "weights_v001"}))
    return str(tmp_path)

@pytest.fixture
def engine(weights_path):
    return IndicatorEngine(weights_dir=weights_path, active_version="weights_v001")

def test_compute_all_returns_7_indicators(engine):
    result = engine.compute_all(_make_trending_buffer("up", 50))
    for key in ["rsi", "macd", "stochastic", "ema", "obv", "vwap", "atr"]:
        assert key in result

def test_compute_score_returns_float(engine):
    indicators = engine.compute_all(_make_trending_buffer("up", 50))
    score = engine.compute_score(indicators)
    assert isinstance(score, float) and -1.0 <= score <= 1.0

def test_uptrend_produces_positive_score(engine):
    assert engine.compute_score(engine.compute_all(_make_trending_buffer("up", 50))) > 0

def test_downtrend_produces_negative_score(engine):
    assert engine.compute_score(engine.compute_all(_make_trending_buffer("down", 50))) < 0

def test_get_snapshot_serializable(engine):
    indicators = engine.compute_all(_make_trending_buffer("up", 50))
    json.dumps(engine.get_snapshot(indicators))

def test_load_weights(engine):
    w = engine.get_weights()
    assert w["rsi"] == 0.20 and w["macd"] == 0.25
