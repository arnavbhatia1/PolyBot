"""Window-path + CLOB-tape recorders.

WindowPathRecorder: 1 Hz state of EVERY 5-min window — both tokens' BBO + top-3
depth, Coinbase mid, strike, elapsed — traded or not. Self-discovering (its own
Gamma slug fetch + CLOB WS subscribe) so coverage is all ~288 windows/day, not
just the ones the trading loop enters; labels itself from Gamma event_metadata
after each window closes. This is the exit-research corpus and the
late-window-sniper kill-bar feed: ~288 labeled windows/day instead of ~35
trades/day.

TapeRecorder: every CLOB trade print to a daily JSONL (gitignored — recordings
must never enter the nightly memory/ commit). A resting-order shadow "fills"
only when the tape prints through it.

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
# analysis) stay in the per-mode DB.
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
    """Samples the active 5-min window at 1 Hz (5 Hz in the final 45s).

    The late-window burst + Binance columns feed the late-window-sniper kill-bar
    study (scripts/analyze_late_window.py): the reverse-engineered winning edge is
    a final-seconds directional fill, and resolving whether a bot-FORMABLE signal
    survives a realistic 135ms FOK fill needs sub-second ask/Coinbase/Binance data.

    Tables (created on first run):
      window_paths (in PATHS_DB, gitignored): window_id, ts, elapsed_s, bid/ask
                   both sides, top-3 depths, coinbase_price, strike, traded,
                   binance_price, binance_cvd_10s/30s, atr, model_prob_up,
                   + full-capture columns (chainlink price/age, CLOB book ages,
                   coinbase BBO + CVD, touch sizes, Binance depth20 sides) —
                   see _APPENDED_COLUMNS
      window_labels (in the per-mode DB): window_id PRIMARY KEY, resolved_up,
                   final_price, price_to_beat, labeled_at

    `atr` + `model_prob_up` stamp the live L1 model per sample so the offline
    sniper harness can replicate the live `sniper_min_edge` floor exactly
    (without them the harness is only a conservative superset of live fires).
    """

    def __init__(self, db: Any, clob_ws: Any, coinbase_feed: Any,
                 chainlink_feed: Any, market_scanner: Any, http_client: Any,
                 binance_trades: Any = None, binance_feed: Any = None,
                 indicator_engine: Any = None, signal_engine: Any = None,
                 binance_depth: Any = None) -> None:
        self.db = db
        self.clob_ws = clob_ws
        self.coinbase_feed = coinbase_feed
        self.chainlink_feed = chainlink_feed
        self.market_scanner = market_scanner
        self.http_client = http_client
        # Binance aggTrade accumulator (the candidate leading/order-flow feed for the
        # late-window sniper study). Recorded only so the offline kill-bar analyzer can
        # test a bot-FORMABLE signal against the resolution venue (Coinbase). None-safe.
        self.binance_trades = binance_trades
        # L1 stamping deps. signal_engine/indicator_engine must be DEDICATED
        # instances (same config as live, but never the trading loop's own —
        # compute_probability mutates engine state the ghost path reads between
        # evaluate() and ghost-record time). None-safe: columns stay NULL.
        self.binance_feed = binance_feed
        self.indicator_engine = indicator_engine
        self.signal_engine = signal_engine
        self.binance_depth = binance_depth
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
        await self._add_appended_columns()
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

    # New columns are APPENDED (schema is immutable truth — columns added at the
    # end, read order follows DB order). Existing rows get NULL; analyzers filter
    # on the relevant column being NOT NULL (post-extension data only).
    _APPENDED_COLUMNS = (
        ("binance_price", "REAL"),
        ("binance_cvd_10s", "REAL"),
        ("binance_cvd_30s", "REAL"),
        ("atr", "REAL"),
        ("model_prob_up", "REAL"),
        # Full capture of everything already flowing through the process —
        # the pivot-research corpus. All None-on-cold, never 0.0 stand-ins.
        ("chainlink_price", "REAL"),     # the RESOLUTION venue's live price
        ("chainlink_age_s", "REAL"),
        ("book_age_up_s", "REAL"),       # CLOB WS book staleness per sample —
        ("book_age_down_s", "REAL"),     # the sniper's precondition, quantified
        ("coinbase_bid", "REAL"),
        ("coinbase_ask", "REAL"),
        ("coinbase_cvd_10s", "REAL"),    # resolution-venue flow at path cadence
        ("coinbase_cvd_30s", "REAL"),
        ("bid_sz_up", "REAL"),           # shares at the touch, both tokens —
        ("ask_sz_up", "REAL"),           # bounds FOK fillable notional
        ("bid_sz_down", "REAL"),
        ("ask_sz_down", "REAL"),
        ("depth20_bid_usd", "REAL"),     # Binance book pressure, side-split
        ("depth20_ask_usd", "REAL"),
    )

    async def _add_appended_columns(self) -> None:
        cur = await self._paths_conn.execute("PRAGMA table_info(window_paths)")
        have = {r["name"] for r in await cur.fetchall()}
        for name, decl in self._APPENDED_COLUMNS:
            if name not in have:
                await self._paths_conn.execute(
                    f"ALTER TABLE window_paths ADD COLUMN {name} {decl}")
        await self._paths_conn.commit()

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
                "INSERT INTO window_paths (window_id, ts, elapsed_s, bid_up, ask_up, "
                "bid_down, ask_down, depth3_bid_up, depth3_ask_up, depth3_bid_down, "
                "depth3_ask_down, coinbase_price, strike, traded) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            data = await self.market_scanner.gamma_events_by_slug(self.http_client, slug)
            if data:
                return self.market_scanner.parse_contract(data[0])
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

    async def _recover_orphan_labels(self) -> None:
        """Boot-time: a restart between a window closing and its label being fetched
        leaves a recorded path with no window_labels row (the queue is in-memory).
        Re-seed any such window still inside the give-up horizon so _label_pass
        relabels it. Older orphans are past Gamma's reliable window and are handled
        by the one-time backfill, not here."""
        if self._paths_conn is None:
            return
        now = time.time()
        try:
            cur = await self._paths_conn.execute("SELECT DISTINCT window_id FROM window_paths")
            path_ids = [r["window_id"] for r in await cur.fetchall()]
            cur = await self.db.conn.execute("SELECT window_id FROM window_labels")
            labeled = {r[0] for r in await cur.fetchall()}
        except Exception as e:
            logger.debug(f"orphan-label recovery scan skipped: {e}")
            return
        seeded = 0
        for wid in path_ids:
            if wid in labeled or wid in self._pending_label:
                continue
            try:
                end_ts = int(wid.rsplit("-", 1)[-1]) + 300.0
            except ValueError:
                continue
            # Only the still-recoverable band: ended >30s ago (resolved) and within
            # the give-up horizon _label_pass honors. Skips the active window and the
            # long-dead backlog.
            if 30 < (now - end_ts) <= _LABEL_GIVE_UP_S:
                self._pending_label[wid] = end_ts
                seeded += 1
        if seeded:
            logger.info(f"orphan-label recovery: re-seeded {seeded} unlabeled window(s) for retry")

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

        cb_fresh = self.coinbase_feed is not None and self.coinbase_feed.state.age_seconds < 5
        cb = self.coinbase_feed.state.price if cb_fresh else None
        strike = (self.chainlink_feed.get_strike(w["window_ts"])
                  if self.chainlink_feed else None)

        # Resolution-venue live price (Chainlink RTDS) + its age.
        cl_px = cl_age = None
        if self.chainlink_feed is not None:
            _age = getattr(self.chainlink_feed, "age_seconds", float("inf"))
            _px = getattr(self.chainlink_feed, "price", 0.0)
            if _px > 0 and _age != float("inf"):
                cl_px = _px
                cl_age = round(_age, 3)

        # CLOB WS book age per token — makes stale/frozen book rows detectable
        # offline (the trading loop gates on 10s; the recorder records instead).
        def _book_age(book: dict) -> float | None:
            ts = book.get("ts")
            return round(now - ts, 3) if ts else None

        # Coinbase BBO + resolution-venue flow (fresh-feed gated; CVD 0.0 is a
        # legitimate balanced-flow value, so it is recorded as-is when fresh).
        cb_bid = cb_ask = cb_cvd10 = cb_cvd30 = None
        if cb_fresh:
            st = self.coinbase_feed.state
            cb_bid = getattr(st, "best_bid", 0.0) or None
            cb_ask = getattr(st, "best_ask", 0.0) or None
            try:
                cb_cvd10 = self.coinbase_feed.get_cvd(10.0)
                cb_cvd30 = self.coinbase_feed.get_cvd(30.0)
            except Exception:
                pass

        def _touch_sz(levels: Any) -> float | None:
            try:
                return float(levels[0]["size"]) if levels else None
            except (KeyError, IndexError, ValueError, TypeError):
                return None

        # Binance top-20 book pressure, side-split (compute_depth_usd's single
        # total destroys direction). None when the depth WS is stale.
        d20_bid = d20_ask = None
        bd = self.binance_depth
        if bd is not None and getattr(bd, "updated_at", 0.0) > 0 and now - bd.updated_at < 5:
            try:
                d20_bid = round(sum(float(l[0]) * float(l[1]) for l in bd.top_bids[:20]), 2) or None
                d20_ask = round(sum(float(l[0]) * float(l[1]) for l in bd.top_asks[:20]), 2) or None
            except (IndexError, ValueError, TypeError):
                d20_bid = d20_ask = None

        # Binance aggTrade leading/order-flow telemetry (None when the feed is cold —
        # never 0.0, so the analyzer can distinguish "no flow" from "stale feed").
        bn_price = bn_cvd10 = bn_cvd30 = None
        acc = getattr(self.binance_trades, "accumulator", None)
        if acc is not None:
            try:
                if acc.latest_age_s < 5:
                    bn_price = acc.latest_price or None
                    bn_cvd10 = acc.get_cvd(10.0)
                    bn_cvd30 = acc.get_cvd(30.0)
            except Exception:
                pass

        # Live-L1 stamp (same math + config as the trading engine, dedicated
        # instance) so offline harnesses can apply the exact live edge floor.
        # None when any input is cold — never a 0.0 stand-in.
        atr_v = prob_up_v = None
        if (self.binance_feed is not None and self.indicator_engine is not None
                and cb is not None and strike is not None):
            _atr_d: dict[str, Any] = {}
            try:
                _atr_d = self.indicator_engine.compute_all(
                    self.binance_feed.buffer).get("atr", {})
                atr_v = _atr_d.get("atr") or None
            except Exception:
                atr_v = None
            # Separate guard: a prob-only failure must not null the computed ATR
            # (that would masquerade as an ATR-feed outage in the corpus).
            if self.signal_engine is not None and atr_v:
                try:
                    prob_up_v = round(self.signal_engine.compute_probability(
                        cb, strike, max(0.0, 300.0 - elapsed), atr_v,
                        closes=self.binance_feed.buffer.get_closes(),
                        atr_candle_ts=_atr_d.get("candle_ts")), 4)
                except Exception:
                    prob_up_v = None

        self._rows.append((
            w["market_id"], round(now, 3), round(elapsed, 1),
            _f(bba_up, "best_bid"), _f(bba_up, "best_ask"),
            _f(bba_dn, "best_bid"), _f(bba_dn, "best_ask"),
            _top3_usd(book_up.get("bids") or []), _top3_usd(book_up.get("asks") or []),
            _top3_usd(book_dn.get("bids") or []), _top3_usd(book_dn.get("asks") or []),
            cb, strike,
            1 if w["market_id"] in self._traded else 0,
            bn_price, bn_cvd10, bn_cvd30,
            atr_v, prob_up_v,
            cl_px, cl_age,
            _book_age(book_up), _book_age(book_dn),
            cb_bid, cb_ask, cb_cvd10, cb_cvd30,
            _touch_sz(book_up.get("bids")), _touch_sz(book_up.get("asks")),
            _touch_sz(book_dn.get("bids")), _touch_sz(book_dn.get("asks")),
            d20_bid, d20_ask,
        ))

    async def _flush(self) -> None:
        if not self._rows:
            return
        rows, self._rows = self._rows, []
        try:
            await self._paths_conn.executemany(
                "INSERT INTO window_paths (window_id, ts, elapsed_s, bid_up, ask_up, "
                "bid_down, ask_down, depth3_bid_up, depth3_ask_up, depth3_bid_down, "
                "depth3_ask_down, coinbase_price, strike, traded, "
                "binance_price, binance_cvd_10s, binance_cvd_30s, atr, model_prob_up, "
                "chainlink_price, chainlink_age_s, book_age_up_s, book_age_down_s, "
                "coinbase_bid, coinbase_ask, coinbase_cvd_10s, coinbase_cvd_30s, "
                "bid_sz_up, ask_sz_up, bid_sz_down, ask_sz_down, "
                "depth20_bid_usd, depth20_ask_usd) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            await self._paths_conn.commit()
            self.rows_written += len(rows)
        except Exception as e:
            logger.warning(f"window_paths flush failed ({len(rows)} rows): {e}")

    async def run(self) -> None:
        self._running = True
        await self.ensure_tables()
        await self._recover_orphan_labels()
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
            # 1 Hz baseline, burst to ~5 Hz in the final 45s — the late-window sniper
            # study needs sub-second ask/Coinbase/Binance resolution to model a 135ms
            # FOK fill; 1 Hz averages the sweep away (the dead-naive-sniper trap).
            w = self._window
            late = w is not None and 255 <= (time.time() - w["window_ts"]) <= 300
            await asyncio.sleep(0.2 if late else 1.0)

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


def cleanup_job(db: Any, retention_days: int = 90):
    """Nightly retention sweep on window_paths (the plan's rolling 90 days)."""
    async def _job() -> dict[str, Any]:
        import aiosqlite
        cutoff = time.time() - retention_days * 86400
        async with aiosqlite.connect(str(PATHS_DB)) as conn:
            await conn.execute("PRAGMA busy_timeout=15000")
            try:
                cur = await conn.execute("DELETE FROM window_paths WHERE ts < ?", (cutoff,))
                await conn.commit()
                return {"rows_deleted": cur.rowcount}
            except aiosqlite.OperationalError:
                return {"rows_deleted": 0}
    return _job
