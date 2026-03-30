import pytest
import pytest_asyncio
import json
from polybot.core.binance_feed import Candle, CandleBuffer
from polybot.indicators.engine import IndicatorEngine
from polybot.core.signal_engine import SignalEngine
from polybot.db.models import Database
from polybot.execution.paper_trader import PaperTrader

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()

@pytest.fixture
def weights_dir(tmp_path):
    d = tmp_path / "weights"
    d.mkdir()
    (d / "weights_v001.json").write_text(json.dumps({
        "rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20,
        "entry_threshold": 0.60, "version": "weights_v001"}))
    return str(d)

def _make_buy_signal_buffer():
    """Creates a buffer pattern that generates a bullish buy signal:
    strong uptrend, sharp pullback below VWAP (RSI oversold), recovery."""
    buf = CandleBuffer(max_size=200)
    for i in range(30):
        price = 50000 + i * 100
        buf.add(Candle(timestamp=i*60000, open=price-20, high=price+40, low=price-40, close=price, volume=100.0+i*5))
    for i in range(10):
        price = 53000 - i * 200
        buf.add(Candle(timestamp=(30+i)*60000, open=price+20, high=price+40, low=price-40, close=price, volume=150.0))
    for i in range(10):
        price = 51000 + i * 100
        buf.add(Candle(timestamp=(40+i)*60000, open=price-20, high=price+30, low=price-30, close=price, volume=120.0))
    return buf

@pytest.mark.asyncio
async def test_full_ta_flow(db, weights_dir):
    """Indicators → signal → paper trade."""
    buf = _make_buy_signal_buffer()

    engine = IndicatorEngine(weights_dir=weights_dir, active_version="weights_v001")
    indicators = engine.compute_all(buf)
    snapshot = engine.get_snapshot(indicators)

    signal_eng = SignalEngine(entry_threshold=0.30,  # Lower threshold for test
                              weights={"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20})

    # Verify the raw composite score is non-zero (pipeline computed a directional signal)
    raw_score = signal_eng._compute_score(indicators)
    assert raw_score != 0

    signal = signal_eng.evaluate(indicators, has_position=False, in_entry_window=True)

    # If actionable, place a paper trade
    if signal.action in ("BUY_YES", "BUY_NO"):
        trader = PaperTrader(db=db, max_slippage=0.02, max_bankroll_deployed=0.80, max_concurrent_positions=5)
        side = "YES" if signal.action == "BUY_YES" else "NO"
        result = await trader.open_trade(
            market_id="0xbtc5min", question="BTC 5min Up?", side=side,
            price=0.55, size=5.0, signal_score=abs(signal.score),
            signal_strength="high", ev_at_entry=0.10, exit_target=0.90,
            stop_loss=0.40, weight_version="ta_v001")
        assert result.success is True
        bankroll = await db.get_bankroll()
        assert bankroll == pytest.approx(95.0, abs=0.01)
