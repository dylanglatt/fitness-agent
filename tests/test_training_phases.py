"""Tests for training phases — the season structure the plan never had.

The gap: training_plans held one weekly template with no concept of a season.
That is fine for one goal and fatal for three that compete for the same
recovery budget. Without a phase the coach optimizes marathon, bench and fat
loss every single morning, and the failure is silent.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai.coach import _render_phase_block  # noqa: E402
from data.database import Database  # noqa: E402

STRENGTH = dict(
    name="Strength Block",
    focus="strength",
    start_date="2026-08-28",
    end_date="2027-04-30",
    primary_goal="Bench e1RM",
    secondary_mode="Running at base only",
    run_volume_target="20-30 mi/wk",
    lift_target="4x/wk",
    nutrition_mode="slight deficit",
    notes="squat pattern missing",
)
MARATHON = dict(
    name="Marathon Block",
    focus="marathon",
    start_date="2027-07-01",
    end_date="2027-10-24",
    primary_goal="40-50 mi/wk",
    secondary_mode="Lifting maintenance only",
)


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
        asyncio.run(self.db.initialize())
        asyncio.run(self.db.upsert_training_phase(**STRENGTH))
        asyncio.run(self.db.upsert_training_phase(**MARATHON))

    def test_active_phase_by_date(self):
        p = asyncio.run(self.db.get_active_phase("2026-10-01"))
        self.assertEqual(p["name"], "Strength Block")
        p = asyncio.run(self.db.get_active_phase("2027-08-01"))
        self.assertEqual(p["name"], "Marathon Block")

    def test_unphased_dates_return_none(self):
        # The gap between the two seeded phases, and before the season starts.
        self.assertIsNone(asyncio.run(self.db.get_active_phase("2027-05-15")))
        self.assertIsNone(asyncio.run(self.db.get_active_phase("2020-01-01")))

    def test_next_phase(self):
        n = asyncio.run(self.db.get_next_phase("2026-10-01"))
        self.assertEqual(n["name"], "Marathon Block")
        self.assertIsNone(asyncio.run(self.db.get_next_phase("2027-11-01")))

    def test_upsert_is_idempotent_by_name(self):
        asyncio.run(self.db.upsert_training_phase(**STRENGTH))
        asyncio.run(
            self.db.upsert_training_phase(**{**STRENGTH, "primary_goal": "changed"})
        )
        rows = asyncio.run(self.db.list_training_phases())
        self.assertEqual(len(rows), 2)
        active = asyncio.run(self.db.get_active_phase("2026-10-01"))
        self.assertEqual(active["primary_goal"], "changed")

    def test_overlap_resolves_to_the_later_start(self):
        asyncio.run(
            self.db.upsert_training_phase(
                name="Injury Detour",
                focus="recovery",
                start_date="2026-09-15",
                end_date="2026-10-15",
                primary_goal="Rehab",
            )
        )
        p = asyncio.run(self.db.get_active_phase("2026-10-01"))
        self.assertEqual(p["name"], "Injury Detour")

    def test_render_names_primary_and_secondary(self):
        p = asyncio.run(self.db.get_active_phase("2026-10-01"))
        n = asyncio.run(self.db.get_next_phase("2026-10-01"))
        block = _render_phase_block(p, n, date(2026, 10, 1))
        self.assertIn("STRENGTH-FOCUSED", block)
        self.assertIn("PRIMARY", block)
        self.assertIn("SECONDARY", block)
        self.assertIn("NEXT PHASE: Marathon Block", block)
        self.assertIn("weeks remaining", block)
        # The instruction that stops the model chasing everything at once.
        self.assertIn("maintenance", block.lower())

    def test_render_empty_when_unphased(self):
        self.assertEqual(_render_phase_block(None, None, date(2026, 10, 1)), "")


if __name__ == "__main__":
    unittest.main()
