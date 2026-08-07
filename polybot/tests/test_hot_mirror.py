"""The DB hot-read mirror must equal the SQL it replaces, at every transition —
the fire path trusts these sync peeks instead of awaiting aiosqlite."""
import pytest

from polybot.db.models import Database
from polybot.main import _pregate_should_eval


def _kwargs(mid="btc-updown-5m-1785700000", size=2.5):
    return dict(market_id=mid, question="q", side="Up", entry_price=0.8,
                size=size, signal_score=0.9, indicator_snapshot="{}",
                fee_rate=0.07, shares_held=size / 0.8)


async def _assert_parity(db, mid):
    peek = db.preflight_peek(mid)
    sql = await db.get_open_trade_preflight(mid)
    assert peek is not None
    assert peek[0] == sql[0]
    assert peek[1] == sql[1]
    assert peek[2] == pytest.approx(sql[2])
    assert peek[3] == pytest.approx(sql[3])
    assert db.has_open_or_pending_market(mid) == await db.has_position_for_market(mid)


@pytest.mark.asyncio
async def test_mirror_matches_sql_through_every_transition(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.initialize()
    await db.set_bankroll(100.0)
    mid = "btc-updown-5m-1785700000"
    await _assert_parity(db, mid)                      # empty

    pid = await db.open_position_and_debit_bankroll(new_bankroll=97.5, **_kwargs(mid))
    await _assert_parity(db, mid)                      # open
    assert db.preflight_peek(mid)[0] is True and db.preflight_peek(mid)[1] == 1

    pid2 = await db.open_position_and_debit_bankroll(
        new_bankroll=94.0, **_kwargs("btc-updown-5m-1785700300", size=3.5))
    await _assert_parity(mid=mid, db=db)
    assert db.preflight_peek(mid)[3] == pytest.approx(6.0)   # deployed = 2.5 + 3.5

    await db.mark_pending_resolution(pid)
    await _assert_parity(db, mid)                      # pending still blocks the market
    assert db.preflight_peek(mid)[0] is True and db.preflight_peek(mid)[1] == 1

    await db.close_position(pid, exit_price=1.0, pnl=0.5, bankroll_delta=3.125)
    await _assert_parity(db, mid)                      # closed -> gone
    assert db.preflight_peek(mid)[0] is False

    await db.close_position(pid2, exit_price=0.0, pnl=-3.5, new_bankroll=93.6)
    await _assert_parity(db, mid)
    assert db.preflight_peek(mid) == (False, 0, pytest.approx(93.6), pytest.approx(0.0))
    await db.close()


@pytest.mark.asyncio
async def test_mirror_rebuilds_on_reconnect(tmp_path):
    path = str(tmp_path / "m.db")
    db = Database(path)
    await db.initialize()
    await db.set_bankroll(50.0)
    await db.open_position_and_debit_bankroll(new_bankroll=48.0, **_kwargs())
    await db.close()

    db2 = Database(path)
    await db2.initialize()                             # restart: mirror rebuilt from disk
    await _assert_parity(db2, "btc-updown-5m-1785700000")
    assert db2.preflight_peek("btc-updown-5m-1785700000")[0] is True
    await db2.close()


def test_pregate_never_throttles_a_fire_adjacent_wake():
    # A hot wake (near-locked displacement inside the zone) ALWAYS evaluates,
    # even 1ms after the previous eval — no dip can be missed.
    assert _pregate_should_eval(now=100.0, last_eval_ts=99.999, sec_rem=20.0,
                                hot=True, zone_s=30.0)


def test_pregate_throttles_cold_in_zone_ticks():
    common = dict(sec_rem=20.0, hot=False, zone_s=30.0)
    assert not _pregate_should_eval(now=100.0, last_eval_ts=99.9, **common)   # 100ms ago
    assert _pregate_should_eval(now=100.3, last_eval_ts=100.0, **common)      # 300ms ago


def test_pregate_one_hz_outside_zone():
    common = dict(sec_rem=200.0, hot=False, zone_s=30.0)
    assert not _pregate_should_eval(now=100.0, last_eval_ts=99.5, **common)
    assert _pregate_should_eval(now=100.6, last_eval_ts=99.5, **common)


@pytest.mark.asyncio
async def test_open_or_pending_count_tracks_transitions(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    await db.initialize()
    await db.set_bankroll(50.0)
    assert db.open_or_pending_count() == 0
    pid = await db.open_position_and_debit_bankroll(new_bankroll=47.5, **_kwargs())
    assert db.open_or_pending_count() == 1
    await db.mark_pending_resolution(pid)
    assert db.open_or_pending_count() == 1      # pending still needs management
    await db.close_position(pid, exit_price=1.0, pnl=0.5, bankroll_delta=3.1)
    assert db.open_or_pending_count() == 0
    await db.close()


def test_twap_hot_cold_inputs_never_crash():
    """The µs hot check must read every cold input (no feed, no strike, no
    projection, out-of-zone) as NOT hot — throttle, never crash the loop
    (the abs(None) lesson: a mid-reconnect feed spammed one error per tick)."""
    from polybot.main import _twap_hot

    class F:
        def __init__(self, v):
            self.v = v

        def projected_final_twap(self, close_ts, now=None):
            return self.v

    w_ts = (int(1786060801) // 300) * 300
    in_zone = w_ts + 280.0          # 20s remaining
    strikes = {w_ts: 64000.0}
    assert _twap_hot(None, strikes, in_zone, 30.0) is False
    assert _twap_hot(F(64100.0), {}, in_zone, 30.0) is False          # no strike
    assert _twap_hot(F(None), strikes, in_zone, 30.0) is False        # cold projection
    assert _twap_hot(F(64100.0), strikes, w_ts + 100.0, 30.0) is False  # outside zone


def test_twap_hot_fires_at_ninety_pct_of_margin():
    """Hot at ≥90% of the p99.5 margin — a borderline lock must never be
    throttled past its dip."""
    from polybot.core.signal_engine import TWAP_MARGIN_P995, twap_margin
    from polybot.main import _twap_hot

    class F:
        def __init__(self, v):
            self.v = v

        def projected_final_twap(self, close_ts, now=None):
            return self.v

    w_ts = (int(1786060801) // 300) * 300
    now = w_ts + 280.0              # 20s remaining
    m = twap_margin(TWAP_MARGIN_P995, 20.0)
    strikes = {w_ts: 64000.0}
    assert _twap_hot(F(64000.0 + 0.95 * m), strikes, now, 30.0) is True
    assert _twap_hot(F(64000.0 + 0.5 * m), strikes, now, 30.0) is False
