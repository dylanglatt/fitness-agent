"""Unit tests for the strength-progression block.

The gap this closes: lift_sets carried weight/reps all along and e1RM was
computable, but only through an LLM tool gated behind allow_tools — and
daily_brief runs allow_tools=False, so the brief could never reach it. Lifts
arrived at the model as free text with no numbers at all.
"""

import asyncio, os, sys, tempfile, unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai.coach import _render_strength_progression  # noqa: E402
from data.database import Database  # noqa: E402


def _d(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


class StrengthProgressionTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
        asyncio.run(self.db.initialize())

    def _log(self, date, exercise, sets):
        async def go():
            lid = await self.db.log_lift(date=date, exercise=exercise, details="x")
            for i, (w, reps) in enumerate(sets, 1):
                await self.db.log_lift_set(
                    lift_id=lid,
                    date=date,
                    exercise=exercise,
                    set_number=i,
                    reps=reps,
                    weight_lb=w,
                    source="test",
                )

        asyncio.run(go())

    def test_empty_db_renders_nothing(self):
        rows = asyncio.run(self.db.get_strength_progression())
        self.assertEqual(rows, [])
        self.assertEqual(_render_strength_progression(rows), "")

    def test_top_set_e1rm_and_volume(self):
        self._log(_d(2), "Bench press", [(135, 10), (155, 8), (145, 9)])
        rows = asyncio.run(self.db.get_strength_progression())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["exercise"], "Bench press")
        # Heaviest set by e1RM: 155x8 -> 155*(1+8/30) = 196.3 -> 196
        self.assertEqual(r["last_top_set"], "155 x 8")
        self.assertEqual(r["last_e1rm"], 196)
        self.assertEqual(r["last_n_sets"], 3)
        self.assertEqual(r["last_volume_lb"], round(135 * 10 + 155 * 8 + 145 * 9))

    def test_pr_detection_is_all_time_not_window(self):
        # An old session outside the 12-week window still counts for all-time.
        self._log(_d(200), "Deadlift", [(315, 5)])  # e1RM 367
        self._log(_d(3), "Deadlift", [(275, 5)])  # e1RM 321
        r = asyncio.run(self.db.get_strength_progression())[0]
        self.assertFalse(r["is_pr"])
        self.assertEqual(r["best_e1rm_alltime"], 368)  # 315 * (1 + 5/30) = 367.5
        block = _render_strength_progression([r])
        self.assertIn("all-time 368", block)
        self.assertNotIn("*** PR", block)  # "PROGRESSION" in the header contains "PR"

        # Now beat it.
        self._log(_d(1), "Deadlift", [(325, 5)])  # e1RM 379
        r = asyncio.run(self.db.get_strength_progression())[0]
        self.assertTrue(r["is_pr"])
        self.assertIn("*** PR", _render_strength_progression([r]))

    def test_case_variants_are_one_exercise(self):
        self._log(_d(5), "bench press", [(135, 10)])
        self._log(_d(2), "Bench Press", [(145, 10)])
        rows = asyncio.run(self.db.get_strength_progression())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["n_sessions"], 2)

    def test_bodyweight_sets_are_ignored_not_crashed(self):
        self._log(_d(2), "Pull-up", [(None, 10), (None, 8)])
        self.assertEqual(asyncio.run(self.db.get_strength_progression()), [])

    def test_recent_work_survives_the_limit(self):
        for i in range(8):
            for d in (40, 35, 30):
                self._log(_d(d), f"Filler {i}", [(100, 5)])
        self._log(_d(1), "Deadlift", [(275, 5)])  # trained this week, 1 session
        rows = asyncio.run(self.db.get_strength_progression(limit=3))
        names = [r["exercise"] for r in rows]
        self.assertIn("Deadlift", names)
        self.assertTrue(rows[0]["trained_recently"])

    def test_trend_needs_four_sessions(self):
        for i, w in enumerate([135, 140, 150, 160]):
            self._log(_d(20 - i * 4), "Squat", [(w, 5)])
        r = asyncio.run(self.db.get_strength_progression())[0]
        self.assertEqual(r["trend"], "up")
        self.assertGreater(r["delta_lb"], 0)


if __name__ == "__main__":
    unittest.main()
