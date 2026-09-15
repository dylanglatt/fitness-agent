"""Tests for the goal_races table (data/database.py) and its use with
ai/race_periodization.py — set via scripts/set_goal_race.py."""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data.database import Database  # noqa: E402
from ai.race_periodization import training_block, render_race_periodization_block  # noqa: E402


class GoalRaceTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
        asyncio.run(self.db.initialize())

    def test_no_active_race_returns_none(self):
        self.assertIsNone(asyncio.run(self.db.get_active_goal_race()))

    def test_save_and_get_active(self):
        asyncio.run(self.db.save_goal_race(
            "NY Marathon", "2027-11-07", "marathon", peak_weekly_mi=45,
        ))
        race = asyncio.run(self.db.get_active_goal_race())
        self.assertEqual(race["name"], "NY Marathon")
        self.assertEqual(race["race_date"], "2027-11-07")
        self.assertEqual(race["distance"], "marathon")
        self.assertEqual(race["peak_weekly_mi"], 45)
        self.assertEqual(race["status"], "active")

    def test_saving_a_new_active_race_archives_the_old_one(self):
        asyncio.run(self.db.save_goal_race("NY Marathon", "2027-11-07"))
        asyncio.run(self.db.save_goal_race("Boston Marathon", "2028-04-17"))
        race = asyncio.run(self.db.get_active_goal_race())
        self.assertEqual(race["name"], "Boston Marathon")

    def test_draft_race_is_not_active(self):
        asyncio.run(self.db.save_goal_race(
            "Someday 10k", "2028-01-01", "10k", activate=False
        ))
        self.assertIsNone(asyncio.run(self.db.get_active_goal_race()))

    def test_active_race_feeds_the_periodization_render(self):
        asyncio.run(self.db.save_goal_race(
            "NY Marathon", "2026-09-20", "marathon", peak_weekly_mi=45,
        ))
        race = asyncio.run(self.db.get_active_goal_race())
        block_text = render_race_periodization_block(
            race["name"], race["race_date"], race["distance"],
            date(2026, 9, 15), race["peak_weekly_mi"],
        )
        self.assertIn("RACE_WEEK".replace("_", "_"), block_text.upper())
        self.assertIn("MAINTENANCE", block_text)


if __name__ == "__main__":
    unittest.main()
