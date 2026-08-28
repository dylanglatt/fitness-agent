"""Tests for the four brief-quality fixes, each traced to an observed failure
in a real brief Dylan received.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai.coach import (  # noqa: E402
    _render_data_freshness,
    _render_exercise_library,
    _render_yesterday,
)
from data.database import Database  # noqa: E402
from data.exercise_vocab import canonical_exercise  # noqa: E402


def _d(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


class CanonicalNameTests(unittest.TestCase):
    """408 sets carried 84 names for ~35 movements, splitting e1RM histories."""

    def test_plurals_and_spellings_merge(self):
        for a, b in [
            ("barbell row", "barbell rows"),
            ("trap bar deadlift", "trapbar deadlift"),
            ("lateral raise", "lateral raises"),
            ("lat pulldown", "lat pulldowns"),
            ("overhead press", "overhead press (OHP)"),
            ("rear delt fly", "chest supported reverse delt fly"),
        ]:
            self.assertEqual(canonical_exercise(a), canonical_exercise(b), (a, b))

    def test_parser_placeholders_are_rejected(self):
        for junk in (
            "dumbbell exercise",
            "cable machine exercise",
            "triceps",
            "dumbbell exercise (likely dumbbell bench press or similar)",
            "",
            None,
            "   ",
        ):
            self.assertIsNone(canonical_exercise(junk), junk)

    def test_genuinely_different_movements_stay_separate(self):
        self.assertNotEqual(
            canonical_exercise("barbell row"),
            canonical_exercise("single arm dumbbell row"),
        )
        self.assertNotEqual(
            canonical_exercise("bench press"),
            canonical_exercise("incline dumbbell press"),
        )


class ExerciseLibraryTests(unittest.TestCase):
    """The 2026-08-26 brief prescribed back squat, leg press, walking lunges
    and Bulgarian split squats. He has two squat sets, both hack squat."""

    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
        asyncio.run(self.db.initialize())

    def _log(self, date, exercise, n=3):
        async def go():
            lid = await self.db.log_lift(date=date, exercise=exercise, details="x")
            for i in range(1, n + 1):
                await self.db.log_lift_set(
                    lift_id=lid,
                    date=date,
                    exercise=exercise,
                    set_number=i,
                    reps=8,
                    weight_lb=100,
                    source="test",
                )

        asyncio.run(go())

    def test_library_groups_by_pattern_and_merges_names(self):
        self._log(_d(3), "Bench press", 3)
        self._log(_d(3), "barbell rows", 2)
        self._log(_d(10), "Barbell row", 2)
        v = asyncio.run(self.db.get_exercise_vocabulary())
        pull = v["by_pattern"].get("pull", [])
        rows = [i for i in pull if i["exercise"].lower() == "barbell row"]
        self.assertEqual(len(rows), 1, pull)
        self.assertEqual(rows[0]["n_sets"], 4)
        self.assertEqual(rows[0]["n_sessions"], 2)

    def test_untrained_pattern_is_named_not_invented(self):
        self._log(_d(2), "Bench press", 3)
        v = asyncio.run(self.db.get_exercise_vocabulary())
        block = _render_exercise_library(v)
        self.assertIn("NOT TRAINED AT ALL", block)
        self.assertIn("legs", block)
        self.assertIn("PRESCRIBE FROM THIS LIST", block)

    def test_unresolved_sets_are_counted_not_listed(self):
        self._log(_d(2), "dumbbell exercise", 4)
        self._log(_d(2), "Bench press", 2)
        v = asyncio.run(self.db.get_exercise_vocabulary())
        self.assertEqual(v["unresolved_sets"], 4)
        block = _render_exercise_library(v)
        self.assertNotIn("dumbbell exercise", block.lower())
        self.assertIn("could not be identified", block)


class FreshnessTests(unittest.TestCase):
    """Two consecutive briefs said "WHOOP isn't synced" during a 30-day outage."""

    def test_month_long_gap_is_an_outage_with_a_number(self):
        block = _render_data_freshness(
            {
                "whoop_recovery": {"last_date": "2026-07-28", "age_days": 30},
                "strava": {"last_date": "2026-08-26", "age_days": 1},
                "lift_log": {"last_date": "2026-08-25", "age_days": 2},
            }
        )
        self.assertIn("30 DAYS AGO", block)
        self.assertIn("OUTAGE", block)
        self.assertNotIn("Strava activity", block)  # fresh feeds stay quiet

    def test_healthy_feeds_render_nothing(self):
        self.assertEqual(
            _render_data_freshness(
                {
                    "whoop_recovery": {"last_date": "2026-08-27", "age_days": 0},
                    "strava": {"last_date": "2026-08-26", "age_days": 1},
                    "lift_log": {"last_date": "2026-08-26", "age_days": 1},
                }
            ),
            "",
        )

    def test_a_feed_with_no_records_at_all_is_flagged(self):
        block = _render_data_freshness(
            {"whoop_recovery": {"last_date": None, "age_days": None}}
        )
        self.assertIn("NO RECORDS AT ALL", block)


class ContinuityTests(unittest.TestCase):
    """Every brief was a standalone snapshot with no memory of its own advice."""

    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
        asyncio.run(self.db.initialize())

    def test_roundtrip_and_completed_session(self):
        asyncio.run(
            self.db.log_brief(
                date=_d(1),
                planned_kind="lift",
                planned_sub="push",
                prescribed_kind="lift",
                prescribed_sub="pull",
                readiness_status="too_soon",
            )
        )
        prev = asyncio.run(self.db.get_brief_for_date(_d(1)))
        self.assertEqual(prev["prescribed_sub"], "pull")
        block = _render_yesterday(prev, {"lifts": {"barbell row": 4}, "activities": []})
        self.assertIn("lift / pull", block)
        self.assertIn("barbell row x4", block)
        self.assertIn("Do not re-prescribe", block)

    def test_skipped_session_prompts_escalation(self):
        asyncio.run(
            self.db.log_brief(date=_d(1), prescribed_kind="run", prescribed_sub="easy")
        )
        prev = asyncio.run(self.db.get_brief_for_date(_d(1)))
        block = _render_yesterday(prev, {"lifts": {}, "activities": []})
        self.assertIn("NOTHING", block)
        self.assertIn("skipped", block)

    def test_no_prior_brief_renders_nothing(self):
        self.assertEqual(_render_yesterday(None, None), "")

    def test_log_brief_is_idempotent_per_day(self):
        for sub in ("push", "legs"):
            asyncio.run(
                self.db.log_brief(
                    date=_d(1), prescribed_kind="lift", prescribed_sub=sub
                )
            )
        self.assertEqual(
            asyncio.run(self.db.get_brief_for_date(_d(1)))["prescribed_sub"], "legs"
        )


if __name__ == "__main__":
    unittest.main()
