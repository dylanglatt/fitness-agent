"""
WHOOP integration — fetches recovery, sleep, strain, and cycle data.
Uses WHOOP API v2 (v1 was discontinued; see https://developer.whoop.com/docs/developing/v1-v2-migration/).
"""

import httpx
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# v2 keeps the version in the path — base is /developer, endpoints live under /v2/...
WHOOP_BASE = "https://api.prod.whoop.com/developer"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


def _persist_refresh_token_to_env(new_token: str) -> None:
    """
    WHOOP rotates refresh tokens — every successful refresh invalidates the old
    one and returns a new one. If we don't persist it, the bot will fail on the
    NEXT restart with a 400 Bad Request because .env still holds the consumed
    token. This rewrites WHOOP_REFRESH_TOKEN in .env in place.
    """
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    try:
        with open(env_path, "r") as f:
            content = f.read()
        pattern = r"^WHOOP_REFRESH_TOKEN=.*$"
        replacement = f"WHOOP_REFRESH_TOKEN={new_token}"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
        else:
            content += f"\n{replacement}\n"
        with open(env_path, "w") as f:
            f.write(content)
    except Exception as e:
        # Don't crash the bot if we can't write the file — just warn loudly.
        logger.error(
            f"Could not persist rotated WHOOP refresh token to .env: {e}. "
            f"Bot will work this session but will 400 on next restart."
        )


class WhoopClient:
    def __init__(self, config):
        self.client_id = config.WHOOP_CLIENT_ID
        self.client_secret = config.WHOOP_CLIENT_SECRET
        self.refresh_token = config.WHOOP_REFRESH_TOKEN
        self._access_token: Optional[str] = None
        self._token_expires_at: int = 0

    async def _ensure_token(self):
        """Refresh access token if expired.

        WHOOP rotates refresh tokens on every exchange, so we must capture the
        new refresh_token from the response, update our in-memory copy, AND
        persist it to .env so the next process start doesn't use a consumed
        token and hit 400 Bad Request.
        """
        if self._access_token and datetime.utcnow().timestamp() < self._token_expires_at - 60:
            return

        async with httpx.AsyncClient() as client:
            resp = await client.post(TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                # v2 requires the same scopes we originally authorized with;
                # omitting 'scope' here lets WHOOP return the token's existing scopes.
            })
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self._token_expires_at = int(datetime.utcnow().timestamp()) + expires_in

            # Capture the rotated refresh token so we don't burn it on restart.
            new_refresh = data.get("refresh_token")
            if new_refresh and new_refresh != self.refresh_token:
                self.refresh_token = new_refresh
                _persist_refresh_token_to_env(new_refresh)
                logger.info("WHOOP token refreshed (refresh token rotated and persisted).")
            else:
                logger.info("WHOOP token refreshed.")

    def _headers(self):
        return {"Authorization": f"Bearer {self._access_token}"}

    async def get_recovery(self, days: int = 7) -> list[dict]:
        """Fetch recovery records for the last N days."""
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/v2/recovery",
                headers=self._headers(),
                params={"start": start, "limit": 25},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])

    async def get_sleep(self, days: int = 7) -> list[dict]:
        """Fetch sleep records for the last N days."""
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/v2/activity/sleep",
                headers=self._headers(),
                params={"start": start, "limit": 25},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])

    async def get_strain(self, days: int = 7) -> list[dict]:
        """Fetch strain (cycle) records for the last N days.

        NOTE: /v2/cycle is DAY-level, not per-workout. For per-workout HR and
        strain (i.e. "tell me about my run"), use get_workouts / get_workout_by_id
        instead — that's what the debrief relies on.
        """
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/v2/cycle",
                headers=self._headers(),
                params={"start": start, "limit": 25},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])

    async def get_workouts(
        self, hours: int = 24, limit: int = 25
    ) -> list[dict]:
        """Fetch per-workout records from /v2/activity/workout in the last N hours.

        This is the authoritative source for per-run HR, zone time, and workout
        strain — distinct from /v2/cycle which is day-level.

        Requires the `read:workout` scope (granted at OAuth time by whoop_auth.py).
        """
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{WHOOP_BASE}/v2/activity/workout",
                headers=self._headers(),
                params={"start": start, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])

    async def get_workout_by_id(self, workout_id: str) -> Optional[dict]:
        """Fetch a single workout by its v2 UUID.

        Used by the webhook handler when we receive a workout.updated event —
        the event payload only contains the id, so we must fetch the full record.
        Returns None on 404 (workout was deleted before we fetched).
        """
        await self._ensure_token()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{WHOOP_BASE}/v2/activity/workout/{workout_id}",
                headers=self._headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    async def get_body_measurement(self) -> Optional[dict]:
        """Fetch the user's body measurement (height_meter, weight_kilogram, max_heart_rate).

        The v2 API only exposes a single latest value — there is no history
        endpoint, and BF%/lean mass are NOT returned. Third-party scales
        (FitDays etc.) update the app UI but NOT the API payload. Returns
        None on 404 or if the payload is empty.
        """
        await self._ensure_token()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/v2/user/measurement/body",
                headers=self._headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
        return data or None

    async def get_today_snapshot(self, local_tz: Optional[str] = None) -> dict:
        """Get today's recovery, sleep, and strain as a single dict.

        Pre-fix behavior was to return the most recent record from the last
        day regardless of date. That meant when WHOOP hadn't finished
        computing the morning's recovery yet, the brief silently used
        YESTERDAY'S recovery as if it were today's — the "inaccurate
        HRV/RHR in morning briefs" failure mode. Now we filter to records
        whose timestamp (converted to the caller's local tz) falls on
        today's local date. If nothing matches, the field is None and the
        brief honestly says "no recovery yet" instead of confabulating.

        local_tz is an Olson tz string (e.g. "America/New_York"). When
        omitted we fall back to UTC, which is the previous buggy behavior
        but explicit about it.
        """
        recovery = await self.get_recovery(days=2)  # widen to catch late syncs
        sleep = await self.get_sleep(days=2)
        strain = await self.get_strain(days=1)

        # Compute "today" in the caller's local tz. Falls back to UTC if
        # pytz can't load the zone (defensive — bad config shouldn't crash
        # the brief; we just lose date-correctness like before).
        try:
            import pytz
            tz = pytz.timezone(local_tz) if local_tz else pytz.UTC
        except Exception:
            import pytz
            tz = pytz.UTC
        today_local = datetime.now(tz).date()

        def _on_today(rec: dict, ts_keys: tuple[str, ...]) -> bool:
            for k in ts_keys:
                ts = rec.get(k)
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.astimezone(tz).date() == today_local:
                        return True
                except Exception:
                    continue
            return False

        # Recoveries are tied to the sleep they came from; their created_at
        # is when WHOOP computed the score (post-wake). Sleep records use
        # `end` (when the sleep period ended). Strain is per-cycle.
        today_recovery = next(
            (r for r in recovery if _on_today(r, ("created_at", "updated_at"))),
            None,
        )
        today_sleep = next(
            (s for s in sleep if _on_today(s, ("end", "start"))),
            None,
        )
        # Strain stays "most recent" since same-day strain accumulates as
        # the day progresses and the brief fires before strain is "done".
        today_strain = strain[0] if strain else None

        return {
            "recovery": today_recovery,
            "sleep": today_sleep,
            "strain": today_strain,
        }

    # ── Paginated history iterators ─────────────────────────────────────────
    # v2 paginates with `nextToken`. Max `limit` per request is 25.
    # Use these for backfill + multi-week trend fetches.

    async def _iter_paginated(self, path: str, start: Optional[str], end: Optional[str]):
        """Generic v2 paginator. Yields records one at a time until nextToken is None."""
        await self._ensure_token()
        next_token: Optional[str] = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                params: dict = {"limit": 25}
                if start:
                    params["start"] = start
                if end:
                    params["end"] = end
                if next_token:
                    params["nextToken"] = next_token
                # Token might expire mid-backfill; refresh if needed.
                await self._ensure_token()
                resp = await client.get(
                    f"{WHOOP_BASE}{path}", headers=self._headers(), params=params
                )
                resp.raise_for_status()
                payload = resp.json()
                records = payload.get("records", [])
                for rec in records:
                    yield rec
                next_token = payload.get("next_token") or payload.get("nextToken")
                if not next_token or not records:
                    return

    async def iter_all_recovery(self, start: Optional[str] = None, end: Optional[str] = None):
        async for rec in self._iter_paginated("/v2/recovery", start, end):
            yield rec

    async def iter_all_sleep(self, start: Optional[str] = None, end: Optional[str] = None):
        async for rec in self._iter_paginated("/v2/activity/sleep", start, end):
            yield rec

    async def iter_all_cycles(self, start: Optional[str] = None, end: Optional[str] = None):
        async for rec in self._iter_paginated("/v2/cycle", start, end):
            yield rec

    async def iter_all_workouts(
        self, start: Optional[str] = None, end: Optional[str] = None
    ):
        """Paginate through /v2/activity/workout. Yields records one at a time."""
        async for rec in self._iter_paginated("/v2/activity/workout", start, end):
            yield rec

    # ── Row normalizers — raw WHOOP JSON → flat dict ready to upsert ────────

    @staticmethod
    def normalize_recovery(rec: dict) -> tuple[Optional[str], dict]:
        """Return (date, normalized_row) for a v2 recovery record."""
        score = rec.get("score") or {}
        # v2 recoveries are tied to the sleep they're calculated from.
        # The date we index on is the date of the associated sleep end.
        created_at = rec.get("created_at") or rec.get("updated_at") or ""
        date = created_at[:10] if created_at else None
        return date, {
            "recovery_score": score.get("recovery_score"),
            "hrv_rmssd_ms": score.get("hrv_rmssd_milli"),
            "resting_hr": score.get("resting_heart_rate"),
            "spo2_pct": score.get("spo2_percentage"),
            "skin_temp_c": score.get("skin_temp_celsius"),
            "cycle_id": str(rec.get("cycle_id")) if rec.get("cycle_id") is not None else None,
            "sleep_id": rec.get("sleep_id"),
        }

    @staticmethod
    def normalize_sleep(rec: dict) -> tuple[Optional[str], dict]:
        score = rec.get("score") or {}
        stage = score.get("stage_summary") or {}
        # Use the end of the sleep period as the "date" so naps late at night
        # don't fight with the morning main sleep. If both end on the same day,
        # upsert keeps the most recent.
        end = rec.get("end") or rec.get("start") or ""
        date = end[:10] if end else None
        to_hours = lambda ms: round((ms or 0) / 3_600_000, 2) if ms else None
        return date, {
            "total_in_bed_hours": to_hours(stage.get("total_in_bed_time_milli")),
            "total_asleep_hours": to_hours(
                (stage.get("total_in_bed_time_milli") or 0)
                - (stage.get("total_awake_time_milli") or 0)
            ),
            "sleep_efficiency_pct": score.get("sleep_efficiency_percentage"),
            "sleep_performance_pct": score.get("sleep_performance_percentage"),
            "sleep_consistency_pct": score.get("sleep_consistency_percentage"),
            "disturbance_count": score.get("num_disturbances"),
            "rem_hours": to_hours(stage.get("total_rem_sleep_time_milli")),
            "sws_hours": to_hours(stage.get("total_slow_wave_sleep_time_milli")),
            "light_hours": to_hours(stage.get("total_light_sleep_time_milli")),
            "sleep_id": rec.get("id"),
        }

    @staticmethod
    def normalize_cycle(rec: dict) -> tuple[Optional[str], dict]:
        score = rec.get("score") or {}
        start = rec.get("start") or ""
        date = start[:10] if start else None
        return date, {
            "strain": score.get("strain"),
            "kilojoule": score.get("kilojoule"),
            "average_hr": score.get("average_heart_rate"),
            "max_hr": score.get("max_heart_rate"),
            "cycle_id": str(rec.get("id")) if rec.get("id") is not None else None,
        }

    # WHOOP v2 sport_id → human name. Covers the common ones; falls back to
    # "Activity" for ids we don't have mapped. Full list is at
    # https://developer.whoop.com/docs/developing/user-data/workout/ .
    _SPORT_ID_TO_NAME = {
        -1: "Activity",
        0: "Running",
        1: "Cycling",
        16: "Baseball",
        17: "Basketball",
        18: "Rowing",
        19: "Fencing",
        20: "Field Hockey",
        21: "Football",
        22: "Golf",
        24: "Ice Hockey",
        25: "Lacrosse",
        27: "Rugby",
        28: "Sailing",
        29: "Skiing",
        30: "Soccer",
        31: "Softball",
        32: "Squash",
        33: "Swimming",
        34: "Tennis",
        35: "Track & Field",
        36: "Volleyball",
        37: "Water Polo",
        38: "Wrestling",
        39: "Boxing",
        42: "Dance",
        43: "Pilates",
        44: "Yoga",
        45: "Weightlifting",
        47: "Cross Country Skiing",
        48: "Functional Fitness",
        49: "Duathlon",
        51: "Gymnastics",
        52: "Hiking/Rucking",
        53: "Horseback Riding",
        55: "Kayaking",
        56: "Martial Arts",
        57: "Mountain Biking",
        59: "Powerlifting",
        60: "Rock Climbing",
        61: "Paddleboarding",
        62: "Triathlon",
        63: "Walking",
        64: "Surfing",
        65: "Elliptical",
        66: "Stairmaster",
        70: "Meditation",
        71: "Other",
        73: "Diving",
        74: "Operations - Tactical",
        75: "Operations - Medical",
        76: "Operations - Flying",
        77: "Operations - Water",
        82: "Ultimate",
        83: "Climber",
        84: "Jumping Rope",
        85: "Australian Football",
        86: "Skateboarding",
        87: "Coaching",
        88: "Ice Bath",
        89: "Commuting",
        90: "Gaming",
        91: "Snowboarding",
        92: "Motocross",
        93: "Caddying",
        94: "Obstacle Course Racing",
        95: "Motor Racing",
        96: "HIIT",
        97: "Spin",
        98: "Jiu Jitsu",
        99: "Manual Labor",
        100: "Cricket",
        101: "Pickleball",
        102: "Inline Skating",
        103: "Box Fitness",
        104: "Spikeball",
        105: "Wheelchair Pushing",
        106: "Paddle Tennis",
        107: "Barre",
        108: "Stage Performance",
        109: "High Stress Work",
        110: "Parkour",
        111: "Gaelic Football",
        112: "Hurling / Camogie",
        113: "Circus Arts",
        121: "Massage Therapy",
        125: "Watching Sports",
        126: "Assault Bike",
        127: "Kickboxing",
        128: "Stretching",
        230: "Table Tennis",
        231: "Badminton",
        232: "Netball",
        233: "Sauna",
        234: "Disc Golf",
        235: "Yard Work",
        236: "Air Compression",
        237: "Percussive Massage",
        238: "Paintball",
        239: "Ice Skating",
        240: "Handball",
    }

    @classmethod
    def normalize_workout(cls, rec: dict) -> dict:
        """Flatten a v2 workout record to the row shape that `upsert_whoop_workout`
        expects. Start/end are preserved as ISO strings (UTC). Zone durations
        are pulled out of score.zone_duration for easy querying without re-parsing
        raw JSON.
        """
        score = rec.get("score") or {}
        # WHOOP v2 uses 'zone_durations' (plural). An older revision of this
        # code looked for 'zone_duration' (singular) and silently captured
        # zero zones for every workout — leaving HR populated but zone
        # percentages blank in downstream Notion rows. Accept either form.
        zones = score.get("zone_durations") or score.get("zone_duration") or {}
        start = rec.get("start") or ""
        end = rec.get("end") or ""
        sport_id = rec.get("sport_id")
        return {
            "workout_id": str(rec.get("id")) if rec.get("id") is not None else None,
            "start_date": start[:10] if start else None,
            "start_utc": start,
            "end_utc": end,
            "sport_id": sport_id,
            "sport_name": cls._SPORT_ID_TO_NAME.get(sport_id, "Activity"),
            "strain": score.get("strain"),
            "kilojoule": score.get("kilojoule"),
            "average_hr": score.get("average_heart_rate"),
            "max_hr": score.get("max_heart_rate"),
            "distance_m": score.get("distance_meter"),
            "altitude_gain_m": score.get("altitude_gain_meter"),
            "altitude_change_m": score.get("altitude_change_meter"),
            # Zone duration keys per WHOOP v2: zone_zero_milli..zone_five_milli.
            "zone0_ms": zones.get("zone_zero_milli"),
            "zone1_ms": zones.get("zone_one_milli"),
            "zone2_ms": zones.get("zone_two_milli"),
            "zone3_ms": zones.get("zone_three_milli"),
            "zone4_ms": zones.get("zone_four_milli"),
            "zone5_ms": zones.get("zone_five_milli"),
            "percent_recorded": score.get("percent_recorded"),
        }

    @classmethod
    def summarize_workout(cls, rec: dict) -> str:
        """Readable one-line summary of a workout (raw v2 payload)."""
        if not rec:
            return "No workout."
        row = cls.normalize_workout(rec)
        parts = [f"{row['sport_name']} @ {row['start_utc'][:16]}Z"]
        if row.get("strain") is not None:
            parts.append(f"strain {round(row['strain'], 1)}")
        if row.get("average_hr"):
            parts.append(f"avg HR {int(row['average_hr'])}")
        if row.get("max_hr"):
            parts.append(f"max HR {int(row['max_hr'])}")
        return " | ".join(parts)

    def summarize_recovery(self, record: dict) -> str:
        """Readable summary of a recovery record.

        Each field is rendered as "N/A" when the value is missing rather
        than defaulting to 0 — a literal "HRV: 0.0ms" in the morning brief
        is worse than admitting WHOOP hasn't finished computing the score.
        """
        if not record:
            return "No recovery data."
        score = record.get("score") or {}
        recovery_score = score.get("recovery_score")
        hrv_raw = score.get("hrv_rmssd_milli")
        rhr = score.get("resting_heart_rate")
        rec_s = f"{recovery_score}%" if recovery_score is not None else "N/A"
        hrv_s = f"{round(hrv_raw, 1)}ms" if hrv_raw is not None else "N/A"
        rhr_s = f"{rhr} bpm" if rhr is not None else "N/A"
        return f"Recovery: {rec_s} | HRV: {hrv_s} | RHR: {rhr_s}"

    def summarize_sleep(self, record: dict) -> str:
        """Readable summary of a sleep record."""
        if not record:
            return "No sleep data."
        score = record.get("score", {})
        efficiency = score.get("sleep_efficiency_percentage", "N/A")
        stage_summary = record.get("score", {}).get("stage_summary", {})
        total_ms = stage_summary.get("total_in_bed_time_milli", 0)
        total_hours = round(total_ms / 3_600_000, 1)
        disturbances = score.get("num_disturbances", "N/A")
        return f"Sleep: {total_hours}h | Efficiency: {efficiency}% | Disturbances: {disturbances}"
