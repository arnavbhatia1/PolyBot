"""Phase 4: wallet fingerprinting — the learning layer that compounds.

Polymarket's data-api tags every trade with the taker's proxy wallet. The 5-min
casino has a recurring cast; per-wallet markout against window resolution sorts
them into donor / noise / sharp. This is counterparty information — not a public
BTC feature — so it does not fight the no-entry-edge theorem, and the tables get
more accurate every night forever.

Nightly job: for each window labeled in the last day, fetch its taker tape from
the data-api, score each trade against the resolution, fold into per-wallet
stats. Routing (skip resting exits against sharp counterparties) deploys with
Phase 1 — until then this is pure accumulation.

Tables (per-mode DB):
  wallet_trades(window_id, wallet, side_token, price, size, ts, won)
  wallet_stats(wallet PRIMARY KEY, n_trades, n_won, stake_usd, pnl_per_usd,
               markout_sum, classification, updated_at)
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"

# Classification bars: per-$ resolution markout with a minimum history.
MIN_TRADES_TO_CLASSIFY = 20
SHARP_MARKOUT = +0.03    # earns ≥3c/$1 staked vs resolution
DONOR_MARKOUT = -0.03    # loses ≥3c/$1 staked


async def ensure_tables(db: Any) -> None:
    await db.conn.executescript("""
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
        CREATE TABLE IF NOT EXISTS wallet_stats (
            wallet TEXT PRIMARY KEY,
            n_trades INTEGER NOT NULL,
            n_won INTEGER NOT NULL,
            stake_usd REAL NOT NULL,
            pnl_usd REAL NOT NULL,
            classification TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wallet_ingest_log (
            window_id TEXT PRIMARY KEY,
            n_trades INTEGER,
            ingested_at REAL
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
            r.raise_for_status()
            page = r.json()
            if not isinstance(page, list) or not page:
                break
            trades.extend(page)
            if len(page) < 500 or offset >= 4500:
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


def nightly_wallet_job(db: Any, http_client: Any, market_scanner: Any,
                       lookback_s: float = 26 * 3600):
    """Returns the NightlyScheduler coroutine: ingest yesterday's window tapes,
    rebuild per-wallet stats + classification."""
    async def _job() -> dict[str, Any]:
        await ensure_tables(db)
        cur = await db.conn.execute("""
            SELECT l.window_id, l.resolved_up FROM window_labels l
            LEFT JOIN wallet_ingest_log g ON g.window_id = l.window_id
            WHERE g.window_id IS NULL AND l.labeled_at > ?
        """, (time.time() - lookback_s,))
        todo = await cur.fetchall()

        n_trades_total = 0
        for row in todo:
            window_id, resolved_up = row["window_id"], row["resolved_up"]
            trades = await _fetch_window_trades(http_client, market_scanner, window_id)
            rows = []
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
            if rows:
                await db.conn.executemany(
                    "INSERT OR IGNORE INTO wallet_trades VALUES (?,?,?,?,?,?,?,?)", rows)
            await db.conn.execute(
                "INSERT OR REPLACE INTO wallet_ingest_log VALUES (?, ?, ?)",
                (window_id, len(rows), time.time()))
            await db.conn.commit()
            n_trades_total += len(rows)

        # Rebuild stats from scratch (cheap at this scale) + classify.
        # pnl per $1 staked on a BUY at p: win → (1-p)/p per share × p = (1-p) per $; lose → -1.
        await db.conn.execute("DELETE FROM wallet_stats")
        await db.conn.execute("""
            INSERT INTO wallet_stats
            SELECT wallet,
                   COUNT(*) AS n_trades,
                   SUM(won) AS n_won,
                   SUM(price * size) AS stake_usd,
                   SUM(CASE WHEN won = 1 THEN (1.0 - price) * size
                            ELSE -price * size END) AS pnl_usd,
                   'noise', ?
            FROM wallet_trades
            WHERE side = 'BUY' AND price > 0.01 AND price < 0.99
            GROUP BY wallet
        """, (time.time(),))
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
        counts = {r["classification"]: r["n"] for r in await cur.fetchall()}
        return {"windows_ingested": len(todo), "trades_ingested": n_trades_total,
                "wallets": counts}
    return _job
