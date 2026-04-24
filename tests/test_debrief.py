"""
Tests for the debrief path + webhook signature verification.

We stub the live WHOOP / Strava / Claude clients so the tests are fully
offline. Focus is on the branching logic:

  1. WHOOP-only debrief when Strava returns empty — HR + zones still surface.
  2. WHOOP + Strava merge when both return — pace AND HR surface, and the
     pairing uses the time-window overlap helper, not the sport_type field.
  3. Strava failure does NOT block WHOOP — the fetch runs via
     asyncio.gather, and a Strava exception must leave the WHOOP-only path
     reachable.
  4. `_verify_whoop_signature` rejects tampered bodies, wrong timestamps,
     wrong secrets, and accepts correct payloads. This is the only wall
     between the public internet and our DB writes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock


# Ensure the project root is on sys.path when the tests are run from inside
# tests/ or via `python -m pytest` from the root.
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

from ai.coach import Coach  # noqa: E402
from data.database import Database  # noqa: E402
from integrations.webhook_server import _verify_whoop_signature  # noqa: E402
from integrations.whoop import WhoopClient  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _make_whoop_workout(
    *,
    start: datetime,
    end: datetime,
    sport_id: int = 0,
    avg_hr: int = 152,
    max_hr: int = 178,
    strain: float = 11.4,
    zone2_ms: int = 10 * 60 * 1000,
    zone3_ms: int = 20 * 60 * 1000,
    zone4_ms: int = 5 * 60 * 1000,
) -> dict:
    """Shape matches the v2 workout payload the normalizer expects."""
    return {
        "id": "w-1234",
        "start": _iso(start),
        "end": _iso(end),
        "sport_id": sport_id,
        "score": {
            "strain": strain,
            "kilojoule": 1800,
            "average_heart_rate": avg_hr,
            "max_heart_rate": max_hr,
            "distance_meter": 8000,
            "altitude_gain_meter": 50,
            "altitude_change_meter": 10,
            "percent_recorded": 99.2,
            "zone_duration": {
                "zone_zero_milli": 0,
                "zone_one_milli": 2 * 60 * 1000,
                "zone_two_milli": zone2_ms,
                "zone_three_milli": zone3_ms,
                "zone_four_milli": zone4_ms,
                "zone_five_milli": 0,
            },
        },
    }


def _make_strava_activity(
    *,
    start: datetime,
    moving_seconds: int = 2100,
    distance_m: float = 8000,
    avg_mps: float = 3.8,
    avg_hr: int | None = None,
) -> dict:
    return {
        "id": 999111,
        "name": "Morning Run",
        "sport_type": "Run",
        "type": "Run",
        "start_date": _iso(start),
        "start_date_local": _iso(start),
        "distance": distance_m,
        "moving_time": moving_seconds,
        "elapsed_time": moving_seconds + 30,
        "total_elevation_gain": 15,
        "average_speed": avg_mps,
        "max_speed": avg_mps * 1.3,
        "average_heartrate": avg_hr,
        "max_heartrate": None,
    }


def _build_coach(db: Database) -> Coach:
    """Coach with stub clients — no network traffic, no Claude calls."""
    cfg = MagicMock()
    cfg.ANTHROPIC_API_KEY = "sk-test"
    cfg.CLAUDE_MODEL = "claude-sonnet-4-6"
    cfg.STRAVA_CLIENT_ID = "0"
    cfg.STRAVA_CLIENT_SECRET = "x"
    cfg.STRAVA_REFRESH_TOKEN = "x"
    cfg.WHOOP_CLIENT_ID = "0"
    cfg.WHOOP_CLIENT_SECRET = "x"
    cfg.WHOOP_REFRESH_TOKEN = "x"
    cfg.NOTION_API_KEY = ""
    cfg.NOTION_SCHEDULE_DATABASE_ID = ""
    cfg.NOTION_LIFTS_DATABASE_ID = ""
    cfg.NOTION_RUNS_DATABASE_ID = ""
    cfg.NOTION_DAILY_DATABASE_ID = ""
    cfg.HOME_LAT = 40.0
    cfg.HOME_LNG = -73.0
    cfg.HOME_CITY = "Test"
    coach = Coach(cfg, db)
    # Short-circuit _ask_claude so we can assert on the data block it was
    # handed instead of hitting Anthropic.
    coach._captured_prompts: list[str] = []

    async def _fake_ask(prompt: str, **kwargs) -> str:
        coach._captured_prompts.append(prompt)
        return "OK"

    coach._ask_claude = _fake_ask  # type: ignore[assignment]
    return coach


class DebriefTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Isolated SQLite file per test run; cleaned up in tearDown.
        self._tmpdir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self._tmpdir, "test.db"))
        await self.db.initialize()
        self.coach = _build_coach(self.db)

    async def asyncTearDown(self):
        try:
            for root, _dirs, files in os.walk(self._tmpdir):
                for f in files:
                    os.unlink(os.path.join(root, f))
            os.rmdir(self._tmpdir)
        except Exception:
            pass

    async def test_whoop_only_when_strava_empty(self):
        """If Strava returns [], debrief still runs off WHOOP HR + zones."""
        start = datetime.now(timezone.utc) - timedelta(hours=2)
        end = start + timedelta(minutes=35)
        whoop_rec = _make_whoop_workout(start=start, end=end)

        self.coach.whoop.get_workouts = AsyncMock(return_value=[whoop_rec])
        self.coach.strava.get_recent_activities = AsyncMock(return_value=[])

        out = await self.coach.debrief_run(hours_back=8)
        self.assertEqual(out, "OK")
        prompt = self.coach._captured_prompts[-1]
        # The data block must name WHOOP as HR source…
        self.assertIn("WHOOP workout", prompt)
        self.assertIn("Avg HR: 152 bpm", prompt)
        self.assertIn("Max HR: 178 bpm", prompt)
        # …and explicitly note that Strava hasn't synced, rather than silently
        # dropping pace (a silent drop was the original bug we're fixing).
        self.assertIn("NOT YET SYNCED", prompt)

    async def test_merge_when_both_present(self):
        """When WHOOP and Strava overlap in time, both data sources appear."""
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        end = start + timedelta(minutes=35)
        whoop_rec = _make_whoop_workout(start=start, end=end)
        # Strava starts 45s after WHOOP — still within the 10-min slack.
        strava_act = _make_strava_activity(
            start=start + timedelta(seconds=45),
            moving_seconds=35 * 60,
        )

        self.coach.whoop.get_workouts = AsyncMock(return_value=[whoop_rec])
        self.coach.strava.get_recent_activities = AsyncMock(return_value=[strava_act])

        await self.coach.debrief_run(hours_back=8)
        prompt = self.coach._captured_prompts[-1]
        # HR from WHOOP…
        self.assertIn("Avg HR: 152 bpm", prompt)
        # …pace from Strava (3.8 m/s ≈ 7:03/mi).
        self.assertIn("Avg pace:", prompt)
        self.assertIn("mi", prompt)  # distance from Strava, in miles
        # And the block NOT saying Strava is missing.
        self.assertNotIn("NOT YET SYNCED", prompt)

    async def test_strava_failure_does_not_block_whoop(self):
        """Strava exception must leave WHOOP path reachable."""
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        end = start + timedelta(minutes=30)
        whoop_rec = _make_whoop_workout(start=start, end=end)

        self.coach.whoop.get_workouts = AsyncMock(return_value=[whoop_rec])
        self.coach.strava.get_recent_activities = AsyncMock(
            side_effect=RuntimeError("503 strava")
        )

        out = await self.coach.debrief_run(hours_back=8)
        self.assertEqual(out, "OK")
        prompt = self.coach._captured_prompts[-1]
        self.assertIn("WHOOP workout", prompt)
        self.assertIn("NOT YET SYNCED", prompt)

    async def test_no_data_returns_readable_message(self):
        """Both sources empty → no Claude call, just a plain honest message."""
        self.coach.whoop.get_workouts = AsyncMock(return_value=[])
        self.coach.strava.get_recent_activities = AsyncMock(return_value=[])

        out = await self.coach.debrief_run(hours_back=8)
        # We should NOT have hit _ask_claude.
        self.assertEqual(self.coach._captured_prompts, [])
        self.assertIn("No recent workout", out)


class WhoopSignatureTests(unittest.TestCase):
    def setUp(self):
        self.secret = "very_secret_key"
        self.body = b'{"user_id":1,"id":"abc","type":"workout.updated"}'
        self.timestamp = "1730000000"

    def _sign(self, *, body: bytes, ts: str, secret: str) -> str:
        mac = hmac.new(
            secret.encode("utf-8"), ts.encode("utf-8") + body, hashlib.sha256
        ).digest()
        return base64.b64encode(mac).decode("ascii")

    def test_valid_signature_accepted(self):
        sig = self._sign(body=self.body, ts=self.timestamp, secret=self.secret)
        self.assertTrue(
            _verify_whoop_signature(self.body, self.timestamp, sig, self.secret)
        )

    def test_tampered_body_rejected(self):
        sig = self._sign(body=self.body, ts=self.timestamp, secret=self.secret)
        tampered = self.body.replace(b"workout.updated", b"workout.deleted")
        self.assertFalse(
            _verify_whoop_signature(tampered, self.timestamp, sig, self.secret)
        )

    def test_wrong_timestamp_rejected(self):
        sig = self._sign(body=self.body, ts=self.timestamp, secret=self.secret)
        self.assertFalse(
            _verify_whoop_signature(self.body, "1730000001", sig, self.secret)
        )

    def test_wrong_secret_rejected(self):
        sig = self._sign(body=self.body, ts=self.timestamp, secret="other_secret")
        self.assertFalse(
            _verify_whoop_signature(self.body, self.timestamp, sig, self.secret)
        )

    def test_missing_fields_rejected(self):
        # Empty signature, timestamp, body, or secret should all short-circuit.
        for body, ts, sig, sec in [
            (b"", "1", "aaa", "s"),
            (self.body, "", "aaa", "s"),
            (self.body, "1", "", "s"),
            (self.body, "1", "aaa", ""),
        ]:
            self.assertFalse(_verify_whoop_signature(body, ts, sig, sec))

    def test_non_base64_signature_returns_false(self):
        # If the signature is garbage, compare_digest should just return False
        # — NOT raise, because we never want the webhook handler to 500 on a
        # malformed request (that's a DOS vector).
        self.assertFalse(
            _verify_whoop_signature(self.body, self.timestamp, "!not_base64!", self.secret)
        )


class WorkoutNormalizerTests(unittest.TestCase):
    """Regression check on the normalizer — this is what the webhook + the
    nightly sync both hand to the DB upsert, so a silent shape change here
    would corrupt the whoop_workouts table."""

    def test_normalize_workout_shape(self):
        start = datetime(2026, 4, 19, 13, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=30)
        rec = _make_whoop_workout(start=start, end=end, sport_id=0)
        row = WhoopClient.normalize_workout(rec)
        self.assertEqual(row["workout_id"], "w-1234")
        self.assertEqual(row["sport_id"], 0)
        self.assertEqual(row["sport_name"], "Running")
        self.assertEqual(row["start_date"], "2026-04-19")
        self.assertEqual(row["average_hr"], 152)
        self.assertEqual(row["max_hr"], 178)
        self.assertEqual(row["strain"], 11.4)
        # zone_duration should surface in milliseconds as zoneN_ms
        self.assertEqual(row["zone2_ms"], 10 * 60 * 1000)
        self.assertEqual(row["zone3_ms"], 20 * 60 * 1000)

    def test_normalize_handles_unknown_sport_id(self):
        # Any unmapped sport id falls back to "Activity" rather than KeyError.
        start = datetime(2026, 4, 19, 13, 0, tzinfo=timezone.utc)
        rec = _make_whoop_workout(start=start, end=start + timedelta(minutes=10), sport_id=9999)
        row = WhoopClient.normalize_workout(rec)
        self.assertEqual(row["sport_name"], "Activity")


if __name__ == "__main__":
    unittest.main()
