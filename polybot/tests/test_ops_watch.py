"""Nightly ops trend watches (main._latency_watch / _ops_watch_line).

A watch that stays silent because its input is unusable reads exactly like a
watch that is happy — these constants gate the deployment authority.
"""
import json
from datetime import datetime, timedelta, timezone

from polybot.main import _latency_watch, _ops_watch_line


def _stats(tmp_path, n, age_days=0, p50=436.0):
    p = tmp_path / "latency_stats.json"
    p.write_text(json.dumps({
        "post": {"n": n, "p50_ms": p50},
        "last_updated": (datetime.now(timezone.utc)
                         - timedelta(days=age_days)).isoformat(),
    }))
    return p


def test_thin_sample_is_named_not_hidden(tmp_path):
    """The live file sits at n=2: the watch has never surfaced and its silence
    was indistinguishable from health."""
    lat, dark = _latency_watch(_stats(tmp_path, n=2))
    assert lat is None and "2 order samples" in dark
    assert "POST p50 unknown — only 2 order samples" in _ops_watch_line(lat, dark, None)


def test_stale_file_is_named(tmp_path):
    lat, dark = _latency_watch(_stats(tmp_path, n=50, age_days=9))
    assert lat is None and "9 days ago" in dark
    assert "POST p50 unknown" in _ops_watch_line(lat, dark, None)


def test_missing_file_is_named(tmp_path):
    lat, dark = _latency_watch(tmp_path / "nope.json")
    assert lat is None and "no usable order-latency file" in dark
    assert "POST p50 unknown" in _ops_watch_line(lat, dark, None)


def test_usable_stats_report_the_number(tmp_path):
    lat, dark = _latency_watch(_stats(tmp_path, n=50, p50=430.0))
    assert dark is None and lat == {"p50": 430.0, "n": 50}
    line = _ops_watch_line(lat, dark, None)
    assert "POST p50 430ms (n=50)" in line and "⚠️" not in line


def test_drifted_stats_warn(tmp_path):
    lat, dark = _latency_watch(_stats(tmp_path, n=50, p50=700.0))
    assert "⚠️" in _ops_watch_line(lat, dark, None)


def _qd(p75):
    return {"med": 31.0, "p75": p75, "n": 56333, "days": 14.0}


def test_queue_watch_warns_on_a_shrink_too():
    """14-day p75 is 99 sh — 0.73x the 135 constant — and the one-sided watch
    stayed silent, so paper under-credits fills on a gate that counts fills."""
    line = _ops_watch_line(None, None, _qd(99.0))
    assert "⚠️" in line and "under-credits" in line


def test_queue_watch_still_warns_on_growth():
    line = _ops_watch_line(None, None, _qd(400.0))
    assert "⚠️" in line and "over-credits" in line


def test_queue_watch_quiet_inside_the_band():
    line = _ops_watch_line(None, None, _qd(140.0))
    assert "deep-queue consumed" in line and "⚠️" not in line
