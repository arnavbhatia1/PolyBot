from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

_ET = ZoneInfo("America/New_York")

class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path: str = db_path
        self.conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                question TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size REAL NOT NULL,
                signal_score REAL NOT NULL,
                signal_strength TEXT NOT NULL,
                ev_at_entry REAL NOT NULL,
                exit_target REAL NOT NULL,
                stop_loss REAL NOT NULL,
                entry_timestamp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                exit_price REAL,
                exit_timestamp TEXT,
                log_return REAL,
                weight_version TEXT NOT NULL,
                indicator_snapshot TEXT,
                fee_rate REAL,
                shares_held REAL
            );

            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                size REAL NOT NULL,
                signal_score REAL NOT NULL,
                signal_strength TEXT NOT NULL,
                ev_at_entry REAL NOT NULL,
                log_return REAL NOT NULL,
                weight_version TEXT NOT NULL,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bankroll (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                amount REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS peak_bankroll (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                amount REAL NOT NULL
            );
        """)
        # Migrate existing DBs: add missing columns to positions and trade_history
        cursor = await self.conn.execute("PRAGMA table_info(positions)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "fee_rate" not in cols:
            await self.conn.execute("ALTER TABLE positions ADD COLUMN fee_rate REAL")
        if "shares_held" not in cols:
            await self.conn.execute("ALTER TABLE positions ADD COLUMN shares_held REAL")
        cursor = await self.conn.execute("PRAGMA table_info(trade_history)")
        th_cols = {row[1] for row in await cursor.fetchall()}
        if "pnl" not in th_cols:
            await self.conn.execute("ALTER TABLE trade_history ADD COLUMN pnl REAL DEFAULT 0")
        if "fees" not in th_cols:
            await self.conn.execute("ALTER TABLE trade_history ADD COLUMN fees REAL DEFAULT 0")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def get_tables(self) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def open_position(
        self,
        market_id: str,
        question: str,
        side: str,
        entry_price: float,
        size: float,
        signal_score: float,
        signal_strength: str,
        ev_at_entry: float,
        exit_target: float,
        stop_loss: float,
        weight_version: str,
        indicator_snapshot: str = "",
        fee_rate: float | None = None,
        shares_held: float | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.conn.execute(
            """INSERT INTO positions
            (market_id, question, side, entry_price, size, signal_score,
             signal_strength, ev_at_entry, exit_target, stop_loss,
             entry_timestamp, status, weight_version, indicator_snapshot,
             fee_rate, shares_held)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (market_id, question, side, entry_price, size, signal_score,
             signal_strength, ev_at_entry, exit_target, stop_loss,
             now, weight_version, indicator_snapshot, fee_rate, shares_held),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_open_positions(self) -> list[dict[str, Any]]:
        """Returns positions that need management: both 'open' (active) and 'pending_resolution' (expired, awaiting Gamma)."""
        cursor = await self.conn.execute(
            "SELECT * FROM positions WHERE status IN ('open', 'pending_resolution')"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_pending_resolution(self, position_id: int) -> None:
        """Mark an expired position as pending resolution — doesn't count against max_concurrent_positions."""
        await self.conn.execute(
            "UPDATE positions SET status='pending_resolution' WHERE id=?",
            (position_id,),
        )
        await self.conn.commit()

    async def close_position(self, position_id: int, exit_price: float, log_return: float,
                             pnl: float = 0.0, fees: float = 0.0) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            "UPDATE positions SET status='closed', exit_price=?, exit_timestamp=?, log_return=? WHERE id=?",
            (exit_price, now, log_return, position_id),
        )
        cursor = await self.conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        )
        pos = dict(await cursor.fetchone())
        await self.conn.execute(
            """INSERT INTO trade_history
            (position_id, market_id, question, side, entry_price, exit_price, size,
             signal_score, signal_strength, ev_at_entry, log_return,
             weight_version, entry_timestamp, exit_timestamp, pnl, fees)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["id"], pos["market_id"], pos["question"], pos["side"],
             pos["entry_price"], exit_price, pos["size"],
             pos["signal_score"], pos["signal_strength"], pos["ev_at_entry"],
             log_return, pos["weight_version"], pos["entry_timestamp"], now, pnl, fees),
        )
        await self.conn.commit()

    async def has_position_for_market(self, market_id: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id=? AND status IN ('open', 'pending_resolution')",
            (market_id,),
        )
        row = await cursor.fetchone()
        return row[0] > 0

    async def get_open_position_count(self) -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open'"
        )
        row = await cursor.fetchone()
        return row[0]

    async def get_trade_history(self, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM trade_history ORDER BY exit_timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_day_stats(self, date_str: str) -> tuple[int, int, float, float]:
        """Return (wins, losses, fees, pnl_sum) for a given trading day (ET date string).

        Converts the ET date to a UTC range so trades timestamped in UTC are
        correctly bucketed into the Eastern trading day.
        """
        day_start_et = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_ET)
        day_end_et = day_start_et + timedelta(days=1)
        utc_start = day_start_et.astimezone(timezone.utc).isoformat()
        utc_end = day_end_et.astimezone(timezone.utc).isoformat()

        cursor = await self.conn.execute(
            "SELECT pnl, fees, exit_price, entry_price FROM trade_history "
            "WHERE exit_timestamp >= ? AND exit_timestamp < ?",
            (utc_start, utc_end),
        )
        rows = await cursor.fetchall()
        wins = losses = 0
        total_fees = 0.0
        total_pnl = 0.0
        for row in rows:
            pnl_val = row[0]
            fee_val = row[1] or 0.0
            exit_p = row[2]
            entry_p = row[3]
            total_fees += fee_val
            total_pnl += (pnl_val or 0.0)
            # Use stored pnl when available; fall back to price comparison for old rows
            if pnl_val is not None and pnl_val != 0:
                win = pnl_val > 0
            else:
                win = exit_p > entry_p
            if win:
                wins += 1
            else:
                losses += 1
        return wins, losses, total_fees, total_pnl

    async def get_trade_stats(self) -> tuple[int, float]:
        """Return (total_trades, win_rate) for bankroll acceleration."""
        cursor = await self.conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN exit_price > entry_price THEN 1 ELSE 0 END) "
            "FROM trade_history"
        )
        row = await cursor.fetchone()
        total = row[0] or 0
        wins = row[1] or 0
        return total, (wins / total if total > 0 else 0.0)

    async def get_avg_edge(self) -> float:
        """Average edge at entry across all trades, for uncertainty discount."""
        cursor = await self.conn.execute(
            "SELECT AVG(ev_at_entry) FROM trade_history WHERE ev_at_entry > 0"
        )
        row = await cursor.fetchone()
        return row[0] if row[0] is not None else 0.0

    async def set_bankroll(self, amount: float) -> None:
        await self.conn.execute(
            "INSERT INTO bankroll (id, amount) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount",
            (amount,),
        )
        await self.conn.commit()

    async def get_bankroll(self) -> float:
        cursor = await self.conn.execute("SELECT amount FROM bankroll WHERE id=1")
        row = await cursor.fetchone()
        return row[0] if row else 0.0

    async def set_peak_bankroll(self, amount: float) -> None:
        await self.conn.execute(
            "INSERT INTO peak_bankroll (id, amount) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount",
            (amount,),
        )
        await self.conn.commit()

    async def get_peak_bankroll(self) -> float | None:
        cursor = await self.conn.execute("SELECT amount FROM peak_bankroll WHERE id=1")
        row = await cursor.fetchone()
        return row[0] if row else None
