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
            # training_plans: one row per plan. Only one is 'active' at a time.
            # weekly_template is a JSON object keyed by lowercase day-of-week
            # (monday..sunday) with session_type / focus / prescription / notes.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS training_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    weekly_template TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    notes TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now')),
                    activated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # recovery_sessions: sauna, steam room, cold plunge, ice bath,
            # contrast, cryo. Duration in minutes, temp in Fahrenheit (Dylan's
            # unit preference). session_type is free-form enough to handle
            # new modalities but the regex pre-filter & Haiku parser in
            # ai/coach.py try to normalize to a small set.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS recovery_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    session_type TEXT NOT NULL,
                    duration_min REAL,
                    temp_f REAL,
                    notes TEXT DEFAULT '',
                    raw_message TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_recovery_sessions_date "
                "ON recovery_sessions(date)"
            )
            # goals: user-set training targets. Intentionally schema-light —
            # goal_type drives how compute_goal_progress reads the underlying
            # tables. metadata is JSON for type-specific extras (e.g. the
            # exercise name for strength goals, HR anchor for pace goals).
            # status: 'active' | 'completed' | 'abandoned' | 'paused'.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    target_value REAL,
                    target_unit TEXT DEFAULT '',
                    baseline_value REAL,
                    baseline_date TEXT,
                    deadline TEXT,
                    metadata TEXT DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    note TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now')),
                    retired_at TEXT
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status)"
            )
            # whoop_body_measurements: WHOOP's /v2/user/measurement/body only
            # returns a single latest value, so we snapshot it each time we
            # fetch. Over time these rows give us a weight trend the app UI
            # doesn't expose via API. BF% / lean mass are NOT in the v2 API —
            # they need a separate pipeline (FitDays → Apple Health → webhook).
            await db.execute("""
                CREATE TABLE IF NOT EXISTS whoop_body_measurements (
                    date TEXT PRIMARY KEY,
                    weight_kg REAL,
                    height_m REAL,
                    max_hr REAL,
                    raw_json TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # whoop_workouts: per-session workout records from /v2/activity/workout.
            # Distinct from whoop_cycle (which is DAY-level strain). When Dylan
            # starts/ends an activity on WHOOP manually, this is the authoritative
            # per-run source for HR, zones, and workout strain. Keyed by WHOOP
            # workout id (stable across refetch). start_utc/end_utc are ISO-8601
            # UTC strings we use for overlap matching against Strava activities.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS whoop_workouts (
                    workout_id TEXT PRIMARY KEY,
                    start_date TEXT NOT NULL,
                    start_utc TEXT NOT NULL,
                    end_utc TEXT NOT NULL,
                    sport_id INTEGER,
                    sport_name TEXT,
                    strain REAL,
                    kilojoule REAL,
                    average_hr REAL,
                    max_hr REAL,
                    distance_m REAL,
                    altitude_gain_m REAL,
                    altitude_change_m REAL,
                    zone0_ms INTEGER,
                    zone1_ms INTEGER,
                    zone2_ms INTEGER,
                    zone3_ms INTEGER,
                    zone4_ms INTEGER,
                    zone5_ms INTEGER,
                    percent_recorded REAL,
                    raw_json TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_whoop_workouts_date "
                "ON whoop_workouts(start_date)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_whoop_workouts_start "
                "ON whoop_workouts(start_utc)"
            )
            # active_lift_session: singleton (id always 1) tracking a lift
            # workout in progress. Driven by /liftstart and /liftend. While
            # this row exists, the bot's on_message handler routes user
            # replies through the session handler instead of the general
            # chat path — so each message is interpreted as "logging the
            # next set" rather than free-form chat. Auto-expires after
            # 2h of silence (checked on next interaction). exercises_json
            # is the structured plan parsed from the active plan's
            # prescription text at session start; history_json is the
            # rolling log of completed sets in this session.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS active_lift_session (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    started_at TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL,
                    workout_label TEXT,
                    exercises_json TEXT NOT NULL,
                    current_exercise_idx INTEGER DEFAULT 0,
                    current_set_idx INTEGER DEFAULT 0,
                    history_json TEXT DEFAULT '[]'
                )
            """)
            await db.commit()
        # Seed a default balanced-concurrent plan if no plan exists yet.
        await self._seed_default_plan_if_empty()
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

    # ── Active lift session (singleton state machine) ──────────────────────
    #
    # While a session row exists, on_message routes user text through the
    # session handler in coach.py instead of the general chat path. The
    # session is explicit (started by /liftstart, ended by /liftend or by
    # 2-hour inactivity timeout) — no inference, so the bot never confuses
    # "I'm in the middle of squats" with "I want a chat about squats".

    async def start_lift_session(
        self,
        workout_label: str,
        exercises: list[dict],
    ) -> None:
        """Begin a new lift session. Replaces any prior in-flight session."""
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM active_lift_session WHERE id = 1")
            await db.execute(
                "INSERT INTO active_lift_session "
                "(id, started_at, last_activity_at, workout_label, "
                " exercises_json, current_exercise_idx, current_set_idx, history_json) "
                "VALUES (1, ?, ?, ?, ?, 0, 0, '[]')",
                (now, now, workout_label, json.dumps(exercises)),
            )
            await db.commit()

    async def get_active_lift_session(self) -> Optional[dict]:
        """Return the in-flight session as a dict, or None if no session."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM active_lift_session WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        s = dict(row)
        try:
            s["exercises"] = json.loads(s.pop("exercises_json") or "[]")
        except Exception:
            s["exercises"] = []
        try:
            s["history"] = json.loads(s.pop("history_json") or "[]")
        except Exception:
            s["history"] = []
        return s

    async def touch_lift_session(self) -> None:
        """Update last_activity_at — call after any user interaction."""
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE active_lift_session SET last_activity_at = ? WHERE id = 1",
                (now,),
            )
            await db.commit()

    async def update_lift_session_position(
        self,
        current_exercise_idx: int,
        current_set_idx: int,
        history: list[dict],
    ) -> None:
        """Move the cursor (next exercise/set) and append to the history log."""
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE active_lift_session "
                "SET current_exercise_idx = ?, current_set_idx = ?, "
                "    history_json = ?, last_activity_at = ? "
                "WHERE id = 1",
                (current_exercise_idx, current_set_idx, json.dumps(history), now),
            )
            await db.commit()

    async def end_lift_session(self) -> Optional[dict]:
        """Close out the session. Returns the final state for summarizing."""
        session = await self.get_active_lift_session()
        if not session:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM active_lift_session WHERE id = 1")
            await db.commit()
        return session

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

    # ── WHOOP workouts (per-session, distinct from day-level cycle) ─────────

    async def upsert_whoop_workout(self, row: dict, raw: dict) -> None:
        """Insert/update a per-workout WHOOP record. Keyed by workout_id.

        `row` is the output of WhoopClient.normalize_workout(); `raw` is the
        original v2 payload so we don't lose any fields we don't model yet.
        """
        workout_id = row.get("workout_id")
        if not workout_id:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO whoop_workouts
                    (workout_id, start_date, start_utc, end_utc, sport_id, sport_name,
                     strain, kilojoule, average_hr, max_hr, distance_m,
                     altitude_gain_m, altitude_change_m,
                     zone0_ms, zone1_ms, zone2_ms, zone3_ms, zone4_ms, zone5_ms,
                     percent_recorded, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(workout_id) DO UPDATE SET
                    start_date=excluded.start_date,
                    start_utc=excluded.start_utc,
                    end_utc=excluded.end_utc,
                    sport_id=excluded.sport_id,
                    sport_name=excluded.sport_name,
                    strain=excluded.strain,
                    kilojoule=excluded.kilojoule,
                    average_hr=excluded.average_hr,
                    max_hr=excluded.max_hr,
                    distance_m=excluded.distance_m,
                    altitude_gain_m=excluded.altitude_gain_m,
                    altitude_change_m=excluded.altitude_change_m,
                    zone0_ms=excluded.zone0_ms,
                    zone1_ms=excluded.zone1_ms,
                    zone2_ms=excluded.zone2_ms,
                    zone3_ms=excluded.zone3_ms,
                    zone4_ms=excluded.zone4_ms,
                    zone5_ms=excluded.zone5_ms,
                    percent_recorded=excluded.percent_recorded,
                    raw_json=excluded.raw_json,
                    updated_at=datetime('now')
                """,
                (
                    str(workout_id),
                    row.get("start_date"),
                    row.get("start_utc"),
                    row.get("end_utc"),
                    row.get("sport_id"),
                    row.get("sport_name"),
                    row.get("strain"),
                    row.get("kilojoule"),
                    row.get("average_hr"),
                    row.get("max_hr"),
                    row.get("distance_m"),
                    row.get("altitude_gain_m"),
                    row.get("altitude_change_m"),
                    row.get("zone0_ms"),
                    row.get("zone1_ms"),
                    row.get("zone2_ms"),
                    row.get("zone3_ms"),
                    row.get("zone4_ms"),
                    row.get("zone5_ms"),
                    row.get("percent_recorded"),
                    json.dumps(raw),
                ),
            )
            await db.commit()

    async def get_whoop_workouts_in_window(
        self, start_utc: str, end_utc: str
    ) -> list[dict]:
        """Return workouts whose [start_utc, end_utc] overlaps the given window.

        Both bounds inclusive. Used by the debrief to find the WHOOP workout
        that matches a Strava activity's time window (or vice versa).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM whoop_workouts
                WHERE NOT (end_utc < ? OR start_utc > ?)
                ORDER BY start_utc DESC
                """,
                (start_utc, end_utc),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_latest_whoop_workout(
        self, within_hours: int = 24
    ) -> Optional[dict]:
        """Most recent WHOOP workout within the last N hours, or None."""
        cutoff = (datetime.utcnow() - timedelta(hours=within_hours)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM whoop_workouts
                WHERE end_utc >= ?
                ORDER BY start_utc DESC LIMIT 1
                """,
                (cutoff,),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_whoop_workouts_for_date(self, iso_date: str) -> list[dict]:
        """All workouts whose start_date == YYYY-MM-DD, most recent first."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM whoop_workouts WHERE start_date = ? "
                "ORDER BY start_utc DESC",
                (iso_date,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def find_whoop_workout_for_strava_activity(
        self,
        activity: dict,
        plus_minus_seconds: int = 1800,
    ) -> Optional[dict]:
        """Find the WHOOP workout that overlaps with a Strava activity in time.

        Match on (same calendar day) AND (start time within ±30 minutes by
        default). Returns the whoop_workouts row dict (with all the zone_*
        and HR columns) or None when no workout matches.

        Used by the live morning-brief and webhook write paths to get HR +
        zones from WHOOP at the moment a new Strava activity comes in,
        instead of waiting for backfill_notion.py to be re-run.

        The ±30 min window matches the same threshold used by
        get_correlated_runs_in_range — Strava and WHOOP timestamps for the
        same physical workout typically agree within seconds, but the
        cushion handles clock skew, lap-button delays, and post-hoc edits.
        """
        # Pull the activity's date + start time from raw_json or fall back
        # to top-level fields. start_date is UTC ISO; start_date_local
        # could mislead us by an hour around DST, so prefer UTC.
        start_iso = activity.get("start_date") or activity.get("start_date_local") or ""
        if not start_iso:
            return None
        date = start_iso[:10]
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM whoop_workouts
                WHERE start_date = ?
                  AND ABS(strftime('%s', start_utc) - strftime('%s', ?)) < ?
                ORDER BY ABS(strftime('%s', start_utc) - strftime('%s', ?)) ASC
                LIMIT 1
                """,
                (date, start_iso, plus_minus_seconds, start_iso),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row else None

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

    async def get_correlated_runs_in_range(
        self, start_date: str, end_date: str, sport_type: str = "Run"
    ) -> list[dict]:
        """Return Strava runs in a date range joined with their matching WHOOP
        workout (HR, zones, strain) when one exists.

        The join is by-date + time-proximity. Strava and WHOOP both record a
        start timestamp for the same session (Strava activities for running
        workouts are pushed from WHOOP, so the two start times agree to within
        a few seconds in practice). We LEFT JOIN so a run with no WHOOP match
        still comes back — the caller can see when HR/zone data is missing.

        Distances are returned in miles, pace in min/mile, durations in
        minutes — Dylan's units throughout. Zone time is returned in minutes
        per zone (Z1..Z5) for easy trend analysis.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    s.activity_id,
                    s.date,
                    s.name AS strava_name,
                    s.sport_type,
                    s.distance_m,
                    s.moving_time_s,
                    s.elapsed_time_s,
                    s.total_elevation_gain_m,
                    s.average_hr AS strava_avg_hr,
                    s.max_hr AS strava_max_hr,
                    s.average_speed_mps,
                    s.max_speed_mps,
                    json_extract(s.raw_json, '$.start_date') AS strava_start_utc,
                    w.workout_id AS whoop_workout_id,
                    w.start_utc AS whoop_start_utc,
                    w.end_utc AS whoop_end_utc,
                    w.sport_name AS whoop_sport_name,
                    w.strain AS whoop_strain,
                    w.kilojoule AS whoop_kilojoule,
                    w.average_hr AS whoop_avg_hr,
                    w.max_hr AS whoop_max_hr,
                    w.zone0_ms, w.zone1_ms, w.zone2_ms,
                    w.zone3_ms, w.zone4_ms, w.zone5_ms
                FROM strava_activities s
                LEFT JOIN whoop_workouts w
                  ON w.start_date = s.date
                 AND ABS(
                       strftime('%s', w.start_utc) -
                       strftime('%s', json_extract(s.raw_json, '$.start_date'))
                     ) < 1800
                WHERE s.date BETWEEN ? AND ?
                  AND LOWER(s.sport_type) = LOWER(?)
                ORDER BY s.date DESC
                """,
                (start_date, end_date, sport_type),
            ) as cursor:
                rows = await cursor.fetchall()

        def _ms_to_min(v):
            return round(v / 60000.0, 1) if v is not None else None

        out: list[dict] = []
        for r in rows:
            d = dict(r)
            # Strava-side unit conversion (miles, ft, min, min/mile)
            dist_m = d.pop("distance_m", None) or 0
            d["distance_mi"] = round(dist_m / 1609.344, 2) if dist_m else None
            mt = d.pop("moving_time_s", None)
            d["moving_min"] = round(mt / 60.0, 1) if mt else None
            et = d.pop("elapsed_time_s", None)
            d["elapsed_min"] = round(et / 60.0, 1) if et else None
            elev_m = d.pop("total_elevation_gain_m", None) or 0
            d["elevation_ft"] = round(elev_m * 3.28084, 0) if elev_m else None
            mps = d.pop("average_speed_mps", None)
            if mps and mps > 0:
                # 26.8224 = m/s to min/mi
                d["avg_pace_min_per_mi"] = round(26.8224 / mps, 2)
            else:
                d["avg_pace_min_per_mi"] = None
            max_mps = d.pop("max_speed_mps", None)
            if max_mps and max_mps > 0:
                d["max_pace_min_per_mi"] = round(26.8224 / max_mps, 2)
            else:
                d["max_pace_min_per_mi"] = None
            # WHOOP-side zone time to minutes
            d["whoop_z1_min"] = _ms_to_min(d.pop("zone1_ms", None))
            d["whoop_z2_min"] = _ms_to_min(d.pop("zone2_ms", None))
            d["whoop_z3_min"] = _ms_to_min(d.pop("zone3_ms", None))
            d["whoop_z4_min"] = _ms_to_min(d.pop("zone4_ms", None))
            d["whoop_z5_min"] = _ms_to_min(d.pop("zone5_ms", None))
            d.pop("zone0_ms", None)  # rest-zone noise — drop
            out.append(d)
        return out

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

    # ── Training plans ──────────────────────────────────────────────────────
    #
    # A plan is a weekly template. weekly_template is JSON keyed by
    # lowercase day-of-week (monday..sunday) with the shape:
    #   { "session_type": "lift" | "run" | "rest" | "cross",
    #     "focus": "<short label>",
    #     "prescription": "<full session detail>",
    #     "notes": "<scheduling logic, substitutions, etc.>" }
    # Only one plan at a time has status='active'. Activating a new plan
    # archives the previous one.

    async def get_active_plan(self) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM training_plans WHERE status = 'active' "
                "ORDER BY activated_at DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        plan = dict(row)
        try:
            plan["weekly_template"] = json.loads(plan["weekly_template"])
        except Exception:
            plan["weekly_template"] = {}
        return plan

    async def list_plans(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, name, goal, status, created_at, activated_at "
                "FROM training_plans ORDER BY created_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def save_plan(
        self,
        name: str,
        goal: str,
        weekly_template: dict,
        notes: str = "",
        activate: bool = True,
    ) -> int:
        """Save a new plan. If activate=True, archive any currently-active plan."""
        tpl_json = json.dumps(weekly_template)
        async with aiosqlite.connect(self.db_path) as db:
            if activate:
                await db.execute(
                    "UPDATE training_plans SET status = 'archived' WHERE status = 'active'"
                )
            cursor = await db.execute(
                "INSERT INTO training_plans (name, goal, weekly_template, status, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, goal, tpl_json, "active" if activate else "draft", notes),
            )
            plan_id = cursor.lastrowid
            await db.commit()
        logger.info(f"Saved training plan '{name}' (id={plan_id}, active={activate})")
        return plan_id

    async def set_active_plan(self, plan_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            # Check plan exists
            async with db.execute(
                "SELECT id FROM training_plans WHERE id = ?", (plan_id,)
            ) as cursor:
                exists = await cursor.fetchone()
            if not exists:
                return False
            await db.execute(
                "UPDATE training_plans SET status = 'archived' WHERE status = 'active'"
            )
            await db.execute(
                "UPDATE training_plans SET status = 'active', "
                "activated_at = datetime('now') WHERE id = ?",
                (plan_id,),
            )
            await db.commit()
        logger.info(f"Activated plan id={plan_id}")
        return True

    async def get_session_for_day(self, day_of_week: str) -> Optional[dict]:
        """Return the session for a given weekday ('monday'..'sunday')
        from the active plan, or None if no plan / no session."""
        plan = await self.get_active_plan()
        if not plan:
            return None
        tpl = plan.get("weekly_template") or {}
        return tpl.get(day_of_week.lower())

    async def _seed_default_plan_if_empty(self):
        """Insert a sensible starter plan if the table has no rows."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM training_plans") as cursor:
                (count,) = await cursor.fetchone()
        if count:
            return
        template = {
            "monday": {
                "session_type": "lift",
                "focus": "legs",
                "prescription": (
                    "Main: back squat 4x6 working up to a top set at ~80% (RPE 8). "
                    "Assistance: RDL 3x10, walking lunge 3x10/side, leg curl 3x12, "
                    "standing calf raise 3x15, core (ab wheel or weighted plank) 3 sets. "
                    "~55–65 min."
                ),
                "notes": (
                    "Heavy legs first in the week while you're fresh from Sunday. "
                    "Progressive overload: add 2.5–5 lb on the main lift when you hit "
                    "all reps with technique intact."
                ),
            },
            "tuesday": {
                "session_type": "run",
                "focus": "easy aerobic",
                "prescription": (
                    "Easy Z2 run 30–45 min, conversational pace. Target HR roughly "
                    "65–75% of max (~130–145 bpm for you). Nothing hard — this is "
                    "aerobic base AND recovery from Monday legs."
                ),
                "notes": (
                    "If legs are trashed from Monday, keep it to 30 min or swap for "
                    "a brisk walk. The point is blood flow, not pace."
                ),
            },
            "wednesday": {
                "session_type": "lift",
                "focus": "push (upper)",
                "prescription": (
                    "Main: bench press 4x6–8 working up to RPE 8. Assistance: "
                    "overhead press 3x8, incline DB press 3x10, lateral raise 3x12, "
                    "tricep pushdown or dip 3x10, core 3 sets. ~50–60 min."
                ),
                "notes": (
                    "Alternate bench and OHP as the main lift week-to-week if you "
                    "want both to progress. Same rule: +2.5–5 lb when you hit all reps."
                ),
            },
            "thursday": {
                "session_type": "run",
                "focus": "quality (tempo or intervals)",
                "prescription": (
                    "The one hard run of the week. Pick one:\n"
                    "  (a) 4 x 1 mi at threshold pace w/ 60s jog recovery\n"
                    "  (b) 20 min steady tempo at comfortably-hard effort\n"
                    "  (c) 8 x 400m at 5k pace w/ 90s easy recovery\n"
                    "Always: 10 min easy warmup + 10 min cool down. Total 35–50 min."
                ),
                "notes": (
                    "This is the 20% of the 80/20. If recovery is yellow, pick the "
                    "shortest option. If red, swap for easy 30 min Z2."
                ),
            },
            "friday": {
                "session_type": "lift",
                "focus": "pull (upper) / posterior chain",
                "prescription": (
                    "Main: deadlift 3x5 OR barbell row 4x6 (alternate weeks). "
                    "Assistance: pull-up or lat pulldown 3x8–10, seated row 3x10, "
                    "face pull or rear delt fly 3x12, barbell/DB curl 3x10, "
                    "core 3 sets. ~55–65 min."
                ),
                "notes": (
                    "Heavy pull day. If low back is cranky, swap deadlift for RDL "
                    "3x8 at moderate weight and emphasize the row variation."
                ),
            },
            "saturday": {
                "session_type": "run",
                "focus": "long run",
                "prescription": (
                    "Long easy run, Z2 pace. Start at your current comfortable "
                    "distance and build 10% per week until 10–12 mi feels easy. "
                    "Conversational throughout — if you can't hold a sentence, slow "
                    "down. Duration target: 60–90 min."
                ),
                "notes": (
                    "This is the volume driver for aerobic base. Sauna after is a "
                    "legitimate cap — heat exposure post-long-run compounds the "
                    "cardiovascular adaptations."
                ),
            },
            "sunday": {
                "session_type": "rest",
                "focus": "active recovery",
                "prescription": (
                    "Full rest or gentle active recovery only: mobility, walk, "
                    "sauna/steam, cold plunge. No running, no lifting."
                ),
                "notes": (
                    "Actual rest is training. Skipping Sunday is how you end up in "
                    "the 30-day recovery hole you were climbing out of last week."
                ),
            },
        }
        plan_notes = (
            "Default starter plan. 3 lifts + 3 runs + 1 rest. Concurrent-friendly "
            "scheduling: heavy legs after Sunday rest, quality run mid-week, long "
            "run separated from leg day by 5 days. Golf/basketball/squash/tennis "
            "count as light cross-training — play them on any day; if intense they "
            "can replace Tuesday's easy run, if gentle they can add to Sunday. "
            "Adjust freely as goals or life shift — this is a starting point, not a "
            "contract."
        )
        await self.save_plan(
            name="Balanced concurrent",
            goal=(
                "Good mix of lifting and running — 3 runs + 3 lifts + 1 rest per "
                "week, polarized running (80% easy + 20% quality), progressive "
                "overload on main lifts (squat, bench/OHP, deadlift/row)."
            ),
            weekly_template=template,
            notes=plan_notes,
            activate=True,
        )
        logger.info("Seeded default 'Balanced concurrent' training plan.")

    # ── Recovery sessions (sauna / steam / cold plunge / etc.) ──────────────

    async def log_recovery_session(
        self,
        date: str,
        session_type: str,
        duration_min: Optional[float] = None,
        temp_f: Optional[float] = None,
        notes: str = "",
        raw: str = "",
    ) -> int:
        """Insert a recovery session (sauna, cold plunge, etc.)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO recovery_sessions "
                "(date, session_type, duration_min, temp_f, notes, raw_message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (date, session_type, duration_min, temp_f, notes, raw),
            )
            rec_id = cursor.lastrowid
            await db.commit()
        bits = [session_type]
        if duration_min is not None:
            bits.append(f"{duration_min}min")
        if temp_f is not None:
            bits.append(f"{temp_f}°F")
        logger.info(f"Recovery session logged: {' | '.join(bits)}")
        return rec_id

    async def get_recent_recovery_sessions(self, days: int = 14) -> list[dict]:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT date, session_type, duration_min, temp_f, notes "
                "FROM recovery_sessions WHERE date >= ? ORDER BY date DESC, id DESC",
                (since,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ── Goals ───────────────────────────────────────────────────────────────
    #
    # One row per goal. goal_type tells compute_goal_progress where to read
    # the "current value" from:
    #   weight   → WHOOP /v2/user/measurement/body (latest only)
    #   strength → lifts table, filtered by metadata.exercise
    #   pace     → strava_activities (running pace)
    #   bf       → external (FitDays → Apple Health → webhook) — no auto source
    #   habit    → active-day count over a trailing window
    # metadata is a JSON blob for type-specific extras (exercise name, HR
    # anchor, etc.). Kept loose so new goal types can bolt on without schema
    # churn.

    async def create_goal(
        self,
        goal_type: str,
        title: str,
        target_value: Optional[float] = None,
        target_unit: str = "",
        baseline_value: Optional[float] = None,
        baseline_date: Optional[str] = None,
        deadline: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        meta_json = json.dumps(metadata or {})
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO goals
                    (goal_type, title, target_value, target_unit, baseline_value,
                     baseline_date, deadline, metadata, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    goal_type,
                    title,
                    target_value,
                    target_unit or "",
                    baseline_value,
                    baseline_date,
                    deadline,
                    meta_json,
                ),
            )
            goal_id = cursor.lastrowid
            await db.commit()
        logger.info(f"Goal created: #{goal_id} {goal_type} — {title}")
        return goal_id

    async def list_goals(self, status: Optional[str] = None) -> list[dict]:
        """List goals. status=None returns all; 'active' returns only active."""
        query = "SELECT * FROM goals"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        out: list[dict] = []
        for r in rows:
            g = dict(r)
            try:
                g["metadata"] = json.loads(g.get("metadata") or "{}")
            except Exception:
                g["metadata"] = {}
            out.append(g)
        return out

    async def get_goal(self, goal_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM goals WHERE id = ?", (goal_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        g = dict(row)
        try:
            g["metadata"] = json.loads(g.get("metadata") or "{}")
        except Exception:
            g["metadata"] = {}
        return g

    async def update_goal_status(
        self, goal_id: int, status: str, note: str = ""
    ) -> bool:
        """Mark a goal completed / abandoned / paused / active. Returns False if no such id."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id FROM goals WHERE id = ?", (goal_id,)
            ) as cursor:
                exists = await cursor.fetchone()
            if not exists:
                return False
            # retired_at marks the last state-change into a terminal state;
            # reactivating clears it.
            retired_expr = (
                "datetime('now')" if status != "active" else "NULL"
            )
            await db.execute(
                f"UPDATE goals SET status = ?, note = ?, retired_at = {retired_expr} "
                f"WHERE id = ?",
                (status, note, goal_id),
            )
            await db.commit()
        logger.info(f"Goal #{goal_id} → {status}")
        return True

    async def compute_goal_progress(self, goal: dict, coach=None) -> dict:
        """Compute live progress for a goal.

        Returns dict with keys: current_value, pct_done, eta, note.
        Any may be None when the data source is missing or ambiguous.
        """
        result: dict = {
            "current_value": None,
            "pct_done": None,
            "eta": None,
            "note": "",
        }
        gtype = (goal.get("goal_type") or "").lower()
        target = goal.get("target_value")
        baseline = goal.get("baseline_value")
        baseline_date = goal.get("baseline_date")
        deadline = goal.get("deadline")
        metadata = goal.get("metadata") or {}

        def _project_eta(
            current: float, start: float, target_v: float, start_date: str
        ) -> Optional[str]:
            """Linear projection of ETA given progress so far."""
            try:
                bd = datetime.strptime(start_date, "%Y-%m-%d").date()
            except Exception:
                return None
            elapsed = (datetime.now().date() - bd).days
            if elapsed <= 0 or current == start or target_v == start:
                return None
            progress_per_day = (current - start) / elapsed
            if progress_per_day == 0:
                return None
            remaining_units = target_v - current
            # Only project if we're moving in the right direction.
            needed_sign = 1 if target_v > current else -1
            if (progress_per_day > 0) != (needed_sign > 0):
                return None
            days_needed = remaining_units / progress_per_day
            if days_needed < 0 or days_needed > 365 * 3:
                return None
            eta_date = datetime.now().date() + timedelta(days=int(days_needed))
            return eta_date.isoformat()

        # ── Weight: pull latest from WHOOP ──────────────────────────────────
        if gtype == "weight":
            body = None
            if coach is not None:
                try:
                    body = await coach.whoop.get_body_measurement()
                except Exception as e:
                    logger.info(f"WHOOP body fetch failed in goal progress: {e}")
            if body and body.get("weight_kilogram"):
                current_lb = round(body["weight_kilogram"] * 2.20462, 1)
                result["current_value"] = current_lb
                # Also persist the snapshot so weight has a history.
                try:
                    await self.upsert_whoop_body_measurement(body)
                except Exception as e:
                    logger.debug(f"Body measurement upsert failed: {e}")
                if baseline is not None and target is not None and baseline != target:
                    total = abs(baseline - target)
                    done = abs(baseline - current_lb)
                    pct = round(max(0, min(100, (done / total) * 100)), 1) if total else 0
                    result["pct_done"] = pct
                    if baseline_date:
                        result["eta"] = _project_eta(current_lb, baseline, target, baseline_date)
            else:
                result["note"] = (
                    "No current weight from WHOOP. Step on the scale (FitDays syncs to WHOOP) "
                    "or manually enter one."
                )

        # ── Strength: pull latest lift for the exercise ─────────────────────
        elif gtype == "strength":
            exercise = metadata.get("exercise") or ""
            if not exercise:
                result["note"] = "Strength goal has no exercise in metadata."
            else:
                rows = await self.get_lifts_for_exercise(exercise, limit=1)
                if rows:
                    # current_value is free-form because lift details are
                    # free-form; user can refine if they want a numeric track.
                    result["current_value"] = rows[0]["details"]
                    result["note"] = f"Most recent {exercise}: {rows[0]['date']}"
                else:
                    result["note"] = f"No lifts logged for {exercise} yet."

        # ── Pace: avg run pace over trailing 30d ────────────────────────────
        elif gtype == "pace":
            today = datetime.now().date()
            d30 = (today - timedelta(days=30)).isoformat()
            rows = await self.get_strava_activities_range(d30, today.isoformat(), sport_type="Run")
            paces_sec = [
                1609.344 / r["average_speed_mps"]
                for r in rows
                if (r.get("average_speed_mps") or 0) > 0
            ]
            if paces_sec:
                avg_sec = sum(paces_sec) / len(paces_sec)
                m, s = divmod(int(avg_sec), 60)
                result["current_value"] = f"{m}:{s:02d}/mi"
                result["note"] = (
                    f"30d avg run pace across {len(paces_sec)} runs. "
                    "For a cleaner signal anchor to a specific HR (e.g. Z2 at 145 bpm)."
                )
            else:
                result["note"] = "No runs logged in the last 30 days."

        # ── BF%: no API source ──────────────────────────────────────────────
        elif gtype == "bf":
            result["note"] = (
                "BF% has no WHOOP API source — FitDays/Apple Health webhook or manual "
                "entry needed. Baseline and target are held; current value is user-supplied."
            )

        # ── Habit: active days in trailing 7 ────────────────────────────────
        elif gtype == "habit":
            today = datetime.now().date()
            d7 = (today - timedelta(days=7)).isoformat()
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT DISTINCT date FROM strava_activities WHERE date >= ?",
                    (d7,),
                ) as cur:
                    strava_days = {r[0] for r in await cur.fetchall()}
                async with db.execute(
                    "SELECT DISTINCT date FROM lifts WHERE date >= ?",
                    (d7,),
                ) as cur:
                    lift_days = {r[0] for r in await cur.fetchall()}
            active = len(strava_days | lift_days)
            result["current_value"] = active
            if target and target > 0:
                result["pct_done"] = round(min(100, (active / target) * 100), 1)
            result["note"] = "Active days = day with a Strava activity or a logged lift."

        else:
            result["note"] = f"Unknown goal_type '{gtype}' — progress not computed."

        # Deadline-aware note
        if deadline:
            try:
                dl = datetime.strptime(deadline, "%Y-%m-%d").date()
                days_left = (dl - datetime.now().date()).days
                tail = (
                    f" | {days_left} days to deadline ({deadline})"
                    if days_left >= 0
                    else f" | past deadline by {-days_left} days"
                )
                result["note"] = (result["note"] or "") + tail
            except Exception:
                pass

        return result

    # ── WHOOP body measurement snapshot ─────────────────────────────────────

    async def upsert_whoop_body_measurement(self, body: dict) -> None:
        """Snapshot today's WHOOP body measurement (weight/height/maxHR).

        The v2 endpoint only returns the latest value, so the only way to
        build a weight trend is to stamp it whenever we fetch.
        """
        if not body:
            return
        today_iso = datetime.now().strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO whoop_body_measurements
                    (date, weight_kg, height_m, max_hr, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(date) DO UPDATE SET
                    weight_kg=excluded.weight_kg,
                    height_m=excluded.height_m,
                    max_hr=excluded.max_hr,
                    raw_json=excluded.raw_json,
                    updated_at=datetime('now')
                """,
                (
                    today_iso,
                    body.get("weight_kilogram"),
                    body.get("height_meter"),
                    body.get("max_heart_rate"),
                    json.dumps(body),
                ),
            )
            await db.commit()

    async def get_body_measurement_history(self, days: int = 90) -> list[dict]:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT date, weight_kg, height_m, max_hr FROM whoop_body_measurements "
                "WHERE date >= ? ORDER BY date DESC",
                (since,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]
