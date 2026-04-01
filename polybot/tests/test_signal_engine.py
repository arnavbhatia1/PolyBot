import pytest
from polybot.core.signal_engine import SignalEngine, TradeSignal

def _make_indicators(atr_passes=True, ema_trend="bullish", rsi_score=0.5,
                     macd_score=0.5, stoch_score=0.5, obv_score=0.3, vwap_score=0.3,
                     atr_value=30.0):
    return {
        "atr": {"atr": atr_value, "passes": atr_passes, "reason": "ok"},
        "ema": {"trend": ema_trend, "fast_ema": 100.0, "slow_ema": 99.0},
        "rsi": {"rsi": 35.0, "score": rsi_score},
        "macd": {"macd": 0.1, "signal": 0.05, "histogram": 0.05, "score": macd_score},
        "stochastic": {"k": 25.0, "d": 30.0, "score": stoch_score},
        "obv": {"obv_slope": 100, "price_slope": 0.5, "score": obv_score},
        "vwap": {"vwap": 99.0, "deviation": -0.5, "score": vwap_score},
    }

@pytest.fixture
def engine():
    return SignalEngine(min_edge=0.10, kelly_fraction=0.25)

def test_buys_up_when_btc_above_strike_and_market_underprices(engine):
    indicators = _make_indicators(atr_value=30.0)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=True,
                             btc_price=66500, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.55, market_price_down=0.45)
    assert signal.action == "BUY_YES"
    assert signal.edge > 0.10

def test_buys_down_when_btc_below_strike_and_market_underprices(engine):
    indicators = _make_indicators(ema_trend="bearish", rsi_score=-0.5, macd_score=-0.5,
                                   stoch_score=-0.5, obv_score=-0.3, vwap_score=-0.3)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=True,
                             btc_price=66300, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.45, market_price_down=0.55)
    assert signal.action == "BUY_NO"
    assert signal.edge > 0.10

def test_skips_when_no_edge(engine):
    indicators = _make_indicators(rsi_score=0.0, macd_score=0.0, stoch_score=0.0,
                                   obv_score=0.0, vwap_score=0.0)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=True,
                             btc_price=66400, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"

def test_atr_gate_blocks(engine):
    signal = engine.evaluate(_make_indicators(atr_passes=False), has_position=False, in_entry_window=True,
                             btc_price=66500, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"

def test_ema_chop_blocks(engine):
    signal = engine.evaluate(_make_indicators(ema_trend="chop"), has_position=False, in_entry_window=True,
                             btc_price=66500, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"

def test_has_position_blocks(engine):
    signal = engine.evaluate(_make_indicators(), has_position=True, in_entry_window=True,
                             btc_price=66500, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    assert signal.action == "SKIP"

def test_kelly_size_positive_with_edge(engine):
    signal = engine.evaluate(_make_indicators(atr_value=30.0), has_position=False, in_entry_window=True,
                             btc_price=66600, strike_price=66400,
                             seconds_remaining=180, market_price_up=0.55, market_price_down=0.45)
    assert signal.kelly_size > 0

def test_base_probability_increases_with_distance(engine):
    p1 = engine._compute_base_probability(66450, 66400, 180, 30)  # $50 above
    p2 = engine._compute_base_probability(66550, 66400, 180, 30)  # $150 above
    assert p2 > p1

def test_base_probability_increases_with_less_time(engine):
    p1 = engine._compute_base_probability(66500, 66400, 240, 30)
    p2 = engine._compute_base_probability(66500, 66400, 60, 30)
    assert p2 > p1
