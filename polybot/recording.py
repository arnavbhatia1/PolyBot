"""Window-path + CLOB-tape recorders (Phases 1-2 of tasks/todo.md).

WindowPathRecorder: 1 Hz state of EVERY 5-min window — both tokens' BBO + top-3
depth, Coinbase mid, strike, elapsed — traded or not. Self-discovering (its own
Gamma slug fetch + CLOB WS subscribe) so coverage is all ~288 windows/day, not
just the ones the trading loop enters; labels itself from Gamma event_metadata
after each window closes. This is the training stream for the Phase 3 exit-value
model: ~288 labeled windows/day instead of ~35 trades/day.

TapeRecorder: every CLOB trade print to a daily JSONL (gitignored — recordings
must never enter the nightly memory/ commit). Input to the Phase 1 passive-exit
shadow sim (a resting order "fills" only when the tape prints through it) and
the Phase 6 maker-markout study.

Both are write-behind: samples buffer in memory and flush in batches, so the
trading loop never waits on disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.paths import MEMORY_DIR

logger = logging.getLogger(__name__)

RECORDINGS_DIR: Path = MEMORY_DIR / "recordings"
# 1 Hz path rows (~86k/day) live in their own gitignored DB so the nightly git
# commit of the per-mode DB stays small; window_labels (tiny, joined by the
# wallet job and analysis) stay in the per-mode DB.
PATHS_DB: Path = Path(__file__).resolve().parent / "db" / "window_paths.db"

_FLUSH_EVERY_S = 10.0
_TAPE_FLUSH_ROWS = 200
_LABEL_RETRY_S = 60.0
_LABEL_GIVE_UP_S = 2400.0  # stop asking Gamma 40 min after window end


def _top3_usd(levels: list[dict[str, Any]]) -> float:
    try:
        return round(sum(float(l["price"]) * float(l["size"]) for l in levels[:3]), 2)
    except (KeyError, ValueError, TypeError):
        return 0.0


class WindowPathRecorder:
    """Samples the active 5-min window at 1 Hz into the per-mode DB.

    Tables (created on first run):
      window_paths (in PATHS_DB, gitignored): window_id, ts, elapsed_s, bid/ask
                   both sides, top-3 depths, coinbase_price, strike, traded
      window_labels (in the per-mode DB): window_id PRIMARY KEY, resolved_up,
                   final_price, price_to_beat, labeled_at
    """

    def __init__(self, db: Any, clob_ws: Any, coinbase_feed: Any,
                 chainlink_feed: Any, market_scanner: Any, http_client: Any) -> None:
        self.db = db
        self.clob_ws = clob_ws
        self.coinbase_feed = coinbase_feed
        self.chainlink_feed = chainlink_feed
        self.market_scanner = market_scanner
        self.http_client = http_client
        self._window: dict[str, Any] | None = None
        self._discovering: int = 0          # window_ts a discovery task is running for
        self._traded: set[str] = set()
        self._pending_label: dict[str, float] = {}  # window_id -> window_end_ts
        self._last_label_run = 0.0
        self._rows: list[tuple] = []
        self._running = False
        self._paths_conn = None
        self.rows_written = 0
        self.labels_written = 0

    async def ensure_tables(self) -> None:
        import aiosqlite
        if self._paths_conn is None:
            self._paths_conn = await aiosqlite.connect(str(PATHS_DB))
            self._paths_conn.row_factory = aiosqlite.Row
            await self._paths_conn.execute("PRAGMA journal_mode=WAL")
            await self._paths_conn.execute("PRAGMA synchronous=NORMAL")
            await self._paths_conn.execute("PRAGMA busy_timeout=15000")
        await self._paths_conn.executescript("""
            CREATE TABLE IF NOT EXISTS window_paths (
                window_id TEXT NOT NULL,
                ts REAL NOT NULL,
                elapsed_s REAL NOT NULL,
                bid_up REAL, ask_up REAL, bid_down REAL, ask_down REAL,
                depth3_bid_up REAL, depth3_ask_up REAL,
                depth3_bid_down REAL, depth3_ask_down REAL,
                coinbase_price REAL,
                strike REAL,
                traded INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_window_paths_window
                ON window_paths(window_id);
            CREATE INDEX IF NOT EXISTS idx_window_paths_ts
                ON window_paths(ts);
        """)
        await self._paths_conn.commit()
        await self.db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS window_labels (
                window_id TEXT PRIMARY KEY,
                resolved_up INTEGER NOT NULL,
                final_price REAL,
                price_to_beat REAL,
                labeled_at REAL NOT NULL
            );
        """)
        await self.db.conn.commit()
        await self._migrate_paths_out_of_main_db()

    async def _migrate_paths_out_of_main_db(self) -> None:
        """One-time: move pre-split window_paths rows out of the per-mode DB so
        the nightly git commit stays small."""
        try:
            cur = await self.db.conn.execute("SELECT * FROM window_paths")
            rows = await cur.fetchall()
        except Exception:
            return
        if rows:
            await self._paths_conn.executemany(
                "INSERT INTO window_paths VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [tuple(r) for r in rows])
            await self._paths_conn.commit()
            logger.info(f"window_paths migrated out of per-mode DB: {len(rows)} rows")
        await self.db.conn.executescript("DROP TABLE IF EXISTS window_paths;")
        await self.db.conn.execute("VACUUM")
        await self.db.conn.commit()

    def mark_traded(self, market_id: str) -> None:
        """Trading loop hook: the bot opened a position in this window."""
        self._traded.add(market_id)
        if len(self._traded) > 500:
            self._traded = set(list(self._traded)[-100:])

    async def _fetch_contract(self, slug: str) -> dict | None:
        try:
            resp = await self.http_client.get(
                f"{self.market_scanner.GAMMA_API}/events", params={"slug": slug})
            resp.raise_for_status()
            data = resp.json()
            if data:
                return self.market_scanner.parse_contract(
                    data[0] if isinstance(data, list) else data)
        except Exception:
            pass
        return None

    async def _discover(self, window_ts: int) -> None:
        slug = f"{self.market_scanner.symbol}-updown-5m-{window_ts}"
        contract = await self._fetch_contract(slug)
        self._discovering = 0
        if not contract:
            return
        token_up = contract.get("token_id_up", "")
        token_down = contract.get("token_id_down", "")
        if not token_up or not token_down:
            return
        prev = self._window
        if prev is not None and prev["window_ts"] != window_ts:
            self._pending_label[prev["market_id"]] = prev["window_ts"] + 300.0
        self._window = {
            "market_id": contract.get("slug", slug),
            "window_ts": window_ts,
            "token_up": token_up,
            "token_down": token_down,
        }
        try:
            await self.clob_ws.subscribe([token_up, token_down])
        except Exception as e:
            logger.debug(f"recorder subscribe failed: {e}")

    async def _label_pass(self) -> None:
        now = time.time()
        for market_id, end_ts in list(self._pending_label.items()):
            if now > end_ts + _LABEL_GIVE_UP_S:
                self._pending_label.pop(market_id, None)
                continue
            if now < end_ts + 30:
                continue
            contract = await self._fetch_contract(market_id)
            meta = (contract or {}).get("event_metadata") or {}
            fp, ptb = meta.get("final_price"), meta.get("price_to_beat")
            if fp is None or ptb is None:
                continue
            self._pending_label.pop(market_id, None)
            try:
                await self.db.conn.execute(
                    "INSERT OR REPLACE INTO window_labels VALUES (?, ?, ?, ?, ?)",
                    (market_id, 1 if fp >= ptb else 0, fp, ptb, now))
                await self.db.conn.commit()
                self.labels_written += 1
            except Exception as e:
                logger.warning(f"window label write failed for {market_id}: {e}")

    def _sample(self) -> None:
        w = self._window
        if w is None:
            return
        now = time.time()
        elapsed = now - w["window_ts"]
        if elapsed < 0 or elapsed > 300:
            return
        book_up = self.clob_ws.get_book(w["token_up"]) if self.clob_ws else {}
        book_dn = self.clob_ws.get_book(w["token_down"]) if self.clob_ws else {}
        bba_up = self.clob_ws.best_bid_ask.get(w["token_up"], {}) if self.clob_ws else {}
        bba_dn = self.clob_ws.best_bid_ask.get(w["token_down"], {}) if self.clob_ws else {}

        def _f(d: dict, k: str) -> float | None:
            try:
                v = float(d.get(k, 0) or 0)
                return v if v > 0 else None
            except (ValueError, TypeError):
                return None

        cb = self.coinbase_feed.state.price if (
            self.coinbase_feed and self.coinbase_feed.state.age_seconds < 5) else None
        strike = (self.chainlink_feed.get_strike(w["window_ts"])
                  if self.chainlink_feed else None)
        self._rows.append((
            w["market_id"], round(now, 3), round(elapsed, 1),
            _f(bba_up, "best_bid"), _f(bba_up, "best_ask"),
            _f(bba_dn, "best_bid"), _f(bba_dn, "best_ask"),
            _top3_usd(book_up.get("bids") or []), _top3_usd(book_up.get("asks") or []),
            _top3_usd(book_dn.get("bids") or []), _top3_usd(book_dn.get("asks") or []),
            cb, strike,
            1 if w["market_id"] in self._traded else 0,
        ))

    async def _flush(self) -> None:
        if not self._rows:
            return
        rows, self._rows = self._rows, []
        try:
            await self._paths_conn.executemany(
                "INSERT INTO window_paths VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            await self._paths_conn.commit()
            self.rows_written += len(rows)
        except Exception as e:
            logger.warning(f"window_paths flush failed ({len(rows)} rows): {e}")

    async def run(self) -> None:
        self._running = True
        await self.ensure_tables()
        logger.info("Window-path recorder running (1 Hz, all windows, batched flush)")
        last_flush = time.time()
        while self._running:
            try:
                window_ts = int(time.time() // 300) * 300
                cur = self._window
                if ((cur is None or cur["window_ts"] != window_ts)
                        and self._discovering != window_ts):
                    self._discovering = window_ts
                    asyncio.create_task(self._discover(window_ts))
                self._sample()
                now = time.time()
                if now - last_flush >= _FLUSH_EVERY_S:
                    await self._flush()
                    last_flush = now
                if now - self._last_label_run >= _LABEL_RETRY_S and self._pending_label:
                    self._last_label_run = now
                    asyncio.create_task(self._label_pass())
            except Exception as e:
                logger.warning(f"window recorder tick failed: {e}")
            await asyncio.sleep(1.0)

    async def stop(self) -> None:
        self._running = False
        await self._flush()
        if self._paths_conn is not None:
            await self._paths_conn.close()
            self._paths_conn = None


class TapeRecorder:
    """CLOB trade prints → memory/recordings/tape_YYYY-MM-DD.jsonl (gitignored)."""

    def __init__(self, dir_path: Path | None = None) -> None:
        self.dir = dir_path or RECORDINGS_DIR
        self._buf: list[str] = []
        self._last_flush = time.time()

    def on_trade(self, asset_id: str, trade: dict[str, Any]) -> None:
        """Wired as ClobWebSocket.on_trade — must never raise into the feed."""
        try:
            self._buf.append(json.dumps({
                "ts": round(trade.get("timestamp", time.time()), 3),
                "token": asset_id,
                "price": trade.get("price"),
                "size": trade.get("size"),
                "side": trade.get("side"),
            }))
            if len(self._buf) >= _TAPE_FLUSH_ROWS or time.time() - self._last_flush > _FLUSH_EVERY_S:
                self.flush()
        except Exception:
            pass

    def flush(self) -> None:
        if not self._buf:
            return
        buf, self._buf = self._buf, []
        self._last_flush = time.time()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with (self.dir / f"tape_{day}.jsonl").open("a", encoding="utf-8") as f:
                f.write("\n".join(buf) + "\n")
        except Exception as e:
            logger.warning(f"tape flush failed ({len(buf)} prints): {e}")
