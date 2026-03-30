# polybot/tests/test_binance_feed.py
import pytest
import numpy as np
from polybot.core.binance_feed import Candle, CandleBuffer

def _make_candle(timestamp=1000, open=50000, high=50100, low=49900, close=50050, volume=10.0):
    return Candle(timestamp=timestamp, open=open, high=high, low=low, close=close, volume=volume)

def test_candle_creation():
    c = _make_candle()
    assert c.close == 50050
    assert c.volume == 10.0

def test_buffer_add_candle():
    buf = CandleBuffer(max_size=5)
    buf.add(_make_candle(timestamp=1000))
    assert len(buf) == 1

def test_buffer_max_size():
    buf = CandleBuffer(max_size=3)
    for i in range(5):
        buf.add(_make_candle(timestamp=i * 60000))
    assert len(buf) == 3

def test_buffer_get_closes():
    buf = CandleBuffer(max_size=10)
    for i in range(5):
        buf.add(_make_candle(timestamp=i * 60000, close=100 + i))
    closes = buf.get_closes()
    assert len(closes) == 5
    assert closes[-1] == 104

def test_buffer_get_highs_lows():
    buf = CandleBuffer(max_size=10)
    buf.add(_make_candle(high=100, low=90))
    buf.add(_make_candle(high=110, low=85))
    assert buf.get_highs()[-1] == 110
    assert buf.get_lows()[-1] == 85

def test_buffer_get_volumes():
    buf = CandleBuffer(max_size=10)
    buf.add(_make_candle(volume=5.0))
    buf.add(_make_candle(volume=10.0))
    assert buf.get_volumes()[-1] == 10.0

def test_buffer_get_last_n():
    buf = CandleBuffer(max_size=10)
    for i in range(5):
        buf.add(_make_candle(timestamp=i * 60000, close=100 + i))
    last3 = buf.get_last_n(3)
    assert len(last3) == 3
    assert last3[-1].close == 104

def test_buffer_update_current_candle():
    buf = CandleBuffer(max_size=10)
    buf.add(_make_candle(timestamp=60000, close=100))
    buf.update_current(close=105, high=110, low=95, volume=20.0)
    assert buf.get_closes()[-1] == 105

def test_buffer_empty_returns_empty_arrays():
    buf = CandleBuffer(max_size=10)
    assert len(buf.get_closes()) == 0

def test_buffer_latest():
    buf = CandleBuffer(max_size=10)
    buf.add(_make_candle(close=999))
    assert buf.latest().close == 999

def test_buffer_latest_empty_returns_none():
    buf = CandleBuffer(max_size=10)
    assert buf.latest() is None
