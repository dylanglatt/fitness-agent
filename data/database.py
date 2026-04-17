"""
SQLite database — stores lift logs, notes, and full WHOOP/Strava history.

History tables exist so the bot can answer trend questions ("how was my HRV
in February?") without paying Claude to re-ingest a year of raw data on every
prompt. The backfill lives in sync_history.py; nightly incremental updates
live in bot/scheduler.py.
"""

import aiosqlite
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "data/fitness_bot.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # ── Schema ───────────────────────────────────────────────────────────────

    async def initialize(self):
        """Create tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            # ── Original tables (unchanged) ─────────────────────────────────
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

            # ── History tables — one row per day / one row per activity ─────
            # WHOOP recovery: keyed by the date the recovery was calculated for
            await db.execute("""
                CREATE TABLE IF NOT EXISTS whoop_recovery (
                    date TEXT PRIMARY KEY,
                    recovery_score REAL,
                    hrv_rmssd_ms REAL,
                    resting_hr REAL,
                    spo2_pct REAL,
                    skin_temp_c REAL,
                    cycle_id TEXT,
                    sleep_id TEXT,
                    raw_json TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # WHOOP sleep: keyed by date (the calendar day the sleep ended)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS whoop_sleep (
                    date TEXT PRIMARY KEY,
                    total_in_bed_hours REAL,
                    total_asleep_hours REAL,
                    sleep_efficiency_pct REAL,
                    sleep_performance_pct REAL,
                    sleep_consistency_pct REAL,
                    disturbance_count INTEGER,
                    rem_hours REAL,
                    sws_hours REAL,
                    light_hours REAL,
                    sleep_id TEXT,
                    raw_json TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # WHOOP cycle (strain): keyed by the calendar day the cycle started
            await db.execute("""
                CREATE TABLE IF NOT EXISTS whoop_cycle (
                    date TEXT PRIMARY KEY,
                    strain REAL,
                    kilojoule REAL,
                    average_hr REAL,
                    max_hr REAL,
                    cycle_id TEXT,
                    raw_json TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # Strava activities: keyed by Strava's activity id (stable across refetch)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS strava_activities (
                    activity_id INTEGER PRIMARY KEY,
                    date TEXT NOT NULL,
                    sport_type TEXT,
                    name TEXT,
                    distance_m REAL,
                    moving_time_s INTEGER,
                    elapsed_time_s INTEGER,
                    total_elevation_gain_m REAL,
                    average_hr REAL,
                    max_hr REAL,
                    average_speed_mps REAL,
                    max_speed_mps REAL,
                    raw_json TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_strava_date ON strava_activities(date)"
            )
            # sync_state: last successful sync timestamp per source. Lets the
            # nightly job ask "what's new since last time?" without refetching
            # the world.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    source TEXT PRIMARY KEY,
                    last_synced_at TEXT,
                    last_record_date TEXT,
                    note TEXT
                )
            """)
            await db.commit()
        logger.info(f"Database initialized at {self.db_path}")

    # ── Lifts / notes (unchanged behaviour) ─────────────────────────────────

    async def log_lift(self, date: str, exercise: str, details: str, raw: str = ""):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO lifts (date, exercise, details, raw_message) VALUES (?, ?, ?, ?)",
                (date, exercise, details, raw),
            )
            await db.commit()
        logger.info(f"Lift logged: {exercise} — {details}")

    async def get_recent_lifts(self, days: int = 7) -> list[dict]:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT date, exercise, details FROM lifts WHERE date >= ? ORDER BY date DESC",
                (since,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_lifts_for_exercise(self, exercise: str, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT date, exercise, details FROM lifts WHERE LOWER(exercise) LIKE ? "
                "ORDER BY date DESC LIMIT ?",
                (f"%{exercise.lower()}%", limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def log_note(self, date: str, content: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO notes (date, content) VALUES (?, ?)", (date, content)
            )
            await db.commit()

    async def get_recent_notes(self, days: int = 7) -> list[dict]:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT date, content FROM notes WHERE date >= ? ORDER BY date DESC",
                (since,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ── WHOOP upserts ───────────────────────────────────────────────────────

    async def upsert_whoop_recovery(self, date: str, row: dict, raw: dict):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO whoop_recovery
                    (date, recovery_score, hrv_rmssd_ms, resting_hr, spo2_pct,
                     skin_temp_c, cycle_id, sleep_id, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(date) DO UPDATE SET
                    recovery_score=excluded.recovery_score,
                    hrv_rmssd_ms=excluded.hrv_rmssd_ms,
                    resting_hr=excluded.resting_hr,
                    spo2_pct=excluded.spo2_pct,
                    skin_temp_c=excluded.skin_temp_c,
                    cycle_id=excluded.cycle_id,
                    sleep_id=excluded.sleep_id,
                    raw_json=excluded.raw_json,
                    updated_at=datetime('now')
                """,
                (
                    date,
                    row.get("recovery_score"),
                    row.get("hrv_rmssd_ms"),
                    row.get("resting_hr"),
                    row.get("spo2_pct"),
                    row.get("skin_temp_c"),
                    row.get("cycle_id"),
                    row.get("sleep_id"),
                    json.dumps(raw),
                ),
            )
            await db.commit()

    async def upsert_whoop_sleep(self, date: str, row: dict, raw: dict):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO whoop_sleep
                    (date, total_in_bed_hours, total_asleep_hours, sleep_efficiency_pct,
                     sleep_performance_pct, sleep_consistency_pct, disturbance_count,
                     rem_hours, sws_hours, light_hours, sleep_id, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(date) DO UPDATE SET
                    total_in_bed_hours=excluded.total_in_bed_hours,
                    total_asleep_hours=excluded.total_asleep_hours,
                    sleep_efficiency_pct=excluded.sleep_efficiency_pct,
                    sleep_performance_pct=excluded.sleep_performance_pct,
                    sleep_consistency_pct=excluded.sleep_consistency_pct,
                    disturbance_count=excluded.disturbance_count,
                    rem_hours=excluded.rem_hours,
                    sws_hours=excluded.sws_hours,
                    light_hours=excluded.light_hours,
                    sleep_id=excluded.sleep_id,
                    raw_json=excluded.raw_json,
                    updated_at=datetime('now')
                """,
                (
                    date,
                    row.get("total_in_bed_hours"),
                    row.get("total_asleep_hours"),
                    row.get("sleep_efficiency_pct"),
                    row.get("sleep_performance_pct"),
                    row.get("sleep_consistency_pct"),
                    row.get("disturbance_count"),
                    row.get("rem_hours"),
                    row.get("sws_hours"),
                    row.get("light_hours"),
                    row.get("sleep_id"),
                    json.dumps(raw),
                ),
            )
            await db.commit()

    async def upsert_whoop_cycle(self, date: str, row: dict, raw: dict):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO whoop_cycle
                    (date, strain, kilojoule, average_hr, max_hr, cycle_id, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(date) DO UPDATE SET
                    strain=excluded.strain,
                    kilojoule=excluded.kilojoule,
                    average_hr=excluded.average_hr,
                    max_hr=excluded.max_hr,
                    cycle_id=excluded.cycle_id,
                    raw_json=excluded.raw_json,
                    updated_at=datetime('now')
                """,
                (
                    date,
                    row.get("strain"),
                    row.get("kilojoule"),
                    row.get("average_hr"),
                    row.get("max_hr"),
                    row.get("cycle_id"),
                    json.dumps(raw),
                ),
            )
            await db.commit()

    # ── Strava upserts ──────────────────────────────────────────────────────

    async def upsert_strava_activity(self, activity: dict):
        """Insert/update one Strava activity. Uses Strava's activity id as PK."""
        activity_id = activity.get("id")
        if activity_id is None:
            return
        start_date = (activity.get("start_date_local") or activity.get("start_date") or "")[:10]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO strava_activities
                    (activity_id, date, sport_type, name, distance_m, moving_time_s,
                     elapsed_time_s, total_elevation_gain_m, average_hr, max_hr,
                     average_speed_mps, max_speed_mps, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(activity_id) DO UPDATE SET
                    date=excluded.date,
                    sport_type=excluded.sport_type,
                    name=excluded.name,
                    distance_m=excluded.distance_m,
                    moving_time_s=excluded.moving_time_s,
                    elapsed_time_s=excluded.elapsed_time_s,
                    total_elevation_gain_m=excluded.total_elevation_gain_m,
                    average_hr=excluded.average_hr,
                    max_hr=excluded.max_hr,
                    average_speed_mps=excluded.average_speed_mps,
                    max_speed_mps=excluded.max_speed_mps,
                    raw_json=excluded.raw_json,
                    updated_at=datetime('now')
                """,
                (
                    activity_id,
                    start_date,
                    activity.get("sport_type") or activity.get("type"),
                    activity.get("name"),
                    activity.get("distance"),
                    activity.get("moving_time"),
                    activity.get("elapsed_time"),
                    activity.get("total_elevation_gain"),
                    activity.get("average_heartrate"),
                    activity.get("max_heartrate"),
                    activity.get("average_speed"),
                    activity.get("max_speed"),
                    json.dumps(activity),
                ),
            )
            await db.commit()

    # ── Sync state ──────────────────────────────────────────────────────────

    async def set_sync_state(self, source: str, last_synced_at: str, last_record_date: Optional[str] = None, note: str = ""):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO sync_state (source, last_synced_at, last_record_date, note)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_synced_at=excluded.last_synced_at,
                    last_record_date=COALESCE(excluded.last_record_date, sync_state.last_record_date),
                    note=excluded.note
                """,
                (source, last_synced_at, last_record_date, note),
            )
            await db.commit()

    async def get_sync_state(self, source: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT source, last_synced_at, last_record_date, note FROM sync_state WHERE source = ?",
                (source,),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row else None

    # ── Read queries for layered context + Claude tool use ──────────────────

    async def get_whoop_daily(self, start_date: str, end_date: str) -> list[dict]:
        """Joined daily WHOOP row: recovery + sleep + cycle merged by date."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    r.date,
                    r.recovery_score,
                    r.hrv_rmssd_ms,
                    r.resting_hr,
                    s.total_asleep_hours,
                    s.sleep_efficiency_pct,
                    s.disturbance_count,
                    c.strain,
                    c.average_hr AS day_avg_hr,
                    c.max_hr AS day_max_hr
                FROM whoop_recovery r
                LEFT JOIN whoop_sleep s ON s.date = r.date
                LEFT JOIN whoop_cycle c ON c.date = r.date
                WHERE r.date BETWEEN ? AND ?
                ORDER BY r.date DESC
                """,
                (start_date, end_date),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_strava_activities_range(self, start_date: str, end_date: str, sport_type: Optional[str] = None) -> list[dict]:
        query = (
            "SELECT activity_id, date, sport_type, name, distance_m, moving_time_s, "
            "total_elevation_gain_m, average_hr, max_hr, average_speed_mps "
            "FROM strava_activities WHERE date BETWEEN ? AND ? "
        )
        params: list = [start_date, end_date]
        if sport_type:
            query += "AND LOWER(sport_type) = LOWER(?) "
            params.append(sport_type)
        query += "ORDER BY date DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_whoop_aggregates(self, start_date: str, end_date: str) -> dict:
        """Aggregate WHOOP metrics over a range — avg/min/max for the summary block."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    COUNT(*) AS days,
                    AVG(recovery_score) AS avg_recovery,
                    MIN(recovery_score) AS min_recovery,
                    MAX(recovery_score) AS max_recovery,
                    AVG(hrv_rmssd_ms) AS avg_hrv,
                    AVG(resting_hr) AS avg_rhr
                FROM whoop_recovery
                WHERE date BETWEEN ? AND ?
                """,
                (start_date, end_date),
            ) as cursor:
                recovery = await cursor.fetchone()
            async with db.execute(
                """
                SELECT
                    AVG(total_asleep_hours) AS avg_sleep_hours,
                    AVG(sleep_efficiency_pct) AS avg_efficiency
                FROM whoop_sleep
                WHERE date BETWEEN ? AND ?
                """,
                (start_date, end_date),
            ) as cursor:
                sleep = await cursor.fetchone()
            async with db.execute(
                """
                SELECT AVG(strain) AS avg_strain, MAX(strain) AS max_strain
                FROM whoop_cycle WHERE date BETWEEN ? AND ?
                """,
                (start_date, end_date),
            ) as cursor:
                cycle = await cursor.fetchone()
        return {
            **(dict(recovery) if recovery else {}),
            **(dict(sleep) if sleep else {}),
            **(dict(cycle) if cycle else {}),
        }

    async def get_strava_aggregates(self, start_date: str, end_date: str) -> dict:
        """Totals + by-sport counts for the summary block."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    COUNT(*) AS activity_count,
                    SUM(distance_m) / 1000.0 AS total_distance_km,
                    SUM(moving_time_s) / 3600.0 AS total_hours,
                    SUM(total_elevation_gain_m) AS total_elevation_m
                FROM strava_activities
                WHERE date BETWEEN ? AND ?
                """,
                (start_date, end_date),
            ) as cursor:
                totals = await cursor.fetchone()
            async with db.execute(
                """
                SELECT sport_type, COUNT(*) AS n,
                       SUM(distance_m)/1000.0 AS km,
                       SUM(moving_time_s)/3600.0 AS hours
                FROM strava_activities
                WHERE date BETWEEN ? AND ?
                GROUP BY sport_type
                ORDER BY n DESC
                """,
                (start_date, end_date),
            ) as cursor:
                by_sport = await cursor.fetchall()
        return {
            **(dict(totals) if totals else {}),
            "by_sport": [dict(r) for r in by_sport],
        }

    async def get_latest_whoop_date(self) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT MAX(date) FROM whoop_recovery") as cursor:
                row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def get_latest_strava_timestamp(self) -> Optional[int]:
        """Return epoch seconds of the most recent Strava activity we have."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT MAX(json_extract(raw_json, '$.start_date')) FROM strava_activities"
            ) as cursor:
                row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        try:
            dt = datetime.strptime(row[0].replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
            return int(dt.timestamp())
        except Exception:
            return None
