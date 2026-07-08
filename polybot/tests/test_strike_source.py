"""_compute_strike_and_btc strike source (polybot/main.py).

Regression guard for the decision strike. It prefers Chainlink's get_strike — which
now captures the FIRST btc/usd report AT/AFTER the window boundary, the exact rule
Polymarket's price_to_beat uses (same data stream it resolves on, matched at +0ms), so
it equals the resolved strike. Gamma's event_metadata.price_to_beat is the same value
but served late/unreliably in-window, so it may only BOOTSTRAP the strike until Chainlink
has the boundary, and must never override a Chainlink strike.
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


def _call(chainlink, gamma_ptb, window_strikes=None):
    cid, ts = _cid()
    contract = {"event_metadata": {"price_to_beat": gamma_ptb}} if gamma_ptb is not None else {}
    ws = window_strikes if window_strikes is not None else {}
    strike, btc, ws_out, _, _ = _compute_strike_and_btc(
        cid, _Binance(), ws, eval_window=ts, last_eval_log_window=-1,
        chainlink_feed=chainlink, coinbase_feed=None, contract=contract)
    return ws_out.get(ts), ts


def test_chainlink_preferred_over_gamma():
    # Both present — Chainlink (first-at/after-boundary) is the resolution strike, wins.
    strike, _ = _call(_Chainlink(63405.83), gamma_ptb=63446.18)
    assert strike == 63405.83


def test_gamma_bootstraps_when_chainlink_not_ready():
    strike, _ = _call(_Chainlink(None), gamma_ptb=62842.20)
    assert strike == 62842.20


def test_gamma_never_overrides_a_chainlink_strike():
    # Seeded with the Chainlink strike; a later tick offers a different Gamma value
    # while Chainlink still reports its strike — Chainlink holds.
    _, ts = _cid()
    seeded = {ts: 63405.83}
    strike, _ = _call(_Chainlink(63405.83), gamma_ptb=63446.18, window_strikes=seeded)
    assert strike == 63405.83


def test_chainlink_settles_over_a_gamma_bootstrap():
    # First tick: Chainlink cold -> Gamma bootstrap. Second tick: Chainlink boundary
    # report has landed -> the cache settles to the reliable Chainlink strike.
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
