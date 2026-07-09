"""Cross-module mirror invariants — documented pairs that must not drift.

Each assertion corresponds to a code comment claiming two values match
("matches live", "= min_edge", "defaults equal settings"). A failure here means
one side of a mirror was edited alone: sync both sides, then update this test
deliberately.
"""
from pathlib import Path

import yaml

from polybot.execution import live_trader
from polybot.execution.base import (DEFAULT_FEE_RATE, EFFECTIVE_FEE_PEAK,
                                    compute_buy_vwap)
from polybot.execution.live_trader import LiveTrader
from polybot.execution.paper_trader import PaperTrader

_SETTINGS = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "config" / "settings.yaml")
    .read_text(encoding="utf-8"))


def test_paper_retry_constants_mirror_live():
    assert PaperTrader._PAPER_MAX_RETRIES == live_trader._MAX_RETRIES
    assert PaperTrader._PAPER_RETRY_BASE_DELAY == live_trader._RETRY_BASE_DELAY


def test_sniper_min_edge_equals_min_edge():
    # settings.yaml documents sniper_min_edge = signal.min_edge so the
    # downstream net-edge / pre-submit gates don't silently raise the floor.
    assert (_SETTINGS["late_window"]["sniper_min_edge"]
            == _SETTINGS["signal"]["min_edge"])


def test_effective_fee_peak_is_quarter_of_rate():
    # EFFECTIVE_FEE_PEAK is the p(1-p) fee at p=0.5: rate * 0.25.
    assert EFFECTIVE_FEE_PEAK == round(DEFAULT_FEE_RATE * 0.25, 5)


def test_paper_realism_defaults_track_settings():
    # PaperTrader's kwarg defaults are documented as equal to settings.yaml's
    # calibrated values (they fire only when settings omit the keys).
    # Re-calibrating settings means updating paper_trader.__init__ and the
    # main.py fallbacks in the same commit.
    t = PaperTrader(db=None)
    exec_cfg = _SETTINGS["execution"]
    assert t.latency_scale == exec_cfg["paper_latency_scale"]
    assert t.latency_floor_s == exec_cfg["paper_latency_floor_s"]
    assert t.network_fail_rate == exec_cfg["paper_network_fail_rate"]


def test_buy_walk_gate_agrees_with_live_precheck():
    # base.compute_buy_vwap (the pre-submit gate) and LiveTrader._estimate_fok_walk
    # (the order-time pre-check) are documented as the same walk math — the gate
    # and the actual FOK must see the same book the same way.
    from py_clob_client_v2.order_builder.constants import BUY

    book = {"asks": [{"price": "0.50", "size": "100"},
                     {"price": "0.60", "size": "100"}]}
    size_usd = 80.0  # walks into the second level
    vwap = compute_buy_vwap(book, size_usd)
    assert vwap is not None and 0.50 < vwap < 0.60
    assert LiveTrader._estimate_fok_walk(book, BUY, size_usd, vwap + 1e-6) is True
    assert LiveTrader._estimate_fok_walk(book, BUY, size_usd, vwap - 1e-3) is False
