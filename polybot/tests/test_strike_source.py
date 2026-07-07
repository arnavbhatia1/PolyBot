"""_compute_strike_and_btc strike source (polybot/main.py).

Regression guard for the live-strike bug: the decision strike must prefer
Chainlink (the resolution venue, whose boundary-captured strike matches the
resolved price_to_beat to ~$1 and is what the recorder uses). Gamma's mid-window
event_metadata.price_to_beat can lag/drift by tens of dollars before it settles;
trusting it flipped near-strike sniper crossings onto the wrong (cheap) side.
Gamma may only BOOTSTRAP the strike until Chainlink has it, and must never
override a Chainlink strike.
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
