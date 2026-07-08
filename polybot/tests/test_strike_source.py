"""_compute_strike_and_btc strike source (polybot/main.py).

Regression guard for the strike source. Priority: (1) the PREVIOUS window's
resolved close — windows chain so price_to_beat[N] == final_price[N-1] exactly,
which is what Polymarket resolves on; (2) Chainlink's boundary-captured strike as
a fallback until the prior window resolves (its last-tick-before-boundary capture
misses the official round by >$8 in a fast open); (3) Gamma's live
event_metadata.price_to_beat as a last-resort cold-start bootstrap (its intra-window
value can lag/drift tens of dollars). The prev-close self-heals over any fallback the
moment it lands, and Gamma must never override a Chainlink strike.
"""
import time

from polybot.main import _compute_strike_and_btc


class _Chainlink:
    def __init__(self, strike):
        self._strike = strike

    def get_strike(self, window_ts):
        return self._strike


class _Recorder:
    """Fake WindowPathRecorder exposing only window_final()."""
    def __init__(self, prev_final):
        self._pf = prev_final

    def window_final(self, window_ts):
        return self._pf


class _Binance:
    buffer = None  # only read on the no-strike path


def _cid():
    # current 5-min window so the function's 600s prune keeps it
    ts = int(time.time() // 300) * 300
    return f"btc-updown-5m-{ts}", ts


def _call(chainlink, gamma_ptb, window_strikes=None):
    cid, ts = _cid()
    contract = {"event_metadata": {"price_to_beat": gamma_ptb}} if gamma_ptb is not None else {}
    ws = window_strikes if window_strikes is not None else {}
    strike, btc, ws_out, _, _ = _compute_strike_and_btc(
        cid, _Binance(), ws, eval_window=ts, last_eval_log_window=-1,
        chainlink_feed=chainlink, coinbase_feed=None, contract=contract)
    return ws_out.get(ts), ts


def test_chainlink_preferred_over_gamma():
    # Both present, Gamma drifted +$40 — Chainlink must win.
    strike, _ = _call(_Chainlink(63405.83), gamma_ptb=63446.18)
    assert strike == 63405.83


def test_gamma_bootstraps_when_chainlink_not_ready():
    strike, _ = _call(_Chainlink(None), gamma_ptb=62842.20)
    assert strike == 62842.20


def test_gamma_never_overrides_a_chainlink_strike():
    # Seed the cache with the Chainlink strike, then a later tick offers a
    # different Gamma value while Chainlink still reports its strike.
    _, ts = _cid()
    seeded = {ts: 63405.83}
    strike, _ = _call(_Chainlink(63405.83), gamma_ptb=63446.18, window_strikes=seeded)
    assert strike == 63405.83


def test_chainlink_upgrades_a_gamma_bootstrap():
    # First tick: Chainlink cold -> Gamma bootstrap. Second tick: Chainlink warm
    # -> the cache upgrades to the reliable Chainlink strike (self-heal).
    cid, ts = _cid()
    ws = {}
    _compute_strike_and_btc(cid, _Binance(), ws, ts, -1,
                            chainlink_feed=_Chainlink(None), coinbase_feed=None,
                            contract={"event_metadata": {"price_to_beat": 62842.20}})
    assert ws[ts] == 62842.20
    _compute_strike_and_btc(cid, _Binance(), ws, ts, -1,
                            chainlink_feed=_Chainlink(62834.00), coinbase_feed=None,
                            contract={"event_metadata": {"price_to_beat": 62842.20}})
    assert ws[ts] == 62834.00


def test_no_strike_when_neither_source_has_it():
    strike, _ = _call(_Chainlink(None), gamma_ptb=None)
    assert strike is None


def test_prev_close_preferred_over_chainlink():
    # The prior window's resolved close IS this window's price_to_beat — it must
    # win over the Chainlink boundary capture (which can miss the official round).
    cid, ts = _cid()
    ws = {}
    _compute_strike_and_btc(cid, _Binance(), ws, ts, -1,
                            chainlink_feed=_Chainlink(63405.83), coinbase_feed=None,
                            contract={}, window_recorder=_Recorder(63390.00))
    assert ws[ts] == 63390.00


def test_prev_close_upgrades_a_chainlink_strike():
    # Cold start: prior window not labeled yet -> Chainlink fallback. Once the prior
    # window resolves, the strike self-heals to the exact price_to_beat.
    cid, ts = _cid()
    ws = {}
    _compute_strike_and_btc(cid, _Binance(), ws, ts, -1,
                            chainlink_feed=_Chainlink(63405.83), coinbase_feed=None,
                            contract={}, window_recorder=_Recorder(None))
    assert ws[ts] == 63405.83
    _compute_strike_and_btc(cid, _Binance(), ws, ts, -1,
                            chainlink_feed=_Chainlink(63405.83), coinbase_feed=None,
                            contract={}, window_recorder=_Recorder(63390.00))
    assert ws[ts] == 63390.00


def test_chainlink_fallback_when_prev_close_absent():
    # No recorder / prior window unresolved -> existing Chainlink-first behavior holds.
    cid, ts = _cid()
    ws = {}
    _compute_strike_and_btc(cid, _Binance(), ws, ts, -1,
                            chainlink_feed=_Chainlink(63405.83), coinbase_feed=None,
                            contract={"event_metadata": {"price_to_beat": 63446.18}},
                            window_recorder=_Recorder(None))
    assert ws[ts] == 63405.83
