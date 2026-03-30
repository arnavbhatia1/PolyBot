import aiosqlite
from datetime import datetime, timezone

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: aiosqlite.Connection | None = None

    async def initialize(self):
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                question TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size REAL NOT NULL,
                claude_probability REAL NOT NULL,
                claude_confidence TEXT NOT NULL,
                ev_at_entry REAL NOT NULL,
                exit_target REAL NOT NULL,
                stop_loss REAL NOT NULL,
                time_stop TEXT,
                entry_timestamp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                exit_price REAL,
                exit_timestamp TEXT,
                log_return REAL,
                prompt_version TEXT NOT NULL
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
                claude_probability REAL NOT NULL,
                claude_confidence TEXT NOT NULL,
                ev_at_entry REAL NOT NULL,
                log_return REAL NOT NULL,
                prompt_version TEXT NOT NULL,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bankroll (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                amount REAL NOT NULL
            );
        """)
        await self.conn.commit()

    async def close(self):
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
        claude_probability: float,
        claude_confidence: str,
        ev_at_entry: float,
        exit_target: float,
        stop_loss: float,
        prompt_version: str,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.conn.execute(
            """INSERT INTO positions
            (market_id, question, side, entry_price, size, claude_probability,
             claude_confidence, ev_at_entry, exit_target, stop_loss,
             entry_timestamp, status, prompt_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (market_id, question, side, entry_price, size, claude_probability,
             claude_confidence, ev_at_entry, exit_target, stop_loss,
             now, prompt_version),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_open_positions(self) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM positions WHERE status = 'open'"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def close_position(self, position_id: int, exit_price: float, log_return: float):
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
             claude_probability, claude_confidence, ev_at_entry, log_return,
             prompt_version, entry_timestamp, exit_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["id"], pos["market_id"], pos["question"], pos["side"],
             pos["entry_price"], exit_price, pos["size"],
             pos["claude_probability"], pos["claude_confidence"], pos["ev_at_entry"],
             log_return, pos["prompt_version"], pos["entry_timestamp"], now),
        )
        await self.conn.commit()

    async def has_position_for_market(self, market_id: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id=? AND status='open'",
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

    async def get_trade_history(self, limit: int = 50) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM trade_history ORDER BY exit_timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def set_bankroll(self, amount: float):
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
