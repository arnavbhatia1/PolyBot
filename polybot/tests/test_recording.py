"""Window-path recorder + tape recorder units."""
import asyncio
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
        "INSERT OR REPLACE INTO window_labels VALUES (?, ?, ?, ?, ?)",
        ("btc-updown-5m-1", 1, 61010.0, 60990.0, time.time()))
    await db.conn.commit()
    cur = await db.conn.execute("SELECT resolved_up FROM window_labels")
    assert (await cur.fetchone())["resolved_up"] == 1
    await rec.stop()
    await db.close()


def test_tape_recorder_writes_jsonl(tmp_path):
    rec = TapeRecorder(dir_path=tmp_path)
    rec.on_trade("tok1", {"price": "0.55", "size": "20", "side": "BUY",
                          "timestamp": time.time()})
    rec.flush()
    files = list(tmp_path.glob("tape_*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text().strip())
    assert row["token"] == "tok1" and row["side"] == "BUY"


def test_tape_recorder_never_raises():
    rec = TapeRecorder(dir_path=None)
    rec.dir = None  # force an internal failure path
    rec.on_trade("tok", {"price": "x"})  # swallowed
