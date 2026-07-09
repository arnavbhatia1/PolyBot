"""_compute_strike_and_btc strike source (polybot/main.py).

Regression guard for the decision strike. Gamma's event_metadata.price_to_beat is
the RESOLVED truth (verified bit-exact with the correct Chainlink boundary report),
so when present it wins — it covers RTDS delivery gaps where our first-received
at/after-boundary report is NOT Polymarket's first report (measured ~1-2% of
windows, up to $35+ off). It is served late/unreliably in-window, so Chainlink's
first-at/after-boundary capture (get_strike) carries every window where Gamma is
absent. _strike_trusted marks windows safe for sniper capital: any ptb-sourced
strike, or a Chainlink capture with no delivery hole (strike_reliable).
"""
import time

from polybot.main import _compute_strike_and_btc, _strike_trusted


class _Chainlink:
    def __init__(self, strike, reliable=True):
        self._strike = strike
        self._reliable = reliable

    def get_strike(self, window_ts):
        return self._strike

    def boundary_captured(self, window_ts):
        return self._strike is not None

    def strike_reliable(self, window_ts):
        return self._strike is not None and self._reliable


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


def test_gamma_ptb_wins_when_both_present():
    # ptb is the value the market resolves on — it overrides our own capture
    # (an RTDS delivery gap can lock the wrong first-received report).
    strike, ts = _call(_Chainlink(63405.83), gamma_ptb=63446.18)
    assert strike == 63446.18
    assert _strike_trusted.get(ts) is True


def test_gamma_bootstraps_when_chainlink_not_ready():
    strike, ts = _call(_Chainlink(None), gamma_ptb=62842.20)
    assert strike == 62842.20
    assert _strike_trusted.get(ts) is True


def test_gamma_settles_over_a_seeded_chainlink_strike():
    # Seeded with the Chainlink capture; a later tick serves the resolved ptb
    # that disagrees (delivery-gap window) — ptb wins.
    _, ts = _cid()
    seeded = {ts: 63405.83}
    strike, _ = _call(_Chainlink(63405.83), gamma_ptb=63446.18, window_strikes=seeded)
    assert strike == 63446.18


def test_chainlink_carries_when_gamma_absent():
    strike, ts = _call(_Chainlink(62834.00), gamma_ptb=None)
    assert strike == 62834.00
    assert _strike_trusted.get(ts) is True


def test_chainlink_delivery_hole_is_untrusted():
    # Boundary captured across an RTDS delivery hole: the strike still serves
    # (base path / telemetry) but the window is NOT trusted for sniper capital.
    strike, ts = _call(_Chainlink(62853.77, reliable=False), gamma_ptb=None)
    assert strike == 62853.77
    assert _strike_trusted.get(ts) is False


def test_no_strike_when_neither_source_has_it():
    strike, _ = _call(_Chainlink(None), gamma_ptb=None)
    assert strike is None
