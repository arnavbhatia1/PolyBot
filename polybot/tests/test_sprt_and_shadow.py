"""SPRT machinery + regime-Kelly shadow stamps + the single settled OPEN banner
(the 07-24 commit: alert-only analytics + log-only banner redesign)."""
import importlib.util
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from polybot.core.sprt import run_sprt, format_status
from polybot.main import (
    _log_open_banner, _on_entry_settled,
    _pending_settled_banners, _lru_set,
)

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_harness():
    hp = ROOT / "scripts" / "analyze_late_window.py"
    spec = importlib.util.spec_from_file_location("analyze_late_window_t", hp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── SPRT core ─────────────────────────────────────────────────────────────────

def test_sprt_retro_sanity_validation_passes_day_3():
    """Pre-registered retro check: the six validation day-means accept H1
    on day 3 at the frozen constants."""
    r = run_sprt([2.9, 8.6, 21.1, 12.6, 1.9, 9.3], mu1=6.0, sigma=7.0)
    assert r.state == "accept_h1"
    assert r.n_days == 3


def test_sprt_rejects_dead_edge():
    r = run_sprt([-6.0, -6.0, -6.0], mu1=6.0, sigma=7.0)
    assert r.state == "accept_h0"
    assert r.n_days == 3


def test_sprt_no_decision_before_min_days():
    # Λ is already past the accept boundary at day 2, but one-day flukes must
    # not decide — min 3 days.
    r = run_sprt([30.0, 30.0], mu1=6.0, sigma=7.0)
    assert r.state == "continue"


def test_sprt_truncates_at_16_days():
    # x = μ₁/2 makes each day's increment exactly 0 — no boundary is ever hit.
    r = run_sprt([3.0] * 20, mu1=6.0, sigma=7.0)
    assert r.state == "truncated"
    assert r.n_days == 16


def test_sprt_void_on_sigma_blowup_or_unset():
    assert run_sprt([30.0, -30.0, 25.0, -25.0], mu1=6.0, sigma=7.0).state == "void"
    assert run_sprt([1.0, 2.0], mu1=6.0, sigma=0.0).state == "void"
    assert "SPRT[x]" in format_status("x", run_sprt([], mu1=6.0, sigma=7.0))


def test_sprt_decided_test_cannot_be_retro_voided():
    # Stop-on-boundary: observations after the stopping point were never
    # "under" the test — appending a volatile stretch to a decided test's
    # day list must replay the same decision, never flip it to void.
    decided = run_sprt([30.0, 30.0, 30.0], mu1=6.0, sigma=7.0)
    assert decided.state == "accept_h1" and decided.n_days == 3
    replay = run_sprt([30.0, 30.0, 30.0, 90.0, -90.0, 80.0], mu1=6.0, sigma=7.0)
    assert replay.state == "accept_h1" and replay.n_days == 3
    assert replay.lam == decided.lam


# ── Single settled OPEN banner ────────────────────────────────────────────────

_CTX = dict(side="Up", size=1.61, cid="btc-updown-5m-1776691500", phase="late_sniper",
            signal_ask=0.80, posted=0.81, strike=117_950.0,
            prob=0.94, edge=0.14, fee_rate=0.07, bankroll=135.0)


def test_paper_banner_prints_charged_fee(caplog):
    with caplog.at_level(logging.INFO, logger="polybot"):
        _log_open_banner(dict(_CTX), 0.77, settled="paper")
    assert "OPEN Up" in caplog.text
    assert "fee $" in caplog.text and "not charged" not in caplog.text
    assert "provisional" not in caplog.text


def test_chain_banner_fee_is_exact_from_settled_shares(caplog):
    # Chain-true booking: wallet holds exactly notional/VWAP shares -> fee $0.00.
    with caplog.at_level(logging.INFO, logger="polybot"):
        _log_open_banner(dict(_CTX), 0.77, settled="chain", shares=1.61 / 0.77)
    assert "fee $0.00" in caplog.text
    caplog.clear()
    # A real 2c fee (fewer shares than notional/price) must surface exactly.
    with caplog.at_level(logging.INFO, logger="polybot"):
        _log_open_banner(dict(_CTX), 0.77, settled="chain", shares=(1.61 - 0.02) / 0.77)
    assert "fee $0.02" in caplog.text


def test_settled_banner_prints_once_from_audit_callback(caplog):
    _pending_settled_banners.clear()
    _lru_set(_pending_settled_banners, 42, dict(_CTX), 32)
    with caplog.at_level(logging.INFO, logger="polybot"):
        _on_entry_settled(42, 0.77, "chain")
    assert "OPEN Up" in caplog.text and "@0.77" in caplog.text
    assert not _pending_settled_banners
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="polybot"):
        _on_entry_settled(42, 0.77, "chain")   # duplicate settle → no second banner
    assert "OPEN Up" not in caplog.text


def test_settled_banner_flags_provisional_when_chain_lookup_fails(caplog):
    _pending_settled_banners.clear()
    _lru_set(_pending_settled_banners, 7, dict(_CTX), 32)
    with caplog.at_level(logging.INFO, logger="polybot"):
        _on_entry_settled(7, 0.81, "provisional")
    assert "provisional" in caplog.text


# ── Discord OPEN ping follows the settled entry (the UI must match the books) ─

class _FakeAlerts:
    def __init__(self):
        self.sent = []

    async def send_trade_opened(self, **kw):
        self.sent.append(kw)


@pytest.mark.asyncio
async def test_settled_callback_sends_discord_ping_with_chain_price():
    import asyncio
    _pending_settled_banners.clear()
    am = _FakeAlerts()
    ctx = dict(_CTX, alert_manager=am, question="Bitcoin Up or Down - July 26, 1AM ET",
               mkt_price=0.76)
    _lru_set(_pending_settled_banners, 11, ctx, 32)
    _on_entry_settled(11, 0.57, "chain")     # audit settled 0.77 → 0.57
    await asyncio.sleep(0)                   # let the create_task'd send run
    assert len(am.sent) == 1
    kw = am.sent[0]
    assert kw["entry_price"] == pytest.approx(0.57)
    assert kw["provisional"] is False
    # fee buffer recomputed at the settled price: rate·size·(1−entry)
    assert kw["fee"] == pytest.approx(0.07 * 1.61 * (1 - 0.57), rel=1e-6)


@pytest.mark.asyncio
async def test_settled_callback_flags_provisional_ping_on_lookup_failure():
    import asyncio
    _pending_settled_banners.clear()
    am = _FakeAlerts()
    ctx = dict(_CTX, alert_manager=am, question="q", mkt_price=0.76)
    _lru_set(_pending_settled_banners, 12, ctx, 32)
    _on_entry_settled(12, 0.81, "provisional")
    await asyncio.sleep(0)
    assert am.sent and am.sent[0]["provisional"] is True
    assert am.sent[0]["entry_price"] == pytest.approx(0.81)


@pytest.mark.asyncio
async def test_day_stats_fees_are_the_modeled_buffer_sum():
    """Day-close fee figure = Σ per-trade modeled entry buffer (rate·size·(1−e))
    + recorded exit-fee models — the same quantity the OPEN pings display, so
    the day total finally matches what was shown through the day."""
    from polybot.db.models import Database
    db = Database(":memory:")
    await db.initialize()
    try:
        await db.conn.execute(
            "INSERT INTO positions (id, market_id, question, side, entry_price, size, "
            "signal_score, entry_timestamp, status, fee_rate, shares_held) "
            "VALUES (1, 'm', 'q', 'Up', 0.57, 1.26, 0.9, "
            "'2026-07-26T04:58:00+00:00', 'closed', 0.07, 2.21)")
        await db.conn.execute(
            "INSERT INTO trade_history (side, entry_price, exit_price, size, "
            "exit_timestamp, exit_reason, pnl, fees, position_id) "
            "VALUES ('Up', 0.57, 1.0, 1.26, '2026-07-26T05:00:00+00:00', "
            "'resolution', 0.95, 0.0, 1)")
        await db.conn.commit()
        wins, losses, fees, pnl = await db.get_day_stats("2026-07-26")
        assert (wins, losses) == (1, 0)
        assert pnl == pytest.approx(0.95)
        assert fees == pytest.approx(0.07 * 1.26 * (1 - 0.57), rel=1e-6)
    finally:
        await db.close()   # a stranded aiosqlite worker thread blocks pytest exit
