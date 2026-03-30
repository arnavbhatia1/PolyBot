# polybot/tests/test_integration.py
import pytest
import pytest_asyncio
from polybot.db.models import Database
from polybot.math_engine.decision_table import DecisionTable
from polybot.core.filters import MarketFilter
from polybot.execution.paper_trader import PaperTrader
from polybot.brain.claude_client import MarketAnalysis

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()

@pytest.mark.asyncio
async def test_full_trade_flow(db):
    """End-to-end: market passes filter -> Claude analyzes -> math decides -> paper trade placed."""
    # 1. Filter
    f = MarketFilter(min_volume_24h=1000, min_liquidity=500, min_days_to_expiry=2,
        max_days_to_expiry=60, max_spread=0.05, category_whitelist=[], category_blacklist=[])
    market = {"condition_id": "0xabc", "question": "Will BTC hit 100k?", "price_yes": 0.55,
              "volume_24h": 5000, "liquidity": 2000, "spread": 0.02, "days_to_expiry": 15, "category": "crypto"}
    assert f.passes(market) is True

    # 2. Claude analysis (mocked)
    analysis = MarketAnalysis(probability=0.72, confidence="high", reasoning="Strong signals",
                              key_factors=["momentum"], base_rate_considered=True)
    assert analysis.passes_gate(min_confidence="high", min_probability=0.65) is True

    # 3. Decision table
    table = DecisionTable(ev_threshold=0.05, kelly_fraction=0.25, entry_discount=0.85, exit_target=0.90, stop_loss_pct=0.15)
    table.build()
    assert table.should_buy(probability=0.72, market_price=0.55) is True
    size = table.position_size(probability=0.72, market_price=0.55, bankroll=100.0)
    decision = table.lookup(0.72)

    # 4. Paper trade
    trader = PaperTrader(db=db, max_slippage=0.02, max_bankroll_deployed=0.80, max_concurrent_positions=5)
    result = await trader.open_trade(market_id="0xabc", question="Will BTC hit 100k?", side="YES",
        price=0.55, size=size, claude_probability=0.72, claude_confidence="high",
        ev_at_entry=table.calculate_ev(0.72, 0.55), exit_target=decision["exit_price"],
        stop_loss=0.55 * (1 - 0.15), prompt_version="v001")
    assert result.success is True

    # 5. Close at profit
    close_result = await trader.close_trade(result.position_id, exit_price=decision["exit_price"])
    assert close_result.success is True
    assert close_result.log_return > 0

    # 6. Verify bankroll grew
    bankroll = await db.get_bankroll()
    assert bankroll > 100.0
