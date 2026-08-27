"""
Notion integration — writes training log entries automatically.

Four-database model (mirrors the user's spreadsheet mock):

  • SCHEDULE   — day-level index. One row per date: Training Group (Lift/Run/
                 Cross-train/Rest/Other), Workout (Push/Pull/Legs/Easy/etc.),
                 Notes. Relates to Lifts, Runs, and Daily Log so a single
                 Schedule page shows the full day at a glance.
  • LIFTS      — one row per exercise per day. Sets, Reps, Weight (lb), RPE.
                 Lets you chart bench progression or compare squat volume
                 across months.
  • RUNS       — one row per cardio activity. Distance (mi), Pace (time/mi),
                 Duration (min), Avg HR, Elevation Gain (ft), HR Zones 1–5
                 as % of total. "Runs" is the DB name but Type distinguishes
                 Run/Ride/Hike/Swim/Walk so all cardio lands here.
  • DAILY LOG  — WHOOP physiology + the morning brief text. One row per day.

Why four databases instead of one big one: Lifts and Runs need very
different columns. Burying both in a single table means half the columns
are always blank and you can't chart either effectively. Separate DBs with
Notion Relations give you per-activity clarity AND a "day view" via the
Schedule DB.

Units: miles and pounds everywhere (matches the rest of the bot and the
user's preference). All unit conversion from Strava's metric API happens
in this module, not in callers.
"""

import json
import logging
import obs
import re
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Markers we embed in Notion's Notes field so the reconciliation pass can
# detect which SQLite rows have already been imported without needing a
# side table. Match the patterns used by log_lift / log_strava_activity.
_LIFTROW_MARKER = re.compile(r"\[liftrow:(\d+)\]")
_STRAVA_MARKER = re.compile(r"\[strava:(\d+)\]")

# Valid Notion Select option sets. Kept in sync with the SQL DDL used to
# create the databases. If you change the schema in Notion, update here too.
_VALID_TRAINING_GROUPS = {"Lift", "Run", "Cross-train", "Rest", "Other"}
_VALID_WORKOUTS_SCHEDULE = {
    "Push", "Pull", "Legs",
    "Easy", "Long", "Tempo", "Intervals",
    "Recovery", "Other",
}
_VALID_WORKOUTS_LIFT = {"Push", "Pull", "Legs", "Other"}
_VALID_RUN_TYPES = {"Run", "Ride", "Hike", "Swim", "Walk"}
_VALID_SOURCES = {"Strava", "WHOOP", "Manual"}


# Strava sport_type → (runs-db Type, fallback training_group).
# Anything unmapped falls back to Type=Run (keeps the row, user can fix),
# unless the activity is clearly lifting in which case we don't route it
# through log_run at all (log_lift is the right path).
_STRAVA_TYPE_MAP: dict[str, str] = {
    "Run": "Run",
    "TrailRun": "Run",
    "VirtualRun": "Run",
    "Ride": "Ride",
    "VirtualRide": "Ride",
    "EBikeRide": "Ride",
    "MountainBikeRide": "Ride",
    "GravelRide": "Ride",
    "Hike": "Hike",
    "Swim": "Swim",
    "Walk": "Walk",
}

# Strava sport types that we route to the Lifts DB rather than Runs. Kept
# narrow — Strava users sometimes log lifting as a generic "Workout", which
# is ambiguous; we leave that for the chat-log path rather than guess.
_STRAVA_LIFT_TYPES = {"WeightTraining"}


def _placeholder(value: str | None) -> bool:
    """Treat empty strings and the .env.example 'your_...' shape as unset."""
    if not value:
        return True
    return value.startswith("your_")


# ── Unit conversion helpers ──────────────────────────────────────────────────

def _meters_to_miles(meters: float | int | None) -> float | None:
    if meters is None:
        return None
    return round(meters / 1609.344, 2)


def _meters_to_feet(meters: float | int | None) -> float | None:
    if meters is None:
        return None
    return round(meters * 3.28084, 0)


def _seconds_to_minutes(seconds: float | int | None) -> float | None:
    if seconds is None:
        return None
    return round(seconds / 60, 1)


def _format_pace(duration_min: float | None, distance_mi: float | None) -> str | None:
    """mm:ss/mi — only meaningful for foot-propelled activities."""
    if not duration_min or not distance_mi or distance_mi <= 0:
        return None
    per_mi_min = duration_min / distance_mi
    whole = int(per_mi_min)
    secs = int(round((per_mi_min - whole) * 60))
    if secs == 60:
        whole += 1
        secs = 0
    return f"{whole}:{secs:02d}/mi"


def zone_pcts_from_whoop_row(row: dict | None) -> dict[str, float] | None:
    """Convert a whoop_workouts SQLite row's zone0_ms..zone5_ms into the
    five zone percentages we put into Notion.

    Collapses WHOOP's Z0 (warmup-before-Z1) into Z1 so the Notion view
    stays five-zone. Returns None when no zone data is available so the
    caller can skip writing those columns and leave them blank rather
    than 0%-which would falsely imply you spent zero time in each zone.

    Used by both the offline backfill and the live daily-brief / webhook
    paths — keeping the math in one place avoids drift between them.
    """
    if not row:
        return None
    zone_ms = [
        row.get("zone0_ms"), row.get("zone1_ms"), row.get("zone2_ms"),
        row.get("zone3_ms"), row.get("zone4_ms"), row.get("zone5_ms"),
    ]
    if not any(z is not None for z in zone_ms):
        return None
    zone_ms = [z or 0 for z in zone_ms]
    total_ms = sum(zone_ms)
    if total_ms <= 0:
        return None
    return {
        "zone_1_pct": round(100.0 * (zone_ms[0] + zone_ms[1]) / total_ms, 1),
        "zone_2_pct": round(100.0 * zone_ms[2] / total_ms, 1),
        "zone_3_pct": round(100.0 * zone_ms[3] / total_ms, 1),
        "zone_4_pct": round(100.0 * zone_ms[4] / total_ms, 1),
        "zone_5_pct": round(100.0 * zone_ms[5] / total_ms, 1),
    }


def _zones_from_strava_distribution(
    zones: list[dict] | None,
    total_seconds: float | int | None,
) -> tuple[dict[str, float] | None, str | None]:
    """Convert Strava's zones response to (Zone1%..Zone5% dict, zone_source).

    Strava's GET /activities/{id}/zones returns a list of zone objects with
    'type': 'heartrate' OR 'pace' and 'distribution_buckets':
    [{'min':..,'max':..,'time':..}] where time is seconds per bucket. We sum
    one zone type's bucket times and express each as a percent of total.

    Heartrate is the preferred source and wins whenever it is present. But
    this user's activities are uploaded to Strava BY WHOOP with the HR stream
    stripped, so Strava has no heart rate to bucket and returns a *pace*
    distribution instead (verified live: types: ['pace']). Before that
    fallback existed this function returned None for every one of those
    activities, which is why the Zone 1-5 % columns in Notion were
    permanently null on exactly the runs the user cares about.

    The second tuple element names where the numbers came from —
    "heartrate", "pace", or None when nothing usable was found — so callers
    can log or label the provenance. Pace-derived percentages occupy the same
    five columns but are NOT heart-rate zones; a caller must not present them
    as if they were.
    """
    if not zones or not total_seconds or total_seconds <= 0:
        return None, None
    # Preference order, not "first type with buckets": an activity carrying
    # both distributions must always be scored off heart rate.
    zone: dict | None = None
    zone_source: str | None = None
    for wanted in ("heartrate", "pace"):
        candidate = next(
            (z for z in zones if isinstance(z, dict) and z.get("type") == wanted),
            None,
        )
        if candidate and (candidate.get("distribution_buckets") or []):
            zone, zone_source = candidate, wanted
            break
    if not zone:
        return None, None
    buckets = zone.get("distribution_buckets") or []
    # Strava returns up to 5 zones; if fewer, the missing zones stay 0.
    pct: dict[str, float] = {}
    for i, bucket in enumerate(buckets[:5]):
        t = bucket.get("time") or 0
        pct[f"zone_{i + 1}_pct"] = round(100.0 * t / total_seconds, 1)
    # Fill any missing zones with 0 so Notion shows 0 rather than blank —
    # makes the "this run was all Z2" vs "missing data" distinction clear.
    for i in range(1, 6):
        pct.setdefault(f"zone_{i}_pct", 0.0)
    return pct, zone_source


# ── Property builders ────────────────────────────────────────────────────────

def _set_number(props: dict, key: str, value) -> None:
    """Drop None/empty so Notion doesn't reject on type mismatch."""
    if value is None:
        return
    try:
        num = float(value)
    except (TypeError, ValueError):
        return
    props[key] = {"number": num}


def _set_rich_text(props: dict, key: str, value) -> None:
    if value is None or value == "":
        return
    s = str(value)
    # Notion rich_text caps at 2000 chars per chunk; truncate rather than
    # silently losing the tail of a long daily brief.
    if len(s) > 2000:
        s = s[:1980] + "\n[truncated]"
    props[key] = {"rich_text": [{"text": {"content": s}}]}


def _set_title(props: dict, key: str, value) -> None:
    s = str(value) if value else "(untitled)"
    props[key] = {"title": [{"text": {"content": s[:2000]}}]}


def _set_select(props: dict, key: str, value, valid: set[str], fallback: str | None = None) -> None:
    if value is None:
        return
    if value in valid:
        props[key] = {"select": {"name": value}}
    elif fallback and fallback in valid:
        props[key] = {"select": {"name": fallback}}


def _set_date(props: dict, key: str, date: str | None) -> None:
    if not date:
        return
    props[key] = {"date": {"start": date}}


# ── Main client ──────────────────────────────────────────────────────────────

class NotionClient:
    def __init__(self, config):
        self.api_key = config.NOTION_API_KEY
        self.schedule_db_id = getattr(config, "NOTION_SCHEDULE_DATABASE_ID", "")
        self.lifts_db_id = getattr(config, "NOTION_LIFTS_DATABASE_ID", "")
        self.lift_sets_db_id = getattr(config, "NOTION_LIFT_SETS_DATABASE_ID", "")
        self.runs_db_id = getattr(config, "NOTION_RUNS_DATABASE_ID", "")
        self.daily_db_id = getattr(config, "NOTION_DAILY_DATABASE_ID", "")

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # ── Per-DB configuration gates ──────────────────────────────────────────
    # Each database is independently gated: the user might fill in the Runs
    # DB id a day before the Lifts DB id. None of the gates should crash on
    # a missing sibling.

    def _has_api_key(self) -> bool:
        return not _placeholder(self.api_key)

    def is_configured_schedule(self) -> bool:
        return self._has_api_key() and not _placeholder(self.schedule_db_id)

    def is_configured_lifts(self) -> bool:
        return self._has_api_key() and not _placeholder(self.lifts_db_id)

    def is_configured_lift_sets(self) -> bool:
        return self._has_api_key() and not _placeholder(self.lift_sets_db_id)

    def is_configured_runs(self) -> bool:
        return self._has_api_key() and not _placeholder(self.runs_db_id)

    def is_configured_daily(self) -> bool:
        return self._has_api_key() and not _placeholder(self.daily_db_id)

    def is_configured(self) -> bool:
        """Back-compat: True if *any* DB is wired up."""
        return any(
            [
                self.is_configured_schedule(),
                self.is_configured_lifts(),
                self.is_configured_lift_sets(),
                self.is_configured_runs(),
                self.is_configured_daily(),
            ]
        )

    # ── Connectivity pings ──────────────────────────────────────────────────

    async def _ping_database(self, db_id: str) -> tuple[bool, str]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(
                    f"{NOTION_BASE}/databases/{db_id}",
                    headers=self._headers(),
                )
            except Exception as e:
                # The string goes back to whoever asked (a /notionping reply);
                # nothing was ever written to the log, so an unreachable Notion
                # during a scheduled run left no trace at all.
                obs.source_failed(
                    logger, "notion_ping", "_ping_database", e, db_id=db_id
                )
                return False, f"Network error reaching Notion: {e}"
        if resp.status_code == 200:
            body = resp.json()
            title_rt = body.get("title") or []
            title = title_rt[0].get("plain_text") if title_rt else "(untitled)"
            return True, f"Connected to '{title}'."
        # Everything from here down is a failure that used to be returned as
        # a human-readable string and never logged — a revoked API key looked
        # identical to a healthy one in the journal. One WARNING names the
        # database and the status before we build the friendly message.
        obs.log_event(
            logger,
            logging.WARNING,
            "notion.ping_failed",
            db_id=db_id,
            status=resp.status_code,
            detail=resp.text[:200],
        )
        if resp.status_code == 401:
            return False, "401 Unauthorized — NOTION_API_KEY is invalid or revoked."
        if resp.status_code == 404:
            return (
                False,
                "404 Not Found — database id is wrong, OR the integration hasn't been "
                "added to this database (open it → ••• → Connections → add integration).",
            )
        return False, f"Notion API returned {resp.status_code}: {resp.text[:200]}"

    async def ping(self) -> tuple[bool, str]:
        """Back-compat single ping — prefers Schedule, falls back to first
        configured DB."""
        for getter, db_id in [
            ("schedule", self.schedule_db_id),
            ("daily", self.daily_db_id),
            ("lifts", self.lifts_db_id),
            ("runs", self.runs_db_id),
        ]:
            if not _placeholder(db_id) and self._has_api_key():
                return await self._ping_database(db_id)
        return False, "No Notion database configured in .env."

    async def ping_all(self) -> dict[str, tuple[bool, str]]:
        """Ping each configured database independently."""
        results: dict[str, tuple[bool, str]] = {}

        async def _one(label: str, configured: bool, db_id: str, missing_msg: str):
            if configured:
                results[label] = await self._ping_database(db_id)
            else:
                results[label] = (False, missing_msg)

        await _one(
            "schedule",
            self.is_configured_schedule(),
            self.schedule_db_id,
            "NOTION_SCHEDULE_DATABASE_ID not set in .env.",
        )
        await _one(
            "lifts",
            self.is_configured_lifts(),
            self.lifts_db_id,
            "NOTION_LIFTS_DATABASE_ID not set in .env.",
        )
        await _one(
            "runs",
            self.is_configured_runs(),
            self.runs_db_id,
            "NOTION_RUNS_DATABASE_ID not set in .env.",
        )
        await _one(
            "daily",
            self.is_configured_daily(),
            self.daily_db_id,
            "NOTION_DAILY_DATABASE_ID not set in .env.",
        )
        return results

    # ── Low-level create-page helper ────────────────────────────────────────

    async def _create_page(self, db_id: str, properties: dict) -> dict | None:
        """POST /v1/pages. Returns the created page object on success, None on
        failure. Never raises so callers can safely best-effort log without
        wrapping each call in try/except."""
        payload = {
            "parent": {"database_id": db_id},
            "properties": properties,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{NOTION_BASE}/pages",
                    headers=self._headers(),
                    json=payload,
                )
        except Exception as e:
            logger.warning(f"Notion create_page network error: {e}")
            return None
        if resp.status_code in (200, 201):
            return resp.json()
        logger.warning(
            f"Notion create_page failed: {resp.status_code} {resp.text[:300]}"
        )
        return None

    # ── Schedule DB writes ──────────────────────────────────────────────────

    async def log_schedule(
        self,
        *,
        date: str,
        training_group: str | None = None,
        workout: str | None = None,
        notes: str | None = None,
    ) -> str | None:
        """Create one Schedule row for `date`. Returns the page UUID on
        success (suitable for use as a Relation target), None otherwise.

        IMPORTANT: returns page["id"], NOT page["url"]. Notion's Relation
        property requires a UUID; passing a URL silently breaks the entire
        page write that's trying to use this as its relation target. The
        earlier version of this method returned `url or id` and that bug
        caused ~80% of backfill writes to silently 400 because every newly
        created Schedule got a URL back, then the Daily/Run row that
        referenced it failed validation.

        Training Group and Workout values outside the known Select options are
        silently dropped rather than written; see _VALID_TRAINING_GROUPS and
        _VALID_WORKOUTS_SCHEDULE.

        Idempotency: this does NOT check for an existing row for the same
        date. Callers that want one Schedule row per day should use
        `find_or_create_schedule(date)` instead.
        """
        if not self.is_configured_schedule():
            logger.debug("Notion Schedule DB not configured — skipping.")
            return None
        props: dict = {}
        _set_title(props, "Date", date)
        _set_date(props, "Day", date)
        _set_select(props, "Training Group", training_group, _VALID_TRAINING_GROUPS, fallback="Other")
        _set_select(props, "Workout", workout, _VALID_WORKOUTS_SCHEDULE, fallback="Other")
        _set_rich_text(props, "Notes", notes)
        page = await self._create_page(self.schedule_db_id, props)
        if page:
            logger.debug("Notion Schedule row created for %s.", date)
            return page.get("id")
        return None

    async def find_schedule_for_date(self, date: str) -> str | None:
        """POST /v1/databases/{id}/query filtered by date. Returns the first
        matching page's id, or None if no row exists for that date.

        Used by find_or_create_schedule to keep one row per day — without this
        lookup, every log_lift / log_run would spawn duplicate Schedule rows.
        """
        if not self.is_configured_schedule():
            return None
        payload = {
            "filter": {"property": "Day", "date": {"equals": date}},
            "page_size": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{NOTION_BASE}/databases/{self.schedule_db_id}/query",
                    headers=self._headers(),
                    json=payload,
                )
        except Exception as e:
            # Returning None here does not just lose a relation: the caller
            # (find_or_create_schedule) reads it as "no row for this date" and
            # CREATES a duplicate Schedule page. A failed lookup must be loud.
            obs.source_failed(
                logger, "notion_schedule", "find_schedule_for_date", e, date=date
            )
            return None
        if resp.status_code != 200:
            obs.log_event(
                logger,
                logging.WARNING,
                "notion.schedule_query_failed",
                date=date,
                status=resp.status_code,
                detail=resp.text[:200],
            )
            return None
        results = resp.json().get("results", [])
        return results[0]["id"] if results else None

    async def find_or_create_schedule(
        self, date: str, training_group: str | None = None, workout: str | None = None
    ) -> str | None:
        """Return a Schedule page id for `date`, creating it if missing.

        Callers (Lifts/Runs writers) use this to get a relation target. If
        Schedule DB isn't configured, returns None and the caller just skips
        the relation property — Lifts/Runs rows still get written, just
        without the back-link.
        """
        if not self.is_configured_schedule():
            return None
        existing = await self.find_schedule_for_date(date)
        if existing:
            return existing
        page_id = await self.log_schedule(
            date=date, training_group=training_group, workout=workout
        )
        return page_id

    # ── Lifts DB writes ─────────────────────────────────────────────────────

    async def log_lift(
        self,
        *,
        date: str,
        exercise: str,
        workout: str | None = None,
        sets: int | None = None,
        reps: int | None = None,
        weight_lb: float | None = None,
        rpe: float | None = None,
        notes: str | None = None,
        lift_id: int | None = None,
    ) -> str | None:
        """Write one Lifts row. Returns the new Notion page id on success,
        None on failure.

        The return type changed from bool → Optional[str] when Lift Sets
        were added — callers that wire up the Parent Lift relation on
        child Lift Sets rows need the page id. Truthy-check sites
        (`if not ok`, `if page:`) keep working unchanged because Python
        falsy semantics treat `None` and `""` the same as `False`.

        All numeric fields are optional — when the chat-log parser can't
        extract structured sets/reps/weight, pass `notes=<raw message>` so
        the original phrasing is preserved and we can still create a row
        that's useful as a record (even if not chartable).

        `lift_id` is the SQLite lifts.id of the row this Notion entry
        corresponds to. When provided, we prefix Notes with
        `[liftrow:<id>]` — the same dedup-marker pattern used for Strava
        activities. The recurring reconciliation pass scans for this
        marker to figure out which SQLite lifts haven't made it into
        Notion yet, so we don't write duplicates when the real-time push
        and the safety-net pass both fire.
        """
        if not self.is_configured_lifts():
            logger.debug("Notion Lifts DB not configured — skipping.")
            return None

        # Auto-create / fetch the Schedule row for this date so we can wire
        # the Relation. If Schedule isn't configured, skip the relation and
        # still write the Lift row.
        schedule_page_id = await self.find_or_create_schedule(
            date=date, training_group="Lift", workout=workout
        )

        # Prefix the dedup marker so reconciliation can detect this row by
        # SQLite id even when the user later edits the notes manually.
        if lift_id is not None:
            marker = f"[liftrow:{lift_id}]"
            notes = f"{marker} {notes}" if notes else marker

        props: dict = {}
        title = f"{exercise} {date}" if exercise else f"Lift {date}"
        _set_title(props, "Name", title)
        _set_date(props, "Date", date)
        _set_select(props, "Workout", workout, _VALID_WORKOUTS_LIFT, fallback="Other")
        _set_rich_text(props, "Exercise", exercise)
        _set_number(props, "Sets", sets)
        _set_number(props, "Reps", reps)
        _set_number(props, "Weight (lb)", weight_lb)
        _set_number(props, "RPE", rpe)
        _set_rich_text(props, "Notes", notes)
        if schedule_page_id:
            props["Day"] = {"relation": [{"id": schedule_page_id}]}

        page = await self._create_page(self.lifts_db_id, props)
        if page:
            logger.debug("Notion Lift row created: %s (%s).", exercise, date)
            return page.get("id")
        return None

    # ── Lift Sets DB writes ─────────────────────────────────────────────────

    async def log_lift_set(
        self,
        *,
        date: str,
        exercise: str,
        set_number: int,
        reps: int | None = None,
        weight_lb: float | None = None,
        equipment: str | None = None,
        to_failure: bool = False,
        rpe: float | None = None,
        notes: str | None = None,
        source: str = "chat",
        parent_lift_page_id: str | None = None,
        lift_set_id: int | None = None,
    ) -> str | None:
        """Write one Lift Sets row. Returns the new Notion page id on
        success, None on failure (or when the DB isn't configured).

        Each row represents ONE set of one exercise. The Parent Lift
        relation links it back to the workout-summary row in the Lifts DB
        — set this from the page_id returned by log_lift().

        `lift_set_id` is the SQLite lift_sets.id of the corresponding row.
        When provided, we embed `[liftsetrow:<id>]` in Notes for the same
        dedup-marker pattern as log_lift — backfill / reconciliation can
        scan for this to avoid double-writing.

        Equipment is select-validated against a small whitelist; values
        outside the list are silently dropped (better than crashing on a
        new equipment type the user hasn't added to the Notion column).
        """
        if not self.is_configured_lift_sets():
            logger.debug("Notion Lift Sets DB not configured — skipping.")
            return None

        if lift_set_id is not None:
            marker = f"[liftsetrow:{lift_set_id}]"
            notes = f"{marker} {notes}" if notes else marker

        props: dict = {}
        title = f"{exercise} · Set {set_number} · {date}"
        _set_title(props, "Name", title)
        _set_date(props, "Date", date)
        _set_rich_text(props, "Exercise", exercise)
        _set_number(props, "Set Number", set_number)
        _set_number(props, "Reps", reps)
        _set_number(props, "Weight (lb)", weight_lb)
        _set_select(
            props, "Equipment", equipment,
            {"Barbell", "Dumbbell", "Machine", "Cable", "Bodyweight", "Trap bar"},
            fallback=None,
        )
        # To Failure is a Checkbox in Notion — set directly, no helper needed.
        props["To Failure"] = {"checkbox": bool(to_failure)}
        _set_number(props, "RPE", rpe)
        _set_rich_text(props, "Notes", notes)
        _set_select(
            props, "Source", source,
            {"liftstart", "chat", "backfill"},
            fallback="chat",
        )
        if parent_lift_page_id:
            props["Parent Lift"] = {"relation": [{"id": parent_lift_page_id}]}

        page = await self._create_page(self.lift_sets_db_id, props)
        if page:
            logger.debug(
                "Notion Lift Set row created: %s set %s (%s).",
                exercise, set_number, date,
            )
            return page.get("id")
        return None

    # ── Runs DB writes ──────────────────────────────────────────────────────

    async def log_run(
        self,
        *,
        date: str,
        name: str,
        type: str = "Run",
        distance_mi: float | None = None,
        pace: str | None = None,
        duration_min: float | None = None,
        avg_hr: float | None = None,
        elevation_gain_ft: float | None = None,
        zone_1_pct: float | None = None,
        zone_2_pct: float | None = None,
        zone_3_pct: float | None = None,
        zone_4_pct: float | None = None,
        zone_5_pct: float | None = None,
        source: str = "Manual",
        notes: str | None = None,
    ) -> bool:
        """Write one Runs row (covers all cardio — runs, rides, hikes, swims, walks).

        Returns True on success, False on failure.
        """
        if not self.is_configured_runs():
            logger.debug("Notion Runs DB not configured — skipping.")
            return False

        # Match to a Schedule row for this date (Training Group='Run' for
        # cardio — the user can edit the schedule entry if they want
        # Cross-train or something more specific).
        schedule_page_id = await self.find_or_create_schedule(
            date=date, training_group="Run", workout=None
        )

        props: dict = {}
        _set_title(props, "Name", name)
        _set_date(props, "Date", date)
        _set_select(props, "Type", type, _VALID_RUN_TYPES, fallback="Run")
        _set_select(props, "Source", source, _VALID_SOURCES, fallback="Manual")
        _set_number(props, "Distance (mi)", distance_mi)
        _set_rich_text(props, "Pace (time/mi)", pace)
        _set_number(props, "Duration (min)", duration_min)
        _set_number(props, "Avg HR", avg_hr)
        _set_number(props, "Elevation Gain (ft)", elevation_gain_ft)
        _set_number(props, "Zone 1 %", zone_1_pct)
        _set_number(props, "Zone 2 %", zone_2_pct)
        _set_number(props, "Zone 3 %", zone_3_pct)
        _set_number(props, "Zone 4 %", zone_4_pct)
        _set_number(props, "Zone 5 %", zone_5_pct)
        _set_rich_text(props, "Notes", notes)
        if schedule_page_id:
            props["Day"] = {"relation": [{"id": schedule_page_id}]}

        page = await self._create_page(self.runs_db_id, props)
        if page:
            logger.debug("Notion Run row created: %s %s (%s).", type, name, date)
            return True
        return False

    async def log_strava_activity(
        self,
        activity: dict,
        zones: list[dict] | None = None,
        whoop_workout: dict | None = None,
    ) -> bool:
        """Convert a raw Strava activity dict → Runs row. Returns True on
        success, False on failure (so backfill scripts can detect silent
        write failures rather than logging OK on a Notion 400).

        `zones` is the optional /activities/{id}/zones Strava response. Pass
        it when you have it; we'll populate Zone 1–5 % columns from Strava's
        zone math.

        `whoop_workout` is the OPTIONAL matched WHOOP workout row from
        SQLite (use Database.find_whoop_workout_for_strava_activity to look
        it up). When provided, WHOOP HR and WHOOP-derived zone percentages
        take precedence over the Strava equivalents — WHOOP's continuous
        wrist HR with user-tuned zones is the more authoritative source.
        Falls back to Strava data when WHOOP didn't capture the workout.

        Without either zone source, columns stay blank (not zero — that
        would falsely imply you spent 0% in each zone rather than 'data
        unavailable').

        We embed a `[strava:<id>]` marker in the Notes field so backfill
        scripts (and any future dedup logic) can detect which Strava
        activities have already been imported without needing a side table.
        """
        sport_raw = activity.get("sport_type", activity.get("type", "Run"))
        activity_id = activity.get("id")
        marker = f"[strava:{activity_id}]" if activity_id else None

        if sport_raw in _STRAVA_LIFT_TYPES:
            # Lifting tracked in Strava doesn't come with sets/reps/weight
            # structure, so this path isn't very useful today. Log to Lifts
            # with the activity name and defer to the chat-log path for
            # detail.
            lift_notes = "Auto-logged from Strava WeightTraining — set/rep detail not captured by Strava."
            if marker:
                lift_notes = f"{marker} {lift_notes}"
            # log_lift returns the page_id on success / None on failure;
            # this function's declared return is bool, so coerce.
            return bool(await self.log_lift(
                date=(activity.get("start_date_local") or "")[:10],
                exercise=activity.get("name") or "WeightTraining",
                notes=lift_notes,
            ))

        distance_mi = _meters_to_miles(activity.get("distance"))
        duration_min = _seconds_to_minutes(activity.get("moving_time"))
        elevation_ft = _meters_to_feet(activity.get("total_elevation_gain"))
        type_label = _STRAVA_TYPE_MAP.get(sport_raw, "Run")
        avg_pace = _format_pace(duration_min, distance_mi) if type_label in ("Run", "Hike", "Walk") else None

        # Source priority: WHOOP > Strava-zones > nothing.
        whoop_zone_pcts = zone_pcts_from_whoop_row(whoop_workout)
        if whoop_zone_pcts:
            zone_pcts = whoop_zone_pcts
            source = "WHOOP"
        else:
            strava_zone_pcts, zone_source = _zones_from_strava_distribution(
                zones, activity.get("moving_time")
            )
            zone_pcts = strava_zone_pcts or {}
            source = "Strava"
            if zone_source is None and zones:
                # We were handed a zones payload and still produced nothing.
                # This is the silent path that left Zone 1-5 % null on every
                # WHOOP-uploaded run — name the types Strava actually sent so
                # the next unknown zone type is a one-line diagnosis.
                obs.log_event(
                    logger,
                    logging.WARNING,
                    "notion.zones_unusable",
                    activity_id=activity_id,
                    zone_types=[
                        z.get("type") for z in zones if isinstance(z, dict)
                    ],
                )
            elif zone_source == "pace":
                # Pace zones share the Zone 1-5 % columns with HR zones but
                # mean something different. Debug, not warning: for this user
                # it is the normal steady state, not a fault.
                logger.debug(
                    "Notion Zone 1-5 %% sourced from Strava pace zones for %s.",
                    activity_id,
                )

        # HR: prefer WHOOP avg when the workout matched; fall back to Strava.
        avg_hr = (whoop_workout or {}).get("average_hr") or activity.get("average_heartrate")

        return await self.log_run(
            date=(activity.get("start_date_local") or "")[:10],
            name=activity.get("name") or sport_raw,
            type=type_label,
            distance_mi=distance_mi,
            pace=avg_pace,
            duration_min=duration_min,
            avg_hr=avg_hr,
            elevation_gain_ft=elevation_ft,
            zone_1_pct=zone_pcts.get("zone_1_pct"),
            zone_2_pct=zone_pcts.get("zone_2_pct"),
            zone_3_pct=zone_pcts.get("zone_3_pct"),
            zone_4_pct=zone_pcts.get("zone_4_pct"),
            zone_5_pct=zone_pcts.get("zone_5_pct"),
            source=source,
            notes=marker,  # so we can detect already-imported activities on re-run
        )

    # ── Daily Log writes ────────────────────────────────────────────────────

    async def log_daily_entry(self, date: str, summary: dict) -> bool:
        """Write one Daily Log row. Returns True on success, False on failure.

        summary keys (all optional):
          recovery_score, hrv, rhr, sleep_hours, sleep_efficiency (numbers)
          notes, daily_brief (strings)
        """
        if not self.is_configured_daily():
            logger.debug("Notion Daily Log DB not configured — skipping.")
            return False

        schedule_page_id = await self.find_or_create_schedule(date=date)

        props: dict = {}
        _set_title(props, "Date", date)
        _set_date(props, "Day", date)
        _set_number(props, "Recovery Score", summary.get("recovery_score"))
        _set_number(props, "HRV (ms)", summary.get("hrv"))
        _set_number(props, "RHR", summary.get("rhr"))
        _set_number(props, "Sleep (hrs)", summary.get("sleep_hours"))
        _set_number(props, "Sleep Efficiency", summary.get("sleep_efficiency"))
        _set_rich_text(props, "Notes", summary.get("notes"))
        _set_rich_text(props, "Daily Brief", summary.get("daily_brief"))
        if schedule_page_id:
            props["Schedule"] = {"relation": [{"id": schedule_page_id}]}

        page = await self._create_page(self.daily_db_id, props)
        if page:
            logger.debug("Notion Daily Log row created for %s.", date)
            return True
        return False

    # ── Reconciliation — fill gaps the real-time path missed ────────────────
    #
    # Background: the real-time push (Strava webhook for runs, in-chat
    # parser + /liftstart for lifts) writes both SQLite AND Notion. But
    # the webhook can be down, the subscription can expire silently, a
    # Notion call can transiently 5xx — and historically those failures
    # left the row in SQLite with no Notion counterpart and no retry.
    # The nightly sync now writes activities to Notion too, but it ALSO
    # invokes this reconciliation pass as a final backstop: anything
    # SQLite has but Notion doesn't (matched by dedup markers in Notes)
    # gets written here. Idempotent — safe to run as often as we like.

    async def _query_pages_since(
        self, db_id: str, date_property: str, since_iso: str
    ) -> list[dict]:
        """Paginate POST /v1/databases/{id}/query for rows where the given
        date property is on/after `since_iso`. Returns the raw page dicts."""
        out: list[dict] = []
        cursor: str | None = None
        while True:
            body: dict = {
                "page_size": 100,
                "filter": {
                    "property": date_property,
                    "date": {"on_or_after": since_iso},
                },
            }
            if cursor:
                body["start_cursor"] = cursor
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(
                        f"{NOTION_BASE}/databases/{db_id}/query",
                        headers=self._headers(),
                        json=body,
                    )
            except Exception as e:
                logger.warning(f"Notion reconcile query network error: {e}")
                return out
            if resp.status_code != 200:
                logger.warning(
                    f"Notion reconcile query {db_id} failed "
                    f"{resp.status_code}: {resp.text[:200]}"
                )
                return out
            data = resp.json()
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    @staticmethod
    def _notes_text(page: dict) -> str:
        """Pull the concatenated plain_text of the 'Notes' property out of a
        Notion page. Returns '' when the property is absent or empty."""
        notes = page.get("properties", {}).get("Notes", {})
        rt = notes.get("rich_text") or []
        return "".join(r.get("plain_text", "") for r in rt)

    async def existing_strava_markers(self, days: int = 7) -> set[str]:
        """Strava activity ids already present in Notion, from BOTH databases.

        WeightTraining activities are routed to the LIFTS DB by
        log_strava_activity, so their [strava:<id>] markers live there, not in
        Runs. Any caller that checks only one database will happily re-create
        every row it can't see — which is exactly how the Lifts DB ended up
        with 93 duplicated activities and the Runs DB gained one per morning.
        """
        since = (datetime.now().date() - timedelta(days=days)).isoformat()
        ids: set[str] = set()
        for configured, db_id in (
            (self.is_configured_runs(), self.runs_db_id),
            (self.is_configured_lifts(), self.lifts_db_id),
        ):
            if not configured:
                continue
            for page in await self._query_pages_since(db_id, "Date", since):
                for m in _STRAVA_MARKER.finditer(self._notes_text(page)):
                    ids.add(m.group(1))
        return ids

    async def reconcile_recent(self, db, days: int = 7) -> dict:
        """Find SQLite lifts + Strava activities that don't have a Notion
        counterpart in the last `days` days, and write the missing ones.

        Dedup keys: `[liftrow:<sqlite_id>]` for lifts, `[strava:<id>]` for
        activities. Both are embedded in the Notion Notes field by the
        primary writers. Anything in SQLite whose marker isn't found in
        Notion gets written. Anything in Notion with no matching SQLite
        row is left alone — this pass only fills gaps, never deletes.

        Returns {'lifts': N_written, 'activities': N_written}.
        """
        result = {"lifts": 0, "activities": 0}
        since = (datetime.now().date() - timedelta(days=days)).isoformat()

        # Fetch the recent Lifts pages ONCE. We read TWO marker types out of
        # them:
        #   [liftrow:<id>] → lift dedup (below)
        #   [strava:<id>]  → Strava 'WeightTraining' activities are routed to
        #                    the LIFTS DB by log_strava_activity (not Runs), so
        #                    their dedup markers live here. The Strava dedup
        #                    further down MUST see them, or every nightly run
        #                    re-creates a fresh WeightTraining row — the
        #                    compounding-duplicate bug found in the June 2026
        #                    audit ("Lunch Weight Training" copied once per
        #                    night across the 7-day window).
        lift_pages: list[dict] = []
        if self.is_configured_lifts():
            lift_pages = await self._query_pages_since(
                self.lifts_db_id, "Date", since
            )

        existing_strava_in_lifts: set[str] = set()
        for page in lift_pages:
            for m in _STRAVA_MARKER.finditer(self._notes_text(page)):
                existing_strava_in_lifts.add(m.group(1))

        # ── Lifts ───────────────────────────────────────────────────────────
        if self.is_configured_lifts():
            # 1. What's already in Notion (by [liftrow:<id>] marker)?
            existing_lift_ids: set[int] = set()
            for page in lift_pages:
                text = self._notes_text(page)
                for m in _LIFTROW_MARKER.finditer(text):
                    try:
                        existing_lift_ids.add(int(m.group(1)))
                    except ValueError as e:
                        # A marker we can't parse is a marker we can't dedup
                        # on, so this lift gets written to Notion a second
                        # time. Silent `pass` is how duplicates breed.
                        obs.source_failed(
                            logger,
                            "notion_liftrow_marker",
                            "reconcile_recent",
                            e,
                            marker=m.group(0)[:40],
                        )

            # 2. What does SQLite have for the same window?
            try:
                sqlite_lifts = await db.get_recent_lifts(days=days)
            except Exception as e:
                logger.warning(f"reconcile_recent: SQLite lifts read failed: {e}")
                sqlite_lifts = []

            # 3. Write whatever's in SQLite but not in Notion.
            for lift in sqlite_lifts:
                lid = lift.get("id")
                if not lid or lid in existing_lift_ids:
                    continue
                try:
                    ok = await self.log_lift(
                        date=lift.get("date"),
                        exercise=lift.get("exercise") or "lift",
                        notes=lift.get("raw_message") or lift.get("details") or "",
                        lift_id=lid,
                    )
                except Exception as e:
                    logger.warning(
                        f"reconcile_recent: log_lift({lid}) raised {e}"
                    )
                    continue
                if ok:
                    result["lifts"] += 1

        # ── Strava activities ───────────────────────────────────────────────
        if self.is_configured_runs():
            # Seed with the [strava:<id>] markers already found in the Lifts DB
            # (WeightTraining lands there) so we don't re-create those rows.
            existing_strava_ids: set[str] = set(existing_strava_in_lifts)
            for page in await self._query_pages_since(
                self.runs_db_id, "Date", since
            ):
                text = self._notes_text(page)
                for m in _STRAVA_MARKER.finditer(text):
                    existing_strava_ids.add(m.group(1))

            try:
                sqlite_acts = await db.get_strava_activities_range(
                    since, datetime.now().date().isoformat()
                )
            except Exception as e:
                logger.warning(f"reconcile_recent: SQLite acts read failed: {e}")
                sqlite_acts = []

            for row in sqlite_acts:
                aid = row.get("activity_id")
                if not aid or str(aid) in existing_strava_ids:
                    continue
                # The DB row's raw_json field has the full Strava payload that
                # log_strava_activity expects (start_date_local, distance,
                # moving_time, …). Fall back to a thin dict built from the
                # SQLite columns if raw_json is missing.
                payload: dict = {}
                raw = row.get("raw_json")
                if raw:
                    try:
                        payload = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception as e:
                        # We fall back to the thin column-built dict below,
                        # which has no _zones and no detailed HR — the row
                        # gets written looking fine but thinner than it should.
                        obs.source_failed(
                            logger,
                            "strava_raw_json",
                            "reconcile_recent",
                            e,
                            activity_id=aid,
                        )
                        payload = {}
                if not payload:
                    payload = {
                        "id": aid,
                        "sport_type": row.get("sport_type"),
                        "name": row.get("name"),
                        "distance": row.get("distance_m"),
                        "moving_time": row.get("moving_time_s"),
                        "total_elevation_gain": row.get("total_elevation_gain_m"),
                        "average_heartrate": row.get("average_hr"),
                        "max_heartrate": row.get("max_hr"),
                        "start_date_local": (row.get("date") or "") + "T00:00:00",
                    }
                else:
                    # Make sure the id is set (some payloads use 'id', others
                    # don't include it after upsert) — the marker needs it.
                    payload.setdefault("id", aid)

                try:
                    whoop_match = await db.find_whoop_workout_for_strava_activity(row)
                except Exception as e:
                    # No match means the row loses WHOOP HR and WHOOP zone
                    # percentages and falls back to whatever Strava has, which
                    # for these activities is often nothing at all.
                    obs.source_failed(
                        logger,
                        "whoop_workout_match",
                        "reconcile_recent",
                        e,
                        activity_id=aid,
                    )
                    whoop_match = None
                try:
                    ok = await self.log_strava_activity(
                        payload, whoop_workout=whoop_match
                    )
                except Exception as e:
                    logger.warning(
                        f"reconcile_recent: log_strava_activity({aid}) raised {e}"
                    )
                    continue
                if ok:
                    result["activities"] += 1

        # One summary line for the whole pass. The per-row "row created"
        # lines are debug now, so this is the only thing an INFO-level
        # journal shows for a reconcile — and it carries the counts.
        obs.log_event(
            logger,
            logging.INFO,
            "notion.reconcile_done",
            days=days,
            lifts_written=result["lifts"],
            activities_written=result["activities"],
        )
        return result
