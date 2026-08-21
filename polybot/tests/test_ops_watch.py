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


# ── GTC RTT watch (the ladder's fill clock — RESEARCH.md 2b) ─────────────────

def _gtc_stats(tmp_path, n, p50=500.0, age_days=0, cancel_p50=54.0):
    from polybot.main import _gtc_watch  # noqa: F401 — import check
    p = tmp_path / "latency_stats.json"
    p.write_text(json.dumps({
        "gtc": {
            "place": {"n": n, "p50_ms": p50},
            "cancel": {"n": n, "p50_ms": cancel_p50},
            "last_updated": (datetime.now(timezone.utc)
                             - timedelta(days=age_days)).isoformat(),
        },
    }))
    return p


def test_gtc_watch_dark_until_samples_exist(tmp_path):
    from polybot.main import _gtc_watch
    p = tmp_path / "latency_stats.json"
    p.write_text(json.dumps({"post": {"n": 50}}))
    gtc, dark = _gtc_watch(p)
    assert gtc is None and "smoke_gtc_test" in dark
    line = _ops_watch_line(None, None, None, gtc, dark)
    assert "GTC RTT unmeasured" in line


def test_gtc_watch_warns_when_paper_table_drifts(tmp_path):
    from polybot.main import _gtc_watch
    gtc, dark = _gtc_watch(_gtc_stats(tmp_path, n=12, p50=500.0))
    assert dark is None
    line = _ops_watch_line(None, None, None, gtc, dark)
    assert "⚠️" in line and "_GTC_LATENCY_QUANTILES" in line


def test_gtc_watch_quiet_inside_the_band(tmp_path):
    from polybot.main import _gtc_watch
    gtc, dark = _gtc_watch(_gtc_stats(tmp_path, n=12, p50=58.0))
    line = _ops_watch_line(None, None, None, gtc, dark)
    assert "GTC place p50 58ms" in line and "⚠️" not in line


def test_gtc_recorder_merges_with_the_fok_writer(tmp_path, monkeypatch):
    """The FOK writer replaces the whole stats file; a GTC section it does not
    preserve would vanish on the next taker POST."""
    import polybot.execution.live_trader as lt
    p = tmp_path / "latency_stats.json"
    monkeypatch.setattr(lt, "_LATENCY_STATS_PATH", p)
    monkeypatch.setattr(lt, "_GTC_PLACE_SAMPLES", type(lt._GTC_PLACE_SAMPLES)(maxlen=400))
    lt._record_gtc_latency("place", 0.5)
    lt._record_submit_latency(0.44, 0.004, 0.436)
    stats = json.loads(p.read_text())
    assert stats["gtc"]["place"]["n"] == 1
    assert stats["gtc"]["place"]["p50_ms"] == 500.0
    assert stats["post"]["n"] >= 1


def test_gtc_ks_flags_a_shape_drift_p50_misses(tmp_path):
    """Bimodal live RTTs with a table-matching median: p50 watch quiet, KS loud."""
    from polybot.main import _gtc_watch
    samples = [20.0] * 6 + [56.0] + [900.0] * 5   # median ~56ms, shape nothing like the table
    p = tmp_path / "latency_stats.json"
    p.write_text(json.dumps({"gtc": {
        "place": {"n": 12, "p50_ms": 56.0, "samples_ms": samples},
        "cancel": {"n": 12, "p50_ms": 54.0},
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }}))
    gtc, dark = _gtc_watch(p)
    assert dark is None and gtc["ks"] > 0.30
    line = _ops_watch_line(None, None, None, gtc, dark)
    assert "⚠️" in line


def test_gtc_ks_quiet_when_samples_match_the_table(tmp_path):
    from polybot.main import _gtc_watch
    from polybot.execution.paper_trader import PaperTrader
    import random
    rng = random.Random(7)
    qs = PaperTrader._GTC_LATENCY_QUANTILES

    def draw():
        u = rng.random()
        for (q0, v0), (q1, v1) in zip(qs[:-1], qs[1:]):
            if u <= q1:
                return (v0 + (v1 - v0) * (u - q0) / (q1 - q0)) * 1000.0
        return qs[-1][1] * 1000.0
    samples = sorted(draw() for _ in range(60))
    p = tmp_path / "latency_stats.json"
    p.write_text(json.dumps({"gtc": {
        "place": {"n": 60, "p50_ms": 56.0, "samples_ms": samples},
        "cancel": {"n": 60, "p50_ms": 54.0},
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }}))
    gtc, dark = _gtc_watch(p)
    assert gtc["ks"] <= 0.30
    assert "⚠️" not in _ops_watch_line(None, None, None, gtc, dark)
