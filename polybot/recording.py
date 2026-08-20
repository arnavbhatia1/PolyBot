"""Window-path + CLOB-tape recorders.

WindowPathRecorder: 1 Hz state of EVERY 5-min window (~288/day), traded or not
— self-discovering, self-labeling from Gamma. The sniper kill-bar feed and the
research corpus.
TapeRecorder: every CLOB trade print to a daily JSONL (gitignored — recordings
must never enter the nightly memory/ commit). Research rule: a resting-order
shadow "fills" only when the tape prints through it.
Both are write-behind: rows buffer in memory and flush in batches, so the
trading loop never waits on disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.paths import MEMORY_DIR

logger = logging.getLogger(__name__)

RECORDINGS_DIR: Path = MEMORY_DIR / "recordings"
# Path rows (~86k/day) live in their own gitignored DB so the nightly commit of
# the per-mode DB stays small; window_labels (tiny) stay in the per-mode DB.
PATHS_DB: Path = Path(__file__).resolve().parent / "db" / "window_paths.db"

_FLUSH_EVERY_S = 10.0
_TAPE_FLUSH_ROWS = 200
_LABEL_RETRY_S = 60.0
_LABEL_GIVE_UP_S = 2400.0  # stop asking Gamma 40 min after window end


def _top3_usd(levels: list[dict[str, Any]]) -> float | None:
    """USD notional of the top-3 levels; None when the book is missing/unparseable.

    None, never 0.0 — 0.0 would read as real zero liquidity downstream.
    """
    if not levels:
        return None
    try:
        return round(sum(float(l["price"]) * float(l["size"]) for l in levels[:3]), 2)
    except (KeyError, ValueError, TypeError):
        return None


class WindowPathRecorder:
    """Samples the active 5-min window at 1 Hz (5 Hz in the final 45s).

    The corpus behind the head-start gauge (analyze_twap_lock.open_gap_read),
    the label flow, and future pivot research; event-true FOK modeling lives
    in the micro-tape (analyze_twap_lock.py replays that, not this).

    Tables (created on first run):
      window_paths (PATHS_DB, gitignored): window_id, ts, elapsed_s, bid/ask
                   both sides, top-3 depths, coinbase_price, strike, traded,
                   + appended columns (see _APPENDED_COLUMNS)
      window_labels (per-mode DB): window_id PRIMARY KEY, resolved_up,
                   final_price, price_to_beat, labeled_at, token_up/down

    atr + model_prob_up stamp the live L1 view per sample — corpus-only
    fields (None on cold feeds, never 0.0); no decision path reads them.
    """

    def __init__(self, db: Any, clob_ws: Any, chainlink_feed: Any,
                 market_scanner: Any, http_client: Any,
                 ) -> None:
        self.db = db
        self.clob_ws = clob_ws
        self.chainlink_feed = chainlink_feed
        self.market_scanner = market_scanner
        self.http_client = http_client
        self._window: dict[str, Any] | None = None
        self._discovering: int = 0          # window_ts a discovery task is running for
        self._tasks: set[asyncio.Task] = set()  # strong refs so GC can't drop a
        self._traded: set[str] = set()          # mid-await discovery/label pass
        self._pending_label: dict[str, float] = {}  # window_id -> window_end_ts
        self._window_tokens: dict[str, tuple[str, str]] = {}  # window_id -> (token_up, token_down)
        self._last_label_run = 0.0
        self._rows: list[tuple] = []
        self._running = False
        self._paths_conn = None
        # Set by main: called ONCE per served-vs-captured resolution mismatch
        # (window_id, kind, served, captured). The per-window SOURCE hard gate
        # — the chain invariant cannot see Polymarket swapping the resolution
        # stream (both served values move together); this can, the same window.
        self.on_source_mismatch: Any = None
        self._source_mismatch_fired = False
        self.source_unchecked = 0       # windows the gate could not compare

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
        # Persist the window's token ids with its label: the tape/micro-tape
        # record by TOKEN — without this map, offline research can only join
        # the subset of windows that produced fills/ghosts.
        cur = await self.db.conn.execute("PRAGMA table_info(window_labels)")
        cols = {row[1] for row in await cur.fetchall()}
        if "token_up" not in cols:
            await self.db.conn.execute("ALTER TABLE window_labels ADD COLUMN token_up TEXT")
        if "token_down" not in cols:
            await self.db.conn.execute("ALTER TABLE window_labels ADD COLUMN token_down TEXT")
        await self.db.conn.commit()
        await self._migrate_paths_out_of_main_db()

    # Columns are APPENDED only — schema is immutable truth, read order follows
    # DB order. Existing rows get NULL; analyzers filter NOT NULL.
    _APPENDED_COLUMNS = (
        ("binance_price", "REAL"),
        ("binance_cvd_10s", "REAL"),
        ("binance_cvd_30s", "REAL"),
        ("atr", "REAL"),
        ("model_prob_up", "REAL"),
        # Full capture of everything already flowing through the process — the
        # pivot-research corpus. All None-on-cold, never 0.0 stand-ins.
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
        ("strike_trusted", "INTEGER"),   # 1 = the sampled strike is the trusted boundary
                                         # capture; 0 = an untrusted capture or the live
                                         # fallback. Research must be able to tell.
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
        """One-time move of window_paths rows out of the per-mode DB, keeping the nightly commit small."""
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

    def _spawn(self, coro) -> None:
        """create_task with a strong ref: event loops hold tasks weakly, and an
        unreferenced task can be GC'd mid-await, silently dropping a window's
        discovery or a label pass."""
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

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
        # Kept past rotation so the post-close label write can persist the
        # token map; pruned alongside the label queue.
        self._window_tokens[contract.get("slug", slug)] = (token_up, token_down)
        if len(self._window_tokens) > 50:
            for k in list(self._window_tokens)[:-25]:
                if k not in self._pending_label and (self._window is None or k != self._window["market_id"]):
                    self._window_tokens.pop(k, None)
        try:
            await self.clob_ws.subscribe([token_up, token_down])
        except Exception as e:
            logger.debug(f"recorder subscribe failed: {e}")

    async def _recover_orphan_labels(self) -> None:
        """Boot-time re-seed of unlabeled windows still inside the give-up horizon.

        The label queue is in-memory: a restart between window close and label
        fetch leaves a recorded path with no window_labels row. Older orphans
        are past Gamma's reliable window — backfill territory, not here.
        """
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
            # Recoverable band only: ended >30s ago (resolved) and inside the
            # give-up horizon — skips the active window and the long-dead backlog.
            if 30 < (now - end_ts) <= _LABEL_GIVE_UP_S:
                self._pending_label[wid] = end_ts
                seeded += 1
        if seeded:
            logger.debug(f"orphan-label recovery: re-seeded {seeded} unlabeled window(s) for retry")

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
            tok_up, tok_down = self._window_tokens.pop(market_id, (None, None))
            if tok_up is None:
                # restart-orphaned window: the in-memory map is gone, but the
                # just-fetched contract carries the ids
                tok_up = contract.get("token_id_up") or None
                tok_down = contract.get("token_id_down") or None
            try:
                await self.db.conn.execute(
                    "INSERT OR REPLACE INTO window_labels "
                    "(window_id, resolved_up, final_price, price_to_beat, labeled_at, "
                    "token_up, token_down) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (market_id, 1 if fp >= ptb else 0, fp, ptb, now, tok_up, tok_down))
                await self.db.conn.commit()
            except Exception as e:
                logger.warning(f"window label write failed for {market_id}: {e}")
            self._check_resolution_source(market_id, fp, ptb)

    def _check_resolution_source(self, market_id: str, fp: float, ptb: float) -> None:
        """Served strike/final vs our TRUSTED stream captures, every window.

        A mismatch means Polymarket resolves on a stream we are not reading —
        the strike, projection, and post-close winner checks are all fiction
        until the feed is re-pointed (the 08-14 30s->60s swap ran undetected
        for 4 days because the chain invariant is source-internal). Trusted
        captures only: a delivery hole must not read as a rule change."""
        if self.on_source_mismatch is None or self._source_mismatch_fired:
            return
        try:
            ep = int(market_id.rsplit("-", 1)[-1])
            caps = self.chainlink_feed.boundary_snapshot()
        except Exception as e:
            # Silence here would disable the gate the day the format changes.
            self.source_unchecked += 1
            logger.error("SOURCE CHECK SKIPPED %s — cannot read the window id or "
                         "our captures (%s)", market_id, e)
            return
        compared = False
        for kind, served, b in (("strike", ptb, ep), ("final", fp, ep + 300)):
            cap = caps.get(b)
            if served is None or cap is None:
                continue
            compared = True
            if abs(served - cap) > 0.005:
                self._source_mismatch_fired = True
                try:
                    self.on_source_mismatch(market_id, kind, served, cap)
                except Exception:
                    logger.exception("source-mismatch handler failed (mismatch stands)")
                return
        if not compared:
            self.source_unchecked += 1
            logger.error("SOURCE CHECK SKIPPED %s — no trusted boundary capture "
                         "to compare", market_id)

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

        cb = None            # Coinbase feed deleted; the column records NULL
        strike = (self.chainlink_feed.get_strike(w["window_ts"])
                  if self.chainlink_feed else None)
        # get_strike serves untrusted captures and the live fallback too, so the
        # corpus records which one it got — nothing else in the row can tell.
        strike_trusted = None
        if strike is not None and self.chainlink_feed is not None:
            strike_trusted = 1 if self.chainlink_feed.strike_reliable(w["window_ts"]) else 0

        # Resolution-venue live price (Chainlink RTDS) + its age
        cl_px = cl_age = None
        if self.chainlink_feed is not None:
            _age = getattr(self.chainlink_feed, "age_seconds", float("inf"))
            _px = getattr(self.chainlink_feed, "price", 0.0)
            if _px > 0 and _age != float("inf"):
                cl_px = _px
                cl_age = round(_age, 3)

        # CLOB book age per token — stale/frozen books detectable offline
        # (the trading loop gates on 10s; the recorder records instead).
        def _book_age(book: dict) -> float | None:
            ts = book.get("ts")
            return round(now - ts, 3) if ts else None

        # Coinbase feed deleted; these columns record NULL by design.
        cb_bid = cb_ask = cb_cvd10 = cb_cvd30 = None

        def _touch_sz(levels: Any) -> float | None:
            try:
                return float(levels[0]["size"]) if levels else None
            except (KeyError, IndexError, ValueError, TypeError):
                return None

        # Binance feeds deleted; these columns record NULL by design.
        d20_bid = d20_ask = None
        bn_price = bn_cvd10 = bn_cvd30 = None
        # L1 model deleted; these columns record NULL by design.
        atr_v = prob_up_v = None

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
            d20_bid, d20_ask, strike_trusted,
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
                "depth20_bid_usd, depth20_ask_usd, strike_trusted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            await self._paths_conn.commit()
        except Exception as e:
            logger.warning(f"window_paths flush failed ({len(rows)} rows): {e}")

    async def run(self) -> None:
        self._running = True
        await self.ensure_tables()
        await self._recover_orphan_labels()
        logger.info("Recording every window for research")
        last_flush = time.time()
        while self._running:
            try:
                window_ts = int(time.time() // 300) * 300
                cur = self._window
                if ((cur is None or cur["window_ts"] != window_ts)
                        and self._discovering != window_ts):
                    self._discovering = window_ts
                    self._spawn(self._discover(window_ts))
                self._sample()
                now = time.time()
                if now - last_flush >= _FLUSH_EVERY_S:
                    await self._flush()
                    last_flush = now
                if now - self._last_label_run >= _LABEL_RETRY_S and self._pending_label:
                    self._last_label_run = now
                    self._spawn(self._label_pass())
            except Exception as e:
                logger.warning(f"window recorder tick failed: {e}")
            # 1 Hz baseline, ~5 Hz in the final 45s: modeling a FOK fill needs
            # sub-second data — 1 Hz averages the sweep away (the dead-naive-sniper trap).
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
    """CLOB trade prints → memory/recordings/tape_YYYY-MM-DD.jsonl (gitignored).

    Writes run on a single-thread executor: flushes fire from the CLOB WS
    callback and print volume peaks exactly when the sniper fires, so the event
    loop must never carry the disk write. One worker keeps appends ordered.
    """

    def __init__(self, dir_path: Path | None = None) -> None:
        self.dir = dir_path or RECORDINGS_DIR
        self._buf: list[str] = []
        self._last_flush = time.time()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tape-writer")

    def on_trade(self, asset_id: str, trade: dict[str, Any]) -> None:
        """Wired as ClobWebSocket.on_trade — must never raise into the feed."""
        try:
            self._buf.append(json.dumps({
                "ts": round(trade.get("timestamp", time.time()), 3),
                "token": asset_id,
                "price": trade.get("price"),
                "size": trade.get("size"),
                "side": trade.get("side"),
                # Exchange-side fields: the exchange's own clock (per-print WS
                # delivery latency, tape-fair pricing) + the served fee rate.
                "ets": trade.get("exchange_ts") or None,
                "fee_bps": trade.get("fee_rate_bps") or None,
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
            self._writer.submit(self._write, buf)
        except RuntimeError:
            self._write(buf)  # executor gone (interpreter shutdown) — write inline

    def _write(self, buf: list[str]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with (self.dir / f"tape_{day}.jsonl").open("a", encoding="utf-8") as f:
                f.write("\n".join(buf) + "\n")
        except Exception as e:
            logger.warning(f"tape flush failed ({len(buf)} prints): {e}")


class MicroTape:
    """Event-driven micro-structure tape → memory/recordings/micro_YYYY-MM-DD.jsonl (gitignored).

    The WindowPathRecorder SAMPLES; fills/kills are decided by the book's exact
    trajectory between samples. This records the events themselves:

      k="b"  every CLOB best-bid/ask CHANGE for subscribed tokens
      k="l"  every Chainlink RTDS report (the sniper's decision clock; feeds
             the projection replay + boundary-gap research)

    b rows only in the final 90s (elapsed >= 210s) to bound volume; l rows
    always (~1 Hz, tiny — the strike-research corpus). Same off-loop
    single-writer pattern as TapeRecorder: callbacks only append to a list,
    the disk write never rides the money path.
    """

    _LATE_ELAPSED_S = 210.0

    def __init__(self, dir_path: Path | None = None) -> None:
        self.dir = dir_path or RECORDINGS_DIR
        self._buf: list[str] = []
        self._last_flush = time.time()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="micro-writer")
        # Rows the RETIRED 30s stream has written this run — the nightly source
        # watch states it, because a zero means that A/B evidence does not exist.
        self.t3_records = 0

    @classmethod
    def _late(cls, ts: float) -> bool:
        return (ts % 300.0) >= cls._LATE_ELAPSED_S

    def on_bba(self, asset_id: str, entry: dict[str, Any]) -> None:
        """Wired as ClobWebSocket.on_bba — must never raise into the feed."""
        try:
            now = time.time()
            if not self._late(now):
                return
            self._buf.append(json.dumps({
                "k": "b", "ts": round(now, 3), "token": asset_id,
                "bid": entry.get("bid"), "ask": entry.get("ask"),
            }))
            self._maybe_flush(now)
        except Exception:
            pass

    def on_twap_report(self, payload_ts: float, value: float,
                       pub_ts: float | None = None) -> None:
        """Wired as ChainlinkFeed.on_twap. Official 60s-TWAP stream (the resolution
        source from 2026-08-14), always recorded with receipt ts so the topic's
        delivery lag stays measurable.

        `pub` = the RTDS envelope's own timestamp (when Polymarket published).
        It splits the ~1.63s observation-to-us lag into Chainlink's upstream
        pipeline (pub − ts, which a direct subscriber also pays) and the relay
        hop (rx − pub). Both stamps are server-side, so that split is immune to
        our clock. Recorded only — nothing decides on it.
        """
        try:
            now = time.time()
            self._buf.append(json.dumps({
                "k": "t", "ts": round(payload_ts, 3), "rx": round(now, 3), "p": value,
                "pub": round(pub_ts, 3) if pub_ts else None,
            }))
            self._maybe_flush(now)
        except Exception:
            pass

    def on_twap30_report(self, payload_ts: float, value: float,
                         pub_ts: float | None = None) -> None:
        """Wired as ChainlinkFeed.on_twap30 — the RETIRED 30s stream, recorded
        only. When Polymarket moves the resolution source again, this is the
        A/B evidence that says which stream the new rule matches (the 08-14
        swap took a day of offline archaeology to identify without it)."""
        try:
            now = time.time()
            self._buf.append(json.dumps({
                "k": "t3", "ts": round(payload_ts, 3), "rx": round(now, 3),
                "p": value,
            }))
            self.t3_records += 1
            self._maybe_flush(now)
        except Exception:
            pass

    def on_bz_tick(self, payload_ts: float, price: float,
                   _pub_ts: float | None = None) -> None:
        """Wired as ChainlinkFeed.on_spot — the RTDS Binance relay. Recorded so
        the bridge delta (the ~2s the crowd sees before our oracle receipt) is
        replayable offline against the same tape."""
        try:
            now = time.time()
            self._buf.append(json.dumps({
                "k": "s", "src": "bz", "ts": round(payload_ts, 3),
                "rx": round(now, 3), "p": price,
            }))
            self._maybe_flush(now)
        except Exception:
            pass

    def on_cl_report(self, payload_ts: float, price: float,
                     pub_ts: float | None = None) -> None:
        """Wired as ChainlinkFeed.on_report.

        payload_ts = the report's own timestamp; receipt time is stamped
        alongside so delivery lag/holes are measurable offline. `pub` is the
        RTDS envelope timestamp — see on_twap_report for why it is recorded.
        """
        try:
            now = time.time()
            self._buf.append(json.dumps({
                "k": "l", "ts": round(payload_ts, 3), "rx": round(now, 3), "p": price,
                "pub": round(pub_ts, 3) if pub_ts else None,
            }))
            self._maybe_flush(now)
        except Exception:
            pass

    def _maybe_flush(self, now: float) -> None:
        if len(self._buf) >= _TAPE_FLUSH_ROWS or now - self._last_flush > _FLUSH_EVERY_S:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        buf, self._buf = self._buf, []
        self._last_flush = time.time()
        try:
            self._writer.submit(self._write, buf)
        except RuntimeError:
            self._write(buf)  # executor gone (interpreter shutdown) — write inline

    def _write(self, buf: list[str]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with (self.dir / f"micro_{day}.jsonl").open("a", encoding="utf-8") as f:
                f.write("\n".join(buf) + "\n")
        except Exception as e:
            logger.warning(f"micro-tape flush failed ({len(buf)} events): {e}")


def compress_recordings_job(level: int = 3):
    """Nightly: gzip every finished tape file (today's stays open for appends).

    The tape is repetitive JSON and compresses ~39x at ~40 MB/s, so a day's
    micro-tape goes 1.9 GB -> ~50 MB in under a minute. That is what makes a
    multi-day research corpus (and recording more than one market) fit the
    45 GB host at all. Readers open .jsonl and .jsonl.gz interchangeably.
    """
    async def _job() -> dict[str, Any]:
        import asyncio as _aio

        def _compress() -> dict[str, Any]:
            import gzip
            import shutil
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            done, saved = 0, 0
            try:
                files = sorted(RECORDINGS_DIR.glob("*.jsonl"))
            except OSError:
                return {"compressed": 0, "mb_saved": 0}
            for f in files:
                if today in f.name:
                    continue                      # still being appended to
                gz = f.with_suffix(".jsonl.gz")
                try:
                    raw = f.stat().st_size
                    tmp = gz.with_suffix(".gz.part")
                    with open(f, "rb") as src, gzip.open(tmp, "wb", compresslevel=level) as dst:
                        shutil.copyfileobj(src, dst, length=1 << 20)
                    tmp.replace(gz)               # atomic: never leave a half file
                    f.unlink()
                    done += 1
                    saved += raw - gz.stat().st_size
                except OSError as e:
                    logger.warning(f"tape compress failed for {f.name}: {e}")
            return {"compressed": done, "mb_saved": round(saved / 1e6)}

        return await _aio.to_thread(_compress)
    return _job


def recordings_cleanup_job(retention_days: int = 30, micro_retention_days: int = 30):
    """Nightly retention sweep on memory/recordings/ (both .jsonl and .jsonl.gz).

    Compression buys the depth: gzipped micro-tape runs ~50 MB/day, so the
    corpus keeps a full month instead of the 7 days raw files forced.
    """
    async def _job() -> dict[str, Any]:
        import asyncio as _aio
        def _sweep() -> int:
            now = time.time()
            cutoff = now - retention_days * 86400
            micro_cutoff = now - micro_retention_days * 86400
            n = 0
            try:
                for f in list(RECORDINGS_DIR.glob("*.jsonl")) + list(RECORDINGS_DIR.glob("*.jsonl.gz")):
                    limit = micro_cutoff if f.name.startswith("micro_") else cutoff
                    try:
                        if f.stat().st_mtime < limit:
                            f.unlink()
                            n += 1
                    except OSError:
                        pass
            except OSError:
                pass
            return n
        return {"recordings_deleted": await _aio.to_thread(_sweep)}
    return _job


def cleanup_job(db: Any, retention_days: int = 90):
    """Nightly retention sweep on window_paths (rolling 90 days)."""
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
