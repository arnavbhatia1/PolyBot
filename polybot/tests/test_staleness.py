"""StalenessTracker connection-state + lifetime-count disambiguation.

A persisted ``n=0`` snapshot could mean either "feed connected but received no
messages" (a genuinely quiet stream) or "socket never came up". The two are
operationally very different; the snapshot must distinguish them.
"""
from polybot.feeds._staleness import StalenessTracker


def test_snapshot_distinguishes_connected_quiet_from_never_connected():
    connected_quiet = StalenessTracker("binance_kline")
    connected_quiet.mark_connected()  # socket up, zero messages observed

    never_up = StalenessTracker("some_feed")  # nothing reported

    snap_quiet = connected_quiet.snapshot()
    snap_never = never_up.snapshot()

    # A connected-but-silent feed is now identifiable.
    assert snap_quiet["n"] == 0
    assert snap_quiet["n_total"] == 0
    assert snap_quiet["connected"] is True

    # Feeds that don't report connection state stay silent (no false "connected").
    assert "connected" not in snap_never


def test_disconnect_marks_state_false():
    t = StalenessTracker("binance_kline")
    t.mark_connected()
    t.mark_disconnected()
    assert t.snapshot()["connected"] is False


def test_n_total_counts_messages_and_survives_reset():
    t = StalenessTracker("f")
    t.observe(1.0)
    t.observe(2.0)
    t.observe(3.0)
    snap = t.snapshot()
    assert snap["n_total"] == 3       # lifetime message count
    assert snap["n"] == 2             # 2 inter-arrival gaps from 3 messages
    # reset() (called on reconnect) must not erase the lifetime count.
    t.reset()
    t.observe(10.0)
    assert t.snapshot()["n_total"] == 4


def test_percentiles_still_present_when_gaps_exist():
    t = StalenessTracker("f")
    for i in range(1, 11):
        t.observe(float(i))
    snap = t.snapshot()
    assert snap["n"] == 9
    assert "p50" in snap and "p95" in snap and "p99" in snap and "max" in snap
