"""Phase 1 passive-exit units: the conservative fill rule + the maker close path."""
import pytest

from polybot.main import _resting_fill_price
from polybot.db.models import Database
from polybot.execution.paper_trader import PaperTrader


# --- conservative prints-through fill rule ---

def _print(ts, price, side="BUY"):
    return {"timestamp": ts, "price": str(price), "side": side}


def test_fill_requires_strictly_through():
    prints = [_print(101, 0.56), _print(102, 0.57)]
    assert _resting_fill_price(prints, level=0.56, posted_ts=100) == 0.56  # 0.57 > level
    assert _resting_fill_price([_print(101, 0.56)], level=0.56, posted_ts=100) is None  # at-level


def test_fill_ignores_prints_before_posting_and_sells():
    prints = [_print(99, 0.60), _print(101, 0.60, side="SELL")]
    assert _resting_fill_price(prints, level=0.56, posted_ts=100) is None


def test_fill_returns_level_not_print_price():
    assert _resting_fill_price([_print(101, 0.99)], level=0.56, posted_ts=100) == 0.56


# --- maker close: zero exit fee, fill at the resting level ---

async def _open_test_position(db, market_id: str) -> int:
    return await db.open_position_and_debit_bankroll(
        940.0, market_id=market_id, question="q", side="Up", entry_price=0.60,
        size=60.0, signal_score=0.6, indicator_snapshot="{}",
        fee_rate=0.07, shares_held=98.0)


@pytest.mark.asyncio
async def test_maker_fill_close_has_zero_exit_fee(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        await db.set_bankroll(1000.0)
        trader = PaperTrader(db=db, max_bankroll_deployed=0.8, max_concurrent_positions=2,
                             paper_network_fail_rate=0.0, paper_latency_mean_s=0.0,
                             paper_latency_jitter_s=0.0)
        assert trader.supports_passive_exit is True
        pid = await _open_test_position(db, "btc-updown-5m-1")
        pos = next(p for p in await db.get_open_positions() if p["id"] == pid)
        result = await trader.close_trade(pid, 0.66, token_id="tok", position=pos,
                                          maker_fill=True)
        assert result.success
        assert result.fill_price == 0.66          # the resting level, no book walk
        assert result.exit_fee_usd == 0.0         # maker pays no taker fee
        assert result.pnl > 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_taker_close_still_charges_exit_fee(tmp_path):
    db = Database(str(tmp_path / "t2.db"))
    await db.initialize()
    try:
        await db.set_bankroll(1000.0)
        trader = PaperTrader(db=db, max_bankroll_deployed=0.8, max_concurrent_positions=2,
                             paper_network_fail_rate=0.0, paper_latency_mean_s=0.0,
                             paper_latency_jitter_s=0.0)
        pid = await _open_test_position(db, "btc-updown-5m-2")
        pos = next(p for p in await db.get_open_positions() if p["id"] == pid)
        result = await trader.close_trade(pid, 0.66, token_id="tok", position=pos)
        assert result.success
        assert result.exit_fee_usd > 0.0
    finally:
        await db.close()
