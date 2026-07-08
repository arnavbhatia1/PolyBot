"""_compute_strike_and_btc strike source (polybot/main.py).

Regression guard for the decision strike. The strike is Polymarket's
event_metadata.price_to_beat — the exact value the market RESOLVES on (== the prior
window's close) and the number the UI shows, set at window open and served throughout
the active window. It is refreshed every loop so a not-yet-finalized value at the very
open self-corrects before the final-45s sniper fires. Chainlink's boundary capture is
ONLY a cold-start fallback until price_to_beat is served (its last-tick-before-boundary
value misses the official round by >$8 in a fast open, ~1% of windows flip side), so it
must never be primary or lock in.
"""
import time

from polybot.main import _compute_strike_and_btc


class _Chainlink:
    def __init__(self, strike):
        self._strike = strike

    def get_strike(self, window_ts):
        return self._strike


class _Binance:
    buffer = None  # only read on the no-strike path


def _cid():
    # current 5-min window so the function's 600s prune keeps it
    ts = int(time.time() // 300) * 300
    return f"btc-updown-5m-{ts}", ts


def _call(chainlink, ptb, window_strikes=None):
    cid, ts = _cid()
    contract = {"event_metadata": {"price_to_beat": ptb}} if ptb is not None else {}
    ws = window_strikes if window_strikes is not None else {}
    strike, btc, ws_out, _, _ = _compute_strike_and_btc(
        cid, _Binance(), ws, eval_window=ts, last_eval_log_window=-1,
        chainlink_feed=chainlink, coinbase_feed=None, contract=contract)
    return ws_out.get(ts), ts


def test_price_to_beat_preferred_over_chainlink():
    # price_to_beat is the value Polymarket resolves on — it must win over Chainlink.
    strike, _ = _call(_Chainlink(63405.83), ptb=63390.00)
    assert strike == 63390.00


def test_chainlink_fallback_when_no_price_to_beat():
    # Cold start: price_to_beat not served yet -> Chainlink boundary fallback.
    strike, _ = _call(_Chainlink(63405.83), ptb=None)
    assert strike == 63405.83


def test_price_to_beat_takes_over_when_it_arrives():
    # First tick: no price_to_beat -> Chainlink. Second tick: it's served -> strike switches.
    cid, ts = _cid()
    ws = {}
    _compute_strike_and_btc(cid, _Binance(), ws, ts, -1,
                            chainlink_feed=_Chainlink(63405.83), coinbase_feed=None, contract={})
    assert ws[ts] == 63405.83
    _compute_strike_and_btc(cid, _Binance(), ws, ts, -1,
                            chainlink_feed=_Chainlink(63405.83), coinbase_feed=None,
                            contract={"event_metadata": {"price_to_beat": 63390.00}})
    assert ws[ts] == 63390.00


def test_price_to_beat_refreshes_each_loop():
    # A stale/early value self-corrects: the strike follows the latest price_to_beat,
    # never locking the value seen at window open.
    cid, ts = _cid()
    ws = {ts: 63000.00}   # stale seed
    _compute_strike_and_btc(cid, _Binance(), ws, ts, -1,
                            chainlink_feed=_Chainlink(None), coinbase_feed=None,
                            contract={"event_metadata": {"price_to_beat": 63390.00}})
    assert ws[ts] == 63390.00


def test_no_strike_when_neither_source_has_it():
    strike, _ = _call(_Chainlink(None), ptb=None)
    assert strike is None
