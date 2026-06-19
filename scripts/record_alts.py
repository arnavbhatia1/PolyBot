"""Standalone alt-coin window-path + tape recorder (edge-hunt round 4 data collection).

BTC-5m up/down is proven efficient (no edge across 46 agents). The live-book scan
found the alt up/down markets are 2-4x LESS efficient (SOL 2c / XRP 3c / DOGE 4c /
BNB 4c ATM spread vs BTC 1c) — the one structural condition under which the
mechanism that killed BTC (bid == P(win), <135ms repricing) might not hold. This
collects the data to re-run the hunt on them.

FULLY ISOLATED from the live BTC bot — its own WS connection, its own DBs
(alt_window_paths.db / alt_recordings.db) and its own tape dir (recordings/alts/),
so it shares zero state with the trading process and cannot stall it. Reuses
WindowPathRecorder with a per-symbol Coinbase ticker feed (SOL/XRP/DOGE-USD) so
coinbase_price is captured at 1 Hz for the latency-edge test; BNB has no Coinbase
listing so its coinbase_price stays NULL. chainlink_feed=None -> the strike column
stays NULL, but the per-window strike is recoverable as coinbase_price at window
open (elapsed~=0) and confirmed by the resolution label.

Run standalone (or supervise like box_arb_monitor.py):
    python scripts/record_alts.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Redirect window_paths storage to an isolated DB BEFORE importing the recorder
# (PATHS_DB is read at connect time) so alt rows never touch the BTC bot's
# window_paths.db. Labels go to a separate alt DB too (see _LabelsDB below).
import polybot.recording as recording  # noqa: E402

recording.PATHS_DB = _ROOT / "polybot" / "db" / "alt_window_paths.db"

from polybot.recording import WindowPathRecorder, TapeRecorder, RECORDINGS_DIR  # noqa: E402
from polybot.feeds.clob_ws import ClobWebSocket  # noqa: E402
from polybot.feeds.market_scanner import BTCMarketScanner as MarketScanner  # noqa: E402
from polybot.feeds.coinbase_feed import CoinbaseFeed  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("record_alts")

# The less-efficient (wider-spread) up/down markets the hunt should target. BTC/ETH
# (1c, efficient) are excluded — BTC is already the proven-dead reference.
SYMBOLS = ["sol", "xrp", "doge", "bnb"]
# Coinbase ticker product per symbol -> fills the coinbase_price column at 1 Hz so
# the latency-edge test (z_lead = (spot-strike)/(rv*sqrt(T_rem))) is computable on
# the alts. The strike is recoverable as the spot at window open (elapsed~=0) and
# confirmed by the resolution label. BNB is NOT on Coinbase (Binance coin) -> no
# spot feed -> coinbase_price stays NULL for bnb (excluded from the latency test).
COINBASE_PRODUCT = {"sol": "SOL-USD", "xrp": "XRP-USD", "doge": "DOGE-USD", "bnb": None}
ALT_LABELS_DB = _ROOT / "polybot" / "db" / "alt_recordings.db"
ALT_TAPE_DIR = RECORDINGS_DIR / "alts"


class _LabelsDB:
    """Minimal db shim: WindowPathRecorder only touches ``.conn`` for window_labels.
    On this fresh isolated DB _migrate_paths_out_of_main_db no-ops (no window_paths
    table to read), so nothing from the BTC DBs is touched."""

    def __init__(self, conn) -> None:
        self.conn = conn


async def main() -> None:
    import aiosqlite
    import httpx

    ALT_LABELS_DB.parent.mkdir(parents=True, exist_ok=True)
    http = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "record_alts/1.0"})

    ws = ClobWebSocket()
    await ws.start()
    tape = TapeRecorder(dir_path=ALT_TAPE_DIR)
    ws.on_trade = tape.on_trade  # captures every subscribed alt token's prints

    recorders: list[WindowPathRecorder] = []
    conns = []
    cb_feeds: list[CoinbaseFeed] = []
    for sym in SYMBOLS:
        conn = await aiosqlite.connect(str(ALT_LABELS_DB))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=15000")
        conns.append(conn)
        scanner = MarketScanner(symbol=sym)
        cb_feed = None
        product = COINBASE_PRODUCT.get(sym)
        if product:
            cb_feed = CoinbaseFeed(product_id=product)
            await cb_feed.start()
            cb_feeds.append(cb_feed)
        recorders.append(
            WindowPathRecorder(_LabelsDB(conn), ws, cb_feed, None, scanner, http))

    logger.info("alt recorder starting: symbols=%s  paths=%s  labels=%s  tape=%s",
                SYMBOLS, recording.PATHS_DB.name, ALT_LABELS_DB.name, ALT_TAPE_DIR)

    async def tape_flush_loop() -> None:
        # Alt volume is low, so the size-triggered flush in on_trade may rarely fire;
        # force a flush every 10s so prints aren't stranded in the buffer.
        while True:
            await asyncio.sleep(10.0)
            tape.flush()

    async def prune_subs_loop() -> None:
        # WindowPathRecorder._discover subscribes each new window's tokens but never
        # unsubscribes closed ones (fine for the BTC bot, whose shared clob_ws is
        # pruned by the trading loop; here nothing prunes -> ~8 tokens/window =
        # ~2300/day, which bloats the reconnect re-subscribe and risks a server drop
        # on a week-long run). Periodically drop every subscribed token that isn't a
        # currently-active window's token. Done HERE (not in recording.py) so the
        # live BTC recorder — where unsubscribing could pull a token the trading loop
        # still needs for resolution monitoring — is left untouched.
        while True:
            await asyncio.sleep(120.0)
            active: set[str] = set()
            for r in recorders:
                w = r._window
                if w:
                    active.update((w["token_up"], w["token_down"]))
            stale = [t for t in list(ws._subscribed_ids) if t not in active]
            if stale:
                try:
                    await ws.unsubscribe(stale)
                    logger.info("pruned %d stale token subscriptions (%d active)",
                                len(stale), len(active))
                except Exception as e:
                    logger.warning("subscription prune failed: %s", e)

    async def status_loop() -> None:
        while True:
            await asyncio.sleep(60.0)
            logger.info("alt recorder: path_rows=%d labels=%d tape_buf=%d subscribed=%d",
                        sum(r.rows_written for r in recorders),
                        sum(r.labels_written for r in recorders),
                        len(tape._buf), len(ws._subscribed_ids))

    # Sequentially create each recorder's tables/connections BEFORE the concurrent
    # run loops: ensure_tables switches the shared paths DB to WAL before it sets
    # busy_timeout, so four tasks racing that pragma would hit "database is locked".
    # Pre-running it serially (idempotent; run() re-enters it as a no-op) avoids the
    # race and lets busy_timeout cover all later concurrent flushes.
    for r in recorders:
        await r.ensure_tables()

    # Persist each window's Up/Down token ids so the latency-test harness can do the
    # EXACT ms-tape fill-check (window_paths stores only the slug; the tape has only
    # token ids — without this map the realistic edge can only be book-bracketed with
    # ~1-2% error). One table in the (isolated) labels DB; first conn owns the DDL.
    await conns[0].execute(
        "CREATE TABLE IF NOT EXISTS window_tokens ("
        "window_id TEXT PRIMARY KEY, token_up TEXT, token_down TEXT)")
    await conns[0].commit()

    async def token_map_loop() -> None:
        while True:
            await asyncio.sleep(15.0)
            for rec, conn in zip(recorders, conns):
                w = rec._window
                if w and w.get("token_up") and w.get("token_down"):
                    try:
                        await conn.execute(
                            "INSERT OR IGNORE INTO window_tokens VALUES (?,?,?)",
                            (w["market_id"], w["token_up"], w["token_down"]))
                        await conn.commit()
                    except Exception:
                        pass

    tasks = [asyncio.create_task(r.run()) for r in recorders]
    tasks.append(asyncio.create_task(token_map_loop()))
    tasks.append(asyncio.create_task(tape_flush_loop()))
    tasks.append(asyncio.create_task(prune_subs_loop()))
    tasks.append(asyncio.create_task(status_loop()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for r in recorders:
            await r.stop()
        tape.flush()
        for f in cb_feeds:
            try:
                await f.stop()
            except Exception:
                pass
        for c in conns:
            await c.close()
        await ws.close()
        await http.aclose()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("alt recorder stopped")
