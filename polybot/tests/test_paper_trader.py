import pytest
import pytest_asyncio
from polybot.execution.base import entry_fee_shares, exit_fee_usdc
from polybot.execution.paper_trader import PaperTrader
from polybot.db.models import Database


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def trader(db):
    # Tests assert deterministic fill behavior — disable the realism randomness
    # (latency + network fail sim) that only matters in live paper runs.
    return PaperTrader(
        db=db,
        max_bankroll_deployed=0.80,
        max_concurrent_positions=5,
        paper_latency_scale=0.0,
        paper_latency_floor_s=0.0,
        paper_network_fail_rate=0.0,
    )


@pytest.mark.asyncio
async def test_open_trade_returns_success(trader):
    result = await trader.open_trade(
        market_id="market_123", question="Will X happen?", side="YES",
        price=0.55, size=10.0, signal_score=0.72,
    )
    assert result.success is True
    assert result.position_id is not None


@pytest.mark.asyncio
async def test_open_trade_reduces_bankroll(trader, db):
    await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72,
    )
    bankroll = await db.get_bankroll()
    # Entry fee collected in shares (not USDC) — bankroll only decreases by size
    expected = 100.0 - 10.0
    assert bankroll == pytest.approx(expected, abs=0.01)


@pytest.mark.asyncio
async def test_rejects_duplicate_market(trader):
    await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72,
    )
    result = await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72,
    )
    assert result.success is False
    assert "duplicate" in result.reason.lower()


@pytest.mark.asyncio
async def test_rejects_when_max_positions_reached(trader, db):
    for i in range(5):
        await trader.open_trade(
            market_id=f"market_{i}", question="Q?", side="YES", price=0.55,
            size=5.0, signal_score=0.72,
        )
    result = await trader.open_trade(
        market_id="market_6", question="Q?", side="YES", price=0.55,
        size=5.0, signal_score=0.72,
    )
    assert result.success is False
    assert "max positions" in result.reason.lower()


@pytest.mark.asyncio
async def test_rejects_when_bankroll_exceeded(trader, db):
    result = await trader.open_trade(
        market_id="market_big", question="Q?", side="YES", price=0.55,
        size=85.0, signal_score=0.72,
    )
    assert result.success is False
    assert "bankroll" in result.reason.lower()


@pytest.mark.asyncio
async def test_close_trade_updates_bankroll(trader, db):
    result = await trader.open_trade(
        market_id="market_123", question="Q?", side="YES", price=0.55,
        size=10.0, signal_score=0.72,
    )
    close_result = await trader.close_trade(position_id=result.position_id, exit_price=0.68)
    assert close_result.success is True
    bankroll = await db.get_bankroll()
    assert bankroll > 100.0


@pytest.mark.asyncio
async def test_scalp_residual_credited_to_bankroll_not_pnl(trader, db):
    """The fee-headroom shares held back from the FOK credit the bankroll (the
    simulated sweep, mirroring live's on-chain recovery) but stay out of pnl
    (live's recorded pnl excludes the swept residual too)."""
    result = await trader.open_trade(
        market_id="m_dust", question="Q?", side="YES", price=0.50,
        size=50.0, signal_score=0.72, fee_rate=0.072,
    )
    bankroll_after_open = await db.get_bankroll()
    close_result = await trader.close_trade(position_id=result.position_id, exit_price=0.60)

    shares_ordered = 50.0 / 0.50
    shares_held = shares_ordered - entry_fee_shares(shares_ordered, 0.50, 0.072)
    headroom = max(max(0.072 * 0.25, 0.0) + 0.002, 0.005)
    shares_sold = shares_held * (1.0 - headroom)
    residual = shares_held - shares_sold
    revenue = shares_sold * 0.60 - exit_fee_usdc(shares_sold, 0.60, 0.072)
    sweep = residual * 0.60 - exit_fee_usdc(residual, 0.60, 0.072)

    bankroll = await db.get_bankroll()
    assert bankroll == pytest.approx(bankroll_after_open + revenue + sweep, abs=0.01)
    assert close_result.pnl == pytest.approx(revenue - 50.0, abs=0.01)


# --- Fee accounting through PaperTrader (module-level fee math lives in test_base_trader) ---

@pytest.mark.asyncio
async def test_shares_held_stored_correctly(trader, db):
    await trader.open_trade(
        market_id="m_shares", question="Q?", side="YES", price=0.50,
        size=50.0, signal_score=0.72, fee_rate=0.072,
    )
    positions = await db.get_open_positions()
    pos = positions[0]
    shares_ordered = 50.0 / 0.50  # 100 shares
    fee_in_shares = entry_fee_shares(shares_ordered, 0.50, 0.072)
    expected_shares = shares_ordered - fee_in_shares
    assert pos["shares_held"] == pytest.approx(expected_shares, abs=0.01)
    assert pos["fee_rate"] == 0.072


@pytest.mark.asyncio
async def test_pnl_realistic_with_fee_in_shares(trader, db):
    """Win at resolution: shares-based entry fee means fewer shares → less payout."""
    result = await trader.open_trade(
        market_id="m_pnl", question="Q?", side="YES", price=0.50,
        size=50.0, signal_score=0.72, fee_rate=0.072,
    )
    # Bankroll after open = 100 - 50 = 50 (fee is in shares, not USDC)
    assert await db.get_bankroll() == pytest.approx(50.0, abs=0.01)

    # Win at resolution ($1.00 — exit fee is $0 at extremes)
    await trader.resolve_position(result.position_id, 1.0)
    bankroll = await db.get_bankroll()
    # Revenue = shares_held × 1.0 - exit_fee(~$0)
    shares_ordered = 50.0 / 0.50
    fee_sh = entry_fee_shares(shares_ordered, 0.50, 0.072)
    shares_held = shares_ordered - fee_sh
    # Bankroll = 50 + shares_held × 1.0
    assert bankroll == pytest.approx(50.0 + shares_held, abs=0.01)
    # Should be less than 150 because of fee deduction
    assert bankroll < 150.0


@pytest.mark.asyncio
async def test_custom_fee_rate_passed_through(trader, db):
    """Sports markets use 0.03 fee rate."""
    await trader.open_trade(
        market_id="m_sports", question="Q?", side="YES", price=0.50,
        size=10.0, signal_score=0.72, fee_rate=0.03,
    )
    positions = await db.get_open_positions()
    pos = positions[0]
    assert pos["fee_rate"] == 0.03
    # Shares held should be more than with crypto rate
    shares_ordered = 10.0 / 0.50
    fee_sh = entry_fee_shares(shares_ordered, 0.50, 0.03)
    assert pos["shares_held"] == pytest.approx(shares_ordered - fee_sh, abs=0.01)


class _FakeClobWs:
    """Minimal clob_ws stub: returns canned book snapshots, counts get_book calls."""

    def __init__(self, books):
        self._books = books  # list consumed one per get_book call; last repeats
        self.calls = 0

    def get_book(self, token_id):
        book = self._books[min(self.calls, len(self._books) - 1)]
        self.calls += 1
        return book


def _fresh_book(ask_price: float) -> dict:
    import time as _t
    return {"ts": _t.time(), "asks": [{"price": str(ask_price), "size": "1000"}],
            "bids": [{"price": str(ask_price - 0.02), "size": "1000"}]}


@pytest.mark.asyncio
async def test_retry_walk_retries_price_moved_then_fills(trader):
    """A "Price moved" FOK rejection retries (same class live retries on):
    attempt 1 sees an ask above the limit, attempt 2 sees it recoil and fills."""
    trader._PAPER_RETRY_BASE_DELAY = 0.001
    ws = _FakeClobWs([_fresh_book(0.60), _fresh_book(0.55)])
    trader.set_clob_ws(ws)
    result = await trader._retry_walk("tok", side="buy", requested_price=0.55, size_usd=10.0)
    assert result.filled is True
    assert ws.calls == 2


@pytest.mark.asyncio
async def test_retry_walk_exhausts_on_persistent_price_moved(trader):
    """All attempts see the ask above the limit — unfilled after exactly
    _PAPER_MAX_RETRIES walks, with the price-moved reason."""
    trader._PAPER_RETRY_BASE_DELAY = 0.001
    ws = _FakeClobWs([_fresh_book(0.60)])
    trader.set_clob_ws(ws)
    result = await trader._retry_walk("tok", side="buy", requested_price=0.55, size_usd=10.0)
    assert result.filled is False
    assert "price moved" in result.reason.lower()
    assert ws.calls == trader._PAPER_MAX_RETRIES


@pytest.mark.asyncio
async def test_retry_walk_no_retry_on_empty_book(trader):
    """Book-empty rejections don't retry — they won't recover in 30ms."""
    import time as _t
    ws = _FakeClobWs([{"ts": _t.time(), "asks": [], "bids": []}])
    trader.set_clob_ws(ws)
    result = await trader._retry_walk("tok", side="buy", requested_price=0.55, size_usd=10.0)
    assert result.filled is False
    assert ws.calls == 1


@pytest.mark.asyncio
async def test_walk_book_none_book_rejects_without_raising(trader):
    """get_book returning None (token never subscribed) behaves like a stale
    book: clean unfilled rejection, no exception."""
    ws = _FakeClobWs([None])
    trader.set_clob_ws(ws)
    result = trader._walk_book("tok", side="buy", requested_price=0.55, size_usd=10.0)
    assert result.filled is False
    assert "stale" in result.reason


class TestRealismShim:
    def test_latency_drawn_from_live_empirical_distribution(self):
        """Draws must follow the recorded live POST-RTT quantiles (inverse-CDF),
        not a gaussian — the right tail (p75 0.679 / p99 1.65) is where fills
        land on repriced books."""
        t = PaperTrader(db=None)
        import random as _r
        _r.seed(7)
        draws = sorted(t._draw_latency() for _ in range(4000))
        assert draws[0] >= 0.405 and draws[-1] <= 2.222
        med = draws[len(draws)//2]
        p75 = draws[int(len(draws)*0.75)]
        assert abs(med - 0.436) < 0.03
        assert abs(p75 - 0.679) < 0.06

    def test_paper_writes_fill_stats_same_schema_as_live(self, tmp_path, monkeypatch):
        """Kill-rate parity must be a measurement: paper records the identical
        fill_stats schema to its own file so live-vs-paper kill rates compare."""
        import polybot.execution.paper_trader as pt
        import json
        f = tmp_path / "fill_stats_paper.json"
        monkeypatch.setattr(pt, "FILL_STATS_PAPER_PATH", f)
        PaperTrader._record_stats(filled=False, side="buy", reason="price moved")
        PaperTrader._record_stats(filled=True, side="buy")
        PaperTrader._record_stats(filled=False, side="sell", reason="not enough shares (pre-check)")
        s = json.loads(f.read_text())
        assert s["total_attempts"] == 3 and s["total_fills"] == 1
        assert s["buy_attempts"] == 2 and s["buy_fills"] == 1
        assert s["failure_buckets"]["price_moved"] == 1
        assert s["failure_buckets"]["precheck_depth"] == 1
        assert s["fill_rate"] == 0.3333
