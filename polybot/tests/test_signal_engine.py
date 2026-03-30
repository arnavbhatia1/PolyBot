import pytest
from polybot.core.signal_engine import SignalEngine, TradeSignal

def _make_indicators(atr_passes=True, ema_trend="bullish", rsi_score=0.5,
                     macd_score=0.5, stoch_score=0.5, obv_score=0.3, vwap_score=0.3):
    return {
        "atr": {"atr": 50.0, "passes": atr_passes, "reason": "ok"},
        "ema": {"trend": ema_trend, "fast_ema": 100.0, "slow_ema": 99.0},
        "rsi": {"rsi": 35.0, "score": rsi_score},
        "macd": {"macd": 0.1, "signal": 0.05, "histogram": 0.05, "score": macd_score},
        "stochastic": {"k": 25.0, "d": 30.0, "score": stoch_score},
        "obv": {"obv_slope": 100, "price_slope": 0.5, "score": obv_score},
        "vwap": {"vwap": 99.0, "deviation": -0.5, "score": vwap_score},
    }

@pytest.fixture
def engine():
    return SignalEngine(entry_threshold=0.60, weights={"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20})

def test_strong_bullish(engine):
    signal = engine.evaluate(_make_indicators(rsi_score=0.8, macd_score=0.9, stoch_score=0.7, obv_score=0.6, vwap_score=0.5),
                             has_position=False, in_entry_window=True)
    assert signal.action == "BUY_YES"

def test_strong_bearish(engine):
    signal = engine.evaluate(_make_indicators(ema_trend="bearish", rsi_score=-0.8, macd_score=-0.9,
                                               stoch_score=-0.7, obv_score=-0.6, vwap_score=-0.5),
                             has_position=False, in_entry_window=True)
    assert signal.action == "BUY_NO"

def test_weak_signal_skip(engine):
    signal = engine.evaluate(_make_indicators(rsi_score=0.1, macd_score=0.1, stoch_score=0.1, obv_score=0.1, vwap_score=0.1),
                             has_position=False, in_entry_window=True)
    assert signal.action == "SKIP"

def test_atr_blocks(engine):
    signal = engine.evaluate(_make_indicators(atr_passes=False, rsi_score=0.9, macd_score=0.9, stoch_score=0.9),
                             has_position=False, in_entry_window=True)
    assert signal.action == "SKIP" and "atr" in signal.reason.lower()

def test_ema_chop_blocks(engine):
    signal = engine.evaluate(_make_indicators(ema_trend="chop", rsi_score=0.9, macd_score=0.9, stoch_score=0.9),
                             has_position=False, in_entry_window=True)
    assert signal.action == "SKIP" and "chop" in signal.reason.lower()

def test_has_position_blocks(engine):
    assert engine.evaluate(_make_indicators(rsi_score=0.9, macd_score=0.9, stoch_score=0.9),
                           has_position=True, in_entry_window=True).action == "SKIP"

def test_outside_window_blocks(engine):
    assert engine.evaluate(_make_indicators(rsi_score=0.9, macd_score=0.9, stoch_score=0.9),
                           has_position=False, in_entry_window=False).action == "SKIP"

def test_signal_includes_score(engine):
    signal = engine.evaluate(_make_indicators(rsi_score=0.8, macd_score=0.9, stoch_score=0.7, obv_score=0.6, vwap_score=0.5),
                             has_position=False, in_entry_window=True)
    assert abs(signal.score) >= 0.60
