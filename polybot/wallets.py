"""Phase 4: wallet fingerprinting — the learning layer that compounds.

Polymarket's data-api tags every trade with the taker's proxy wallet. The 5-min
casino has a recurring cast; per-wallet markout against window resolution sorts
them into donor / noise / sharp. This is counterparty information — not a public
BTC feature — so it does not fight the no-entry-edge theorem, and the tables get
more accurate every night forever.

Nightly job: for each window labeled in the last day, fetch its taker tape from
the data-api, score each trade against the resolution, fold into per-wallet
stats. ``wallet_stats`` is write-only today — the originally-specced routing
(skip resting exits against sharp counterparties) is infeasible on the live
anonymous L2 book (no maker/taker identity pre-post), so no decision-time reader
exists. This remains pure accumulation; see tasks/todo.md Phase 4 for the
realizable post-fill regime-gate alternative.

Storage is split by size: the per-trade tape (~3k prints/window — millions of
rows) lives in its own gitignored DB (``polybot/db/wallet_tape.db``); only the
small ``wallet_stats`` aggregate sits in the per-mode DB the nightly script
commits to git.

Tables:
  wallet_tape.db: wallet_trades(window_id, wallet, token, side, price, size, ts, won)
                  wallet_ingest_log(window_id PRIMARY KEY, n_trades, ingested_at)
  per-mode DB:    wallet_stats(wallet PRIMARY KEY, n_trades, n_won, stake_usd,
                               pnl_usd, classification, updated_at)
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
TAPE_DB_PATH: Path = Path(__file__).resolve().parent / "db" / "wallet_tape.db"

# Classification bars: per-$ resolution markout with a minimum history.
MIN_TRADES_TO_CLASSIFY = 20
SHARP_MARKOUT = +0.03    # earns ≥3c/$1 staked vs resolution
DONOR_MARKOUT = -0.03    # loses ≥3c/$1 staked


async def open_tape_db(path: Path | None = None) -> aiosqlite.Connection:
    """The gitignored per-trade tape DB (caller closes)."""
    conn = await aiosqlite.connect(str(path or TAPE_DB_PATH))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=15000")
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS wallet_trades (
            window_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            token TEXT,
            side TEXT,
            price REAL,
            size REAL,
            ts REAL,
            won INTEGER,
            PRIMARY KEY (window_id, wallet, ts, price, size)
        );
        CREATE TABLE IF NOT EXISTS wallet_ingest_log (
            window_id TEXT PRIMARY KEY,
            n_trades INTEGER,
            ingested_at REAL
        );
    """)
    await conn.commit()
    return conn


async def ensure_stats_table(db: Any) -> None:
    """wallet_stats in the per-mode DB (small; write-only — no decision-time reader yet)."""
    await db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS wallet_stats (
            wallet TEXT PRIMARY KEY,
            n_trades INTEGER NOT NULL,
            n_won INTEGER NOT NULL,
            stake_usd REAL NOT NULL,
            pnl_usd REAL NOT NULL,
            classification TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    await db.conn.commit()


async def _fetch_window_trades(http_client: Any, market_scanner: Any,
                               window_id: str) -> list[dict[str, Any]]:
    """Taker trades for one window via data-api (resolved by slug → conditionId)."""
    try:
        resp = await http_client.get(f"{market_scanner.GAMMA_API}/events",
                                     params={"slug": window_id})
        resp.raise_for_status()
        data = resp.json()
        ev = data[0] if isinstance(data, list) and data else data
        markets = ev.get("markets") or []
        condition_id = (markets[0] or {}).get("conditionId", "") if markets else ""
        if not condition_id:
            return []
        trades: list[dict[str, Any]] = []
        offset = 0
        while True:
            r = await http_client.get(f"{DATA_API}/trades",
                                      params={"market": condition_id,
                                              "limit": 500, "offset": offset})
            if r.status_code == 400 and offset > 0:
                break  # data-api hard pagination ceiling (~3000) — keep what we have
            r.raise_for_status()
            page = r.json()
            if not isinstance(page, list) or not page:
                break
            trades.extend(page)
            if len(page) < 500 or offset >= 3000:
                break
            offset += 500
        return trades
    except Exception as e:
        logger.debug(f"wallet tape fetch failed for {window_id}: {e}")
        return []


def _won(trade: dict[str, Any], resolved_up: int) -> int | None:
    """Did this taker trade end on the winning side? BUY of the winning outcome
    or SELL of the losing outcome wins."""
    outcome = (trade.get("outcome") or "").lower()
    side = (trade.get("side") or "").upper()
    if outcome not in ("up", "down") or side not in ("BUY", "SELL"):
        return None
    bought_up = (outcome == "up") == (side == "BUY")
    return 1 if bought_up == bool(resolved_up) else 0


def score_rows(window_id: str, resolved_up: int,
               trades: list[dict[str, Any]]) -> list[tuple]:
    """data-api trades → wallet_trades rows (shared by nightly job + backfill)."""
    rows: list[tuple] = []
    for t in trades:
        won = _won(t, resolved_up)
        if won is None:
            continue
        try:
            rows.append((window_id,
                         t.get("proxyWallet") or t.get("maker") or "?",
                         t.get("asset", ""), (t.get("side") or "").upper(),
                         float(t.get("price") or 0), float(t.get("size") or 0),
                         float(t.get("timestamp") or 0), won))
        except (ValueError, TypeError):
            continue
    return rows


async def rebuild_stats(db: Any, tape: aiosqlite.Connection) -> dict[str, int]:
    """Aggregate the tape DB into wallet_stats in the per-mode DB + classify.

    pnl per $1 staked on a BUY at p: win → (1-p) per $ of stake; lose → -1.
    """
    await ensure_stats_table(db)
    cur = await tape.execute("""
        SELECT wallet,
               COUNT(*) AS n_trades,
               SUM(won) AS n_won,
               SUM(price * size) AS stake_usd,
               SUM(CASE WHEN won = 1 THEN (1.0 - price) * size
                        ELSE -price * size END) AS pnl_usd
        FROM wallet_trades
        WHERE side = 'BUY' AND price > 0.01 AND price < 0.99
        GROUP BY wallet
    """)
    agg = await cur.fetchall()
    now = time.time()
    await db.conn.execute("DELETE FROM wallet_stats")
    await db.conn.executemany(
        "INSERT INTO wallet_stats VALUES (?,?,?,?,?,'noise',?)",
        [(r["wallet"], r["n_trades"], r["n_won"], r["stake_usd"], r["pnl_usd"], now)
         for r in agg])
    await db.conn.execute("""
        UPDATE wallet_stats SET classification = CASE
            WHEN n_trades < ? THEN 'noise'
            WHEN pnl_usd / MAX(stake_usd, 1.0) >= ? THEN 'sharp'
            WHEN pnl_usd / MAX(stake_usd, 1.0) <= ? THEN 'donor'
            ELSE 'noise' END
    """, (MIN_TRADES_TO_CLASSIFY, SHARP_MARKOUT, DONOR_MARKOUT))
    await db.conn.commit()

    cur = await db.conn.execute(
        "SELECT classification, COUNT(*) n FROM wallet_stats GROUP BY classification")
    return {r["classification"]: r["n"] for r in await cur.fetchall()}


async def migrate_tape_out_of_main_db(db: Any, tape: aiosqlite.Connection) -> int:
    """One-time: move any wallet_trades/wallet_ingest_log rows that landed in the
    per-mode DB (pre-split) into the tape DB, then drop them from the main DB so
    the nightly git commit stays small."""
    moved = 0
    try:
        cur = await db.conn.execute("SELECT * FROM wallet_trades")
        rows = await cur.fetchall()
    except Exception:
        return 0
    if rows:
        await tape.executemany(
            "INSERT OR IGNORE INTO wallet_trades VALUES (?,?,?,?,?,?,?,?)",
            [tuple(r) for r in rows])
        moved = len(rows)
    try:
        cur = await db.conn.execute("SELECT * FROM wallet_ingest_log")
        log_rows = await cur.fetchall()
        if log_rows:
            await tape.executemany(
                "INSERT OR REPLACE INTO wallet_ingest_log VALUES (?,?,?)",
                [tuple(r) for r in log_rows])
    except Exception:
        pass
    await tape.commit()
    await db.conn.executescript(
        "DROP TABLE IF EXISTS wallet_trades; DROP TABLE IF EXISTS wallet_ingest_log;")
    await db.conn.execute("VACUUM")
    await db.conn.commit()
    return moved


def nightly_wallet_job(db: Any, http_client: Any, market_scanner: Any,
                       lookback_s: float = 26 * 3600):
    """Returns the NightlyScheduler coroutine: ingest yesterday's window tapes
    into the tape DB, rebuild per-wallet stats + classification in the main DB."""
    async def _job() -> dict[str, Any]:
        tape = await open_tape_db()
        try:
            moved = await migrate_tape_out_of_main_db(db, tape)
            if moved:
                logger.info(f"wallet tape migrated out of per-mode DB: {moved} rows")
            cur = await tape.execute("SELECT window_id FROM wallet_ingest_log")
            done = {r["window_id"] for r in await cur.fetchall()}
            cur = await db.conn.execute(
                "SELECT window_id, resolved_up FROM window_labels WHERE labeled_at > ?",
                (time.time() - lookback_s,))
            todo = [r for r in await cur.fetchall() if r["window_id"] not in done]

            n_trades_total = 0
            for row in todo:
                window_id, resolved_up = row["window_id"], row["resolved_up"]
                trades = await _fetch_window_trades(http_client, market_scanner, window_id)
                rows = score_rows(window_id, resolved_up, trades)
                if rows:
                    await tape.executemany(
                        "INSERT OR IGNORE INTO wallet_trades VALUES (?,?,?,?,?,?,?,?)", rows)
                await tape.execute(
                    "INSERT OR REPLACE INTO wallet_ingest_log VALUES (?, ?, ?)",
                    (window_id, len(rows), time.time()))
                await tape.commit()
                n_trades_total += len(rows)

            counts = await rebuild_stats(db, tape)
            return {"windows_ingested": len(todo), "trades_ingested": n_trades_total,
                    "wallets": counts}
        finally:
            await tape.close()
    return _job
