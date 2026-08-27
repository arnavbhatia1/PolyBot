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

@pytest.mark.asyncio
async def test_full_trade_flow(db):
    """End-to-end: lock leg fires -> paper trade placed -> resolves at $1."""
    engine = SignalEngine(min_edge=0.04, kelly_fraction=0.15)

    # Locked window (disp one dollar past the MAX knot at k=4) with the winner ask dipped.
    from polybot.core.signal_engine import TWAP_MARGIN_MAX, twap_margin
    signal = engine.evaluate_twap_lock(
        66400.0 + twap_margin(TWAP_MARGIN_MAX, 4.0) + 1.0, 66400.0, 4.0, market_ask_up=0.90, market_ask_down=0.11,
        zone_s=30.0, k_min_s=0.8, sniper_min_edge=0.04)
    assert signal.action == "LATE_SNIPE_YES"
    assert signal.edge >= 0.04

    trader = PaperTrader(db=db, max_bankroll_deployed=0.80,
                         paper_network_fail_rate=0.0)
    size = max(round(1000.0 * signal.kelly_size, 2), 1.0)
    result = await trader.open_trade(
        market_id="0xabc", question="BTC 5min Up?", side="Up",
        price=0.90, size=size, signal_score=signal.prob)
    assert result.success is True

    # Hold to resolution (no exit engine) -> win pays $1.
    close_result = await trader.resolve_position(result.position_id, 1.0)
    assert close_result.success is True

    bankroll = await db.get_bankroll()
    assert bankroll > 1000.0
