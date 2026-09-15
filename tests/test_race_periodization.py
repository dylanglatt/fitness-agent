"""Tests for ai/race_periodization.py — the goal-race periodization the plan
never had. See module docstring for the gap this closes."""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai.race_periodization import (  # noqa: E402
    training_block, is_lifting_primary, leg_lift_guidance,
    weekly_mileage_guidance, render_race_periodization_block, days_to_race,
    BASE, BUILD, PEAK, TAPER, RACE_WEEK, POST_RACE,
)


class TrainingBlockTests(unittest.TestCase):
    def test_far_out_is_base(self):
        self.assertEqual(training_block(200, "marathon"), BASE)

    def test_marathon_block_boundaries(self):
        # marathon: race_week<=6, taper<=21, peak<=42, build<=112, else base
        self.assertEqual(training_block(6, "marathon"), RACE_WEEK)
        self.assertEqual(training_block(7, "marathon"), TAPER)
        self.assertEqual(training_block(21, "marathon"), TAPER)
        self.assertEqual(training_block(22, "marathon"), PEAK)
        self.assertEqual(training_block(42, "marathon"), PEAK)
        self.assertEqual(training_block(43, "marathon"), BUILD)
        self.assertEqual(training_block(112, "marathon"), BUILD)
        self.assertEqual(training_block(113, "marathon"), BASE)

    def test_race_day_itself_is_race_week(self):
        self.assertEqual(training_block(0, "marathon"), RACE_WEEK)

    def test_past_race_is_post_race_then_base(self):
        self.assertEqual(training_block(-1, "marathon"), POST_RACE)
        self.assertEqual(training_block(-14, "marathon"), POST_RACE)
        self.assertEqual(training_block(-15, "marathon"), BASE)

    def test_unknown_distance_falls_back_to_marathon_thresholds(self):
        self.assertEqual(training_block(10, "ultra"), TAPER)
        self.assertEqual(training_block(10, "marathon"), TAPER)

    def test_shorter_distance_has_a_shorter_taper(self):
        # 5k taper is 5 days, so day 6 is already outside taper -> peak
        self.assertEqual(training_block(6, "5k"), PEAK)
        self.assertEqual(training_block(5, "5k"), TAPER)


class LiftingPrimaryTests(unittest.TestCase):
    def test_base_and_build_and_peak_keep_lifting_primary(self):
        for b in (BASE, BUILD, PEAK):
            self.assertTrue(is_lifting_primary(b), b)

    def test_taper_and_race_week_deprioritize_lifting(self):
        for b in (TAPER, RACE_WEEK):
            self.assertFalse(is_lifting_primary(b), b)

    def test_post_race_keeps_lifting_primary(self):
        self.assertTrue(is_lifting_primary(POST_RACE))


class GuidanceTests(unittest.TestCase):
    def test_leg_guidance_covers_every_block(self):
        for b in (BASE, BUILD, PEAK, TAPER, RACE_WEEK, POST_RACE):
            self.assertTrue(leg_lift_guidance(b))

    def test_mileage_guidance_with_and_without_a_peak_number(self):
        self.assertIn("mi/wk", weekly_mileage_guidance(PEAK, 45))
        self.assertIn("volume", weekly_mileage_guidance(PEAK, None))


class RenderTests(unittest.TestCase):
    def test_render_includes_maintenance_note_only_when_not_primary(self):
        today = date(2026, 9, 15)
        far = render_race_periodization_block(
            "NY Marathon", "2027-11-07", "marathon", today, 45
        )
        self.assertIn("BASE", far)
        self.assertNotIn("MAINTENANCE", far)

        near = render_race_periodization_block(
            "NY Marathon", "2026-09-20", "marathon", today, 45
        )
        self.assertIn("RACE_WEEK".replace("_", "_"), near.upper())
        self.assertIn("MAINTENANCE", near)

    def test_bad_date_returns_empty_string(self):
        self.assertEqual(
            render_race_periodization_block("X", "not-a-date", "marathon", date.today()),
            "",
        )

    def test_days_to_race_helper(self):
        self.assertEqual(
            days_to_race(date(2026, 9, 15), date(2026, 9, 20)), 5
        )


if __name__ == "__main__":
    unittest.main()
