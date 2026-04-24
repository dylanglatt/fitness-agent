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

import httpx
import logging

logger = logging.getLogger(__name__)

NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

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


def _zones_from_strava_distribution(
    zones: list[dict] | None,
    total_seconds: float | int | None,
) -> dict[str, float] | None:
    """Convert Strava's zones response to Zone1%..Zone5% dict.

    Strava's GET /activities/{id}/zones returns a list of zone objects with
    'type': 'heartrate' and 'distribution_buckets': [{'min':..,'max':..,'time':..}]
    where time is seconds per bucket. We sum the HR zone bucket times and
    express each as a percent of total. If zones aren't available (no HR
    monitor for the activity), returns None and the Notion columns stay blank.
    """
    if not zones or not total_seconds or total_seconds <= 0:
        return None
    hr_zone = next((z for z in zones if z.get("type") == "heartrate"), None)
    if not hr_zone:
        return None
    buckets = hr_zone.get("distribution_buckets") or []
    if not buckets:
        return None
    # Strava returns up to 5 HR zones; if fewer, the missing zones stay 0.
    pct: dict[str, float] = {}
    for i, bucket in enumerate(buckets[:5]):
        t = bucket.get("time") or 0
        pct[f"zone_{i + 1}_pct"] = round(100.0 * t / total_seconds, 1)
    # Fill any missing zones with 0 so Notion shows 0 rather than blank —
    # makes the "this run was all Z2" vs "missing data" distinction clear.
    for i in range(1, 6):
        pct.setdefault(f"zone_{i}_pct", 0.0)
    return pct


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
                return False, f"Network error reaching Notion: {e}"
        if resp.status_code == 200:
            body = resp.json()
            title_rt = body.get("title") or []
            title = title_rt[0].get("plain_text") if title_rt else "(untitled)"
            return True, f"Connected to '{title}'."
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
        """Create one Schedule row for `date`. Returns the page URL (relation
        target) on success, None otherwise.

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
            logger.info(f"Notion Schedule row created for {date}.")
            return page.get("url") or page.get("id")
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
            logger.debug(f"Notion Schedule query error: {e}")
            return None
        if resp.status_code != 200:
            logger.debug(
                f"Notion Schedule query failed: {resp.status_code} {resp.text[:200]}"
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
    ) -> None:
        """Write one Lifts row.

        All numeric fields are optional — when the chat-log parser can't
        extract structured sets/reps/weight, pass `notes=<raw message>` so
        the original phrasing is preserved and we can still create a row
        that's useful as a record (even if not chartable).
        """
        if not self.is_configured_lifts():
            logger.debug("Notion Lifts DB not configured — skipping.")
            return

        # Auto-create / fetch the Schedule row for this date so we can wire
        # the Relation. If Schedule isn't configured, skip the relation and
        # still write the Lift row.
        schedule_page_id = await self.find_or_create_schedule(
            date=date, training_group="Lift", workout=workout
        )

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
            logger.info(f"Notion Lift row created: {exercise} ({date}).")

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
    ) -> None:
        """Write one Runs row (covers all cardio — runs, rides, hikes, swims, walks)."""
        if not self.is_configured_runs():
            logger.debug("Notion Runs DB not configured — skipping.")
            return

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
            logger.info(f"Notion Run row created: {type} {name} ({date}).")

    async def log_strava_activity(
        self,
        activity: dict,
        zones: list[dict] | None = None,
    ) -> None:
        """Convert a raw Strava activity dict → Runs row.

        `zones` is the optional /activities/{id}/zones response. Pass it when
        you have it; we'll populate Zone 1–5 % columns. Without it those
        columns stay blank (not zero — that would falsely imply you spent
        0% in each zone rather than 'data unavailable').

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
            await self.log_lift(
                date=(activity.get("start_date_local") or "")[:10],
                exercise=activity.get("name") or "WeightTraining",
                notes=lift_notes,
            )
            return

        distance_mi = _meters_to_miles(activity.get("distance"))
        duration_min = _seconds_to_minutes(activity.get("moving_time"))
        elevation_ft = _meters_to_feet(activity.get("total_elevation_gain"))
        type_label = _STRAVA_TYPE_MAP.get(sport_raw, "Run")
        avg_pace = _format_pace(duration_min, distance_mi) if type_label in ("Run", "Hike", "Walk") else None

        zone_pcts = _zones_from_strava_distribution(zones, activity.get("moving_time"))

        await self.log_run(
            date=(activity.get("start_date_local") or "")[:10],
            name=activity.get("name") or sport_raw,
            type=type_label,
            distance_mi=distance_mi,
            pace=avg_pace,
            duration_min=duration_min,
            avg_hr=activity.get("average_heartrate"),
            elevation_gain_ft=elevation_ft,
            zone_1_pct=(zone_pcts or {}).get("zone_1_pct"),
            zone_2_pct=(zone_pcts or {}).get("zone_2_pct"),
            zone_3_pct=(zone_pcts or {}).get("zone_3_pct"),
            zone_4_pct=(zone_pcts or {}).get("zone_4_pct"),
            zone_5_pct=(zone_pcts or {}).get("zone_5_pct"),
            source="Strava",
            notes=marker,  # so we can detect already-imported activities on re-run
        )

    # ── Daily Log writes ────────────────────────────────────────────────────

    async def log_daily_entry(self, date: str, summary: dict) -> None:
        """Write one Daily Log row.

        summary keys (all optional):
          recovery_score, hrv, rhr, sleep_hours, sleep_efficiency (numbers)
          notes, daily_brief (strings)
        """
        if not self.is_configured_daily():
            logger.debug("Notion Daily Log DB not configured — skipping.")
            return

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
            logger.info(f"Notion Daily Log row created for {date}.")
