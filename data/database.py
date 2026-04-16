"""
SQLite database — stores lift logs, notes, and any data the bot needs to persist locally.
Lightweight, no external dependencies.
"""

import aiosqlite
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "data/fitness_bot.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        """Create tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS lifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    exercise TEXT NOT NULL,
                    details TEXT NOT NULL,
                    raw_message TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT UNIQUE NOT NULL,
                    recovery_score REAL,
                    hrv REAL,
                    rhr REAL,
                    sleep_hours REAL,
                    sleep_efficiency REAL,
                    raw_json TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.commit()
        logger.info(f"Database initialized at {self.db_path}")

    async def log_lift(self, date: str, exercise: str, details: str, raw: str = ""):
        """Store a lift log entry."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO lifts (date, exercise, details, raw_message) VALUES (?, ?, ?, ?)",
                (date, exercise, details, raw)
            )
            await db.commit()
        logger.info(f"Lift logged: {exercise} — {details}")

    async def get_recent_lifts(self, days: int = 7) -> list[dict]:
        """Retrieve lift logs from the last N days."""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT date, exercise, details FROM lifts WHERE date >= ? ORDER BY date DESC",
                (since,)
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_lifts_for_exercise(self, exercise: str, limit: int = 20) -> list[dict]:
        """Get history for a specific exercise — useful for tracking progression."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT date, details FROM lifts WHERE LOWER(exercise) LIKE ? ORDER BY date DESC LIMIT ?",
                (f"%{exercise.lower()}%", limit)
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def log_note(self, date: str, content: str):
        """Store a qualitative note (e.g., 'knees felt off today')."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO notes (date, content) VALUES (?, ?)",
                (date, content)
            )
            await db.commit()

    async def get_recent_notes(self, days: int = 7) -> list[dict]:
        """Retrieve notes from the last N days."""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT date, content FROM notes WHERE date >= ? ORDER BY date DESC",
                (since,)
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]
