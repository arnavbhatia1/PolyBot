"""Window-path recorder + tape recorder units."""
import json
import time

import pytest

from polybot.db.models import Database
from polybot.recording import TapeRecorder, WindowPathRecorder, _top3_usd


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


class _FakeClob:
    def __init__(self):
        now = time.time()
        self.books = {
            "tu": {"bids": [{"price": "0.55", "size": "100"}],
                   "asks": [{"price": "0.57", "size": "80"}], "ts": now},
            "td": {"bids": [{"price": "0.43", "size": "60"}],
                   "asks": [{"price": "0.45", "size": "90"}], "ts": now},
        }
        self.best_bid_ask = {
            "tu": {"best_bid": "0.55", "best_ask": "0.57", "ts": now},
            "td": {"best_bid": "0.43", "best_ask": "0.45", "ts": now},
        }

    def get_book(self, t):
        return self.books.get(t, {})

    async def subscribe(self, tokens):
        pass


class _FakeCoinbase:
    def __init__(self, price=61000.0):
        class _S: pass
        self.state = _S()
        self.state.price = price
        self.state.age_seconds = 0.5


class _FakeChainlink:
    def get_strike(self, window_ts):
        return 60990.0


class _FakeBuffer:
    def get_closes(self):
        return [60000.0, 60010.0, 60005.0]


class _FakeBinanceFeed:
    def __init__(self):
        self.buffer = _FakeBuffer()


class _FakeIndicatorEngine:
    def compute_all(self, buffer):
        return {"atr": {"atr": 25.0, "passes": True, "candle_ts": 7}}


class _FakeSignalEngine:
    def compute_probability(self, btc_price, strike_price, seconds_remaining,
                            atr, closes=None, atr_candle_ts=None):
        assert atr == 25.0 and atr_candle_ts == 7
        return 0.7123


def test_top3_usd_sums_first_three_levels():
    levels = [{"price": "0.5", "size": "10"}, {"price": "0.6", "size": "10"},
              {"price": "0.7", "size": "10"}, {"price": "0.9", "size": "1000"}]
    assert _top3_usd(levels) == 0.5 * 10 + 0.6 * 10 + 0.7 * 10


@pytest.mark.asyncio
async def test_window_recorder_samples_and_flushes(db, tmp_path, monkeypatch):
    import polybot.recording as recording
    monkeypatch.setattr(recording, "PATHS_DB", tmp_path / "paths.db")
    await db.initialize()
    rec = WindowPathRecorder(db=db, clob_ws=_FakeClob(), coinbase_feed=_FakeCoinbase(),
                             chainlink_feed=_FakeChainlink(), market_scanner=None,
                             http_client=None)
    await rec.ensure_tables()
    window_ts = int(time.time() // 300) * 300
    rec._window = {"market_id": f"btc-updown-5m-{window_ts}", "window_ts": window_ts,
                   "token_up": "tu", "token_down": "td"}
    rec.mark_traded(f"btc-updown-5m-{window_ts}")
    rec._sample()
    await rec._flush()
    cur = await rec._paths_conn.execute("SELECT * FROM window_paths")
    rows = await cur.fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["bid_up"] == 0.55 and r["ask_down"] == 0.45
    assert r["strike"] == 60990.0 and r["coinbase_price"] == 61000.0
    assert r["traded"] == 1
    assert 0 <= r["elapsed_s"] <= 300
    # No L1-stamping deps wired -> the appended columns stay NULL (never 0.0).
    assert r["atr"] is None and r["model_prob_up"] is None
    await rec.stop()
    await db.close()


@pytest.mark.asyncio
async def test_window_recorder_stamps_live_l1(db, tmp_path, monkeypatch):
    import polybot.recording as recording
    monkeypatch.setattr(recording, "PATHS_DB", tmp_path / "paths.db")
    await db.initialize()
    rec = WindowPathRecorder(db=db, clob_ws=_FakeClob(), coinbase_feed=_FakeCoinbase(),
                             chainlink_feed=_FakeChainlink(), market_scanner=None,
                             http_client=None, binance_feed=_FakeBinanceFeed(),
                             indicator_engine=_FakeIndicatorEngine(),
                             signal_engine=_FakeSignalEngine())
    await rec.ensure_tables()
    window_ts = int(time.time() // 300) * 300
    rec._window = {"market_id": f"btc-updown-5m-{window_ts}", "window_ts": window_ts,
                   "token_up": "tu", "token_down": "td"}
    rec._sample()
    await rec._flush()
    cur = await rec._paths_conn.execute("SELECT atr, model_prob_up FROM window_paths")
    r = (await cur.fetchall())[0]
    assert r["atr"] == 25.0
    assert r["model_prob_up"] == 0.7123
    await rec.stop()
    await db.close()


class _FakeChainlinkLive(_FakeChainlink):
    price = 60995.5
    age_seconds = 1.25


class _FakeCoinbaseFull(_FakeCoinbase):
    def __init__(self):
        super().__init__()
        self.state.best_bid = 60999.0
        self.state.best_ask = 61001.0

    def get_cvd(self, window_s):
        return 3.5 if window_s == 10.0 else 7.25

    def covers(self, window_s):
        return True  # buffer continuously spans the window (no reconnect)


class _FakeDepthFeed:
    def __init__(self):
        self.updated_at = time.time()
        self.top_bids = [["60990", "2.0"], ["60980", "1.0"]]
        self.top_asks = [["61010", "1.5"]]

    def get_depth_usd(self, levels=20):
        return 1.0  # unused here; recorder computes side-split sums itself


@pytest.mark.asyncio
async def test_window_recorder_full_capture_columns(db, tmp_path, monkeypatch):
    import polybot.recording as recording
    monkeypatch.setattr(recording, "PATHS_DB", tmp_path / "paths.db")
    await db.initialize()
    rec = WindowPathRecorder(db=db, clob_ws=_FakeClob(), coinbase_feed=_FakeCoinbaseFull(),
                             chainlink_feed=_FakeChainlinkLive(), market_scanner=None,
                             http_client=None, binance_depth=_FakeDepthFeed())
    await rec.ensure_tables()
    # Migration appended every declared column.
    cur = await rec._paths_conn.execute("PRAGMA table_info(window_paths)")
    have = {r["name"] for r in await cur.fetchall()}
    for name, _ in WindowPathRecorder._APPENDED_COLUMNS:
        assert name in have, f"missing appended column {name}"
    window_ts = int(time.time() // 300) * 300
    rec._window = {"market_id": f"btc-updown-5m-{window_ts}", "window_ts": window_ts,
                   "token_up": "tu", "token_down": "td"}
    rec._sample()
    await rec._flush()
    cur = await rec._paths_conn.execute("SELECT * FROM window_paths")
    r = (await cur.fetchall())[0]
    assert r["chainlink_price"] == 60995.5 and r["chainlink_age_s"] == 1.25
    assert 0 <= r["book_age_up_s"] < 5 and 0 <= r["book_age_down_s"] < 5
    assert r["coinbase_bid"] == 60999.0 and r["coinbase_ask"] == 61001.0
    assert r["coinbase_cvd_10s"] == 3.5 and r["coinbase_cvd_30s"] == 7.25
    assert r["bid_sz_up"] == 100.0 and r["ask_sz_up"] == 80.0
    assert r["bid_sz_down"] == 60.0 and r["ask_sz_down"] == 90.0
    assert r["depth20_bid_usd"] == pytest.approx(60990 * 2.0 + 60980 * 1.0)
    assert r["depth20_ask_usd"] == pytest.approx(61010 * 1.5)
    await rec.stop()
    await db.close()


@pytest.mark.asyncio
async def test_window_recorder_full_capture_null_on_cold(db, tmp_path, monkeypatch):
    """Cold/absent feeds -> the new columns record NULL, never 0.0 stand-ins."""
    import polybot.recording as recording
    monkeypatch.setattr(recording, "PATHS_DB", tmp_path / "paths.db")
    await db.initialize()
    rec = WindowPathRecorder(db=db, clob_ws=_FakeClob(), coinbase_feed=_FakeCoinbase(),
                             chainlink_feed=_FakeChainlink(), market_scanner=None,
                             http_client=None)  # no depth feed; plain fakes
    await rec.ensure_tables()
    window_ts = int(time.time() // 300) * 300
    rec._window = {"market_id": f"btc-updown-5m-{window_ts}", "window_ts": window_ts,
                   "token_up": "tu", "token_down": "td"}
    rec._sample()
    await rec._flush()
    cur = await rec._paths_conn.execute(
        "SELECT chainlink_price, chainlink_age_s, coinbase_bid, coinbase_ask, "
        "coinbase_cvd_10s, coinbase_cvd_30s, depth20_bid_usd, depth20_ask_usd "
        "FROM window_paths")
    r = (await cur.fetchall())[0]
    assert all(r[k] is None for k in r.keys())
    await rec.stop()
    await db.close()


@pytest.mark.asyncio
async def test_window_recorder_label_write(db, tmp_path, monkeypatch):
    import polybot.recording as recording
    monkeypatch.setattr(recording, "PATHS_DB", tmp_path / "paths.db")
    await db.initialize()
    rec = WindowPathRecorder(db=db, clob_ws=_FakeClob(), coinbase_feed=None,
                             chainlink_feed=None, market_scanner=None, http_client=None)
    await rec.ensure_tables()
    await db.conn.execute(
        "INSERT OR REPLACE INTO window_labels "
        "(window_id, resolved_up, final_price, price_to_beat, labeled_at, token_up, token_down) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("btc-updown-5m-1", 1, 61010.0, 60990.0, time.time(), "tok-u", "tok-d"))
    await db.conn.commit()
    cur = await db.conn.execute("SELECT resolved_up, token_up, token_down FROM window_labels")
    row = await cur.fetchone()
    assert row["resolved_up"] == 1
    # token map persisted with the label — the join key for tape/micro research
    assert row["token_up"] == "tok-u" and row["token_down"] == "tok-d"
    await rec.stop()
    await db.close()


def test_tape_recorder_writes_jsonl(tmp_path):
    rec = TapeRecorder(dir_path=tmp_path)
    rec.on_trade("tok1", {"price": "0.55", "size": "20", "side": "BUY",
                          "timestamp": time.time()})
    rec.flush()
    rec._writer.shutdown(wait=True)  # writes land on the writer thread — drain first
    files = list(tmp_path.glob("tape_*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text().strip())
    assert row["token"] == "tok1" and row["side"] == "BUY"


def test_tape_recorder_never_raises():
    rec = TapeRecorder(dir_path=None)
    rec.dir = None  # force an internal failure path
    rec.on_trade("tok", {"price": "x"})  # swallowed


class TestMicroTape:
    def _tape(self, tmp_path):
        from polybot.recording import MicroTape
        return MicroTape(dir_path=tmp_path)

    def test_late_phase_gating(self, tmp_path, monkeypatch):
        """b/c events record only in the final 90s of a window (elapsed >= 210);
        l (chainlink) events record always — boundary-adjacent reports are the
        strike-research corpus."""
        import polybot.recording as rec
        t = self._tape(tmp_path)
        t.flush = lambda: None   # keep events in _buf for counting
        early = (int(time.time() // 300)) * 300 + 100.0   # elapsed 100s
        late = (int(time.time() // 300)) * 300 + 250.0    # elapsed 250s
        monkeypatch.setattr(rec.time, "time", lambda: early)
        t.on_cb_tick(early, 60000.0)
        t.on_bba("tok", {"bid": "0.5", "ask": "0.52"})
        assert t._buf == []
        monkeypatch.setattr(rec.time, "time", lambda: late)
        t.on_cb_tick(late, 60010.0)
        t.on_bba("tok", {"bid": "0.5", "ask": "0.52"})
        t.on_cl_report(late, 60005.0)
        assert len(t._buf) == 3

    def test_schema_and_flush(self, tmp_path):
        import json as _j
        t = self._tape(tmp_path)
        late = (int(time.time() // 300)) * 300 + 250.0
        t.on_cb_tick(late, 60010.0)
        t.on_cl_report(1783600000.0, 60005.0)
        t.flush()
        t._writer.shutdown(wait=True)
        files = list(tmp_path.glob("micro_*.jsonl"))
        assert len(files) == 1
        rows = [_j.loads(l) for l in files[0].read_text().splitlines()]
        kinds = {r["k"] for r in rows}
        assert kinds == {"c", "l"}
        l_row = next(r for r in rows if r["k"] == "l")
        assert l_row["ts"] == 1783600000.0 and "rx" in l_row and l_row["p"] == 60005.0

    def test_hooks_never_raise(self, tmp_path):
        """Feed callbacks must be crash-proof — a tape bug can't touch the money path."""
        t = self._tape(tmp_path)
        t._buf = None  # force internal failure
        t.on_cb_tick(time.time(), 60000.0)   # must not raise
        t.on_bba("tok", {})                  # must not raise
        t.on_cl_report(time.time(), 1.0)     # must not raise


@pytest.mark.asyncio
async def test_recordings_cleanup_job_prunes_old_files(tmp_path, monkeypatch):
    """Nightly sweep deletes recordings past retention (the only unbounded-growth
    files on the host) and leaves fresh ones. Micro-tape ages out on its own
    shorter clock (~2.6GB/day would fill the 45GB host under tape's window)."""
    import os
    import polybot.recording as recording
    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    old = tmp_path / "micro_2026-01-01.jsonl"; old.write_text("x")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    micro_aged = tmp_path / "micro_2026-07-01.jsonl"; micro_aged.write_text("x")
    os.utime(micro_aged, (time.time() - 10 * 86400,) * 2)   # > micro window, < tape window
    micro_fresh = tmp_path / "micro_2026-07-12.jsonl"; micro_fresh.write_text("x")
    os.utime(micro_fresh, (time.time() - 2 * 86400,) * 2)
    tape_aged = tmp_path / "tape_2026-07-01.jsonl"; tape_aged.write_text("x")
    os.utime(tape_aged, (time.time() - 10 * 86400,) * 2)    # tape keeps the long window
    fresh = tmp_path / "tape_2026-07-12.jsonl"; fresh.write_text("x")
    keepme = tmp_path / "notes.txt"; keepme.write_text("x")   # non-jsonl untouched
    os.utime(keepme, (time.time() - 400 * 86400,) * 2)
    job = recording.recordings_cleanup_job(retention_days=30, micro_retention_days=7)
    out = await job()
    assert out["recordings_deleted"] == 2
    assert not old.exists() and not micro_aged.exists()
    assert micro_fresh.exists() and tape_aged.exists() and fresh.exists() and keepme.exists()
