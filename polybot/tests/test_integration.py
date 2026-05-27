# polybot/tests/test_integration.py
import pytest
import pytest_asyncio
from polybot.db.models import Database
from polybot.core.signal_engine import SignalEngine
from polybot.execution.paper_trader import PaperTrader

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(1000.0)
    yield database
    await database.close()

def _make_indicators(atr_value=30.0):
    return {
        "atr": {"atr": atr_value, "passes": True, "reason": "ok"},
        "ema": {"trend": "bullish", "fast_ema": 100.0, "slow_ema": 99.0},
        "rsi": {"rsi": 50.0, "score": 0.3},
        "macd": {"macd": 0.0, "signal": 0.0, "histogram": 0.0, "score": 0.2},
        "stochastic": {"k": 50.0, "d": 50.0, "score": 0.1},
        "obv": {"obv_slope": 0, "price_slope": 0, "score": 0.0},
        "vwap": {"vwap": 100.0, "deviation": 0, "score": 0.0},
    }

@pytest.mark.asyncio
async def test_full_trade_flow(db):
    """End-to-end: signal engine finds edge -> paper trade placed -> close at profit."""
    engine = SignalEngine(min_edge=0.10, kelly_fraction=0.15, momentum_weight=0.08, atr_sigma_ratio=1.4)

    # BTC $100 above strike with 3 min left, market at 55% — model finds edge
    signal = engine.evaluate(
        _make_indicators(atr_value=30), has_position=False, in_entry_window=True,
        btc_price=66500, strike_price=66400,
        seconds_remaining=180, market_price_up=0.55, market_price_down=0.45)
    assert signal.action == "BUY_YES"
    assert signal.edge >= 0.10

    # Paper trade
    trader = PaperTrader(db=db, max_slippage=0.02, max_bankroll_deployed=0.80,
                         paper_network_fail_rate=0.0)
    size = round(1000.0 * signal.kelly_size, 2)
    size = max(size, 1.0)
    result = await trader.open_trade(
        market_id="0xabc", question="BTC 5min Up?", side="Up",
        price=0.55, size=size, signal_score=signal.prob)
    assert result.success is True

    # Evaluate hold: model still confident, market at 70% — hold
    action, prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=30), btc_price=66600, strike_price=66400,
        seconds_remaining=60, market_price_for_side=0.70, side="Up")
    assert action == "HOLD"

    # Close at resolution (win)
    close_result = await trader.close_trade(result.position_id, exit_price=1.0)
    assert close_result.success is True
    assert close_result.log_return > 0

    # Bankroll grew
    bankroll = await db.get_bankroll()
    assert bankroll > 1000.0

@pytest.mark.asyncio
async def test_scalp_exit_flow(db):
    """Signal engine finds edge, enters, conditions flip, exits early with profit."""
    engine = SignalEngine(min_edge=0.10, kelly_fraction=0.15, momentum_weight=0.08, atr_sigma_ratio=1.4)

    # Enter: BTC above strike, model sees edge
    signal = engine.evaluate(
        _make_indicators(atr_value=30), has_position=False, in_entry_window=True,
        btc_price=66500, strike_price=66400,
        seconds_remaining=180, market_price_up=0.55, market_price_down=0.45)
    assert signal.action == "BUY_YES"

    trader = PaperTrader(db=db, max_slippage=0.02, max_bankroll_deployed=0.80,
                         paper_network_fail_rate=0.0)
    result = await trader.open_trade(
        market_id="0xdef", question="BTC 5min Up?", side="Up",
        price=0.55, size=50.0, signal_score=signal.prob)
    assert result.success is True

    # Market drift inside the scalp zone: edge in (-0.10, deep_loss_floor] → EXIT.
    # BTC barely above strike with high ATR → model ~60%, market 70% → edge ~-0.10.
    # Confirms the legitimate scalp window still triggers AFTER the ITM patience
    # scaling (-0.10 × (1 + 0.5 × itm_depth)) — at market=0.70 the floor is
    # ~-0.12 but the optimal_threshold blend gives effective ≈ -0.085, so
    # edge = -0.10 cleanly clears the trigger without hitting deep-loss-hold
    # (which requires edge strictly less than -0.10).
    action, _, _, _ = engine.evaluate_hold(
        _make_indicators(atr_value=80), btc_price=66420, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.70, side="Up")
    assert action == "EXIT"

    # Close at current market price (still profitable vs 0.55 entry)
    close_result = await trader.close_trade(result.position_id, exit_price=0.60)
    assert close_result.success is True
    assert close_result.log_return > 0  # 0.60 > 0.55 entry
