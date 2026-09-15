"""Tests for ai/exercise_db.py — the free-exercise-db lookup/substitution
layer. Uses the real vendored dataset (data/exercise_db.json) rather than a
fixture, since the point is to catch drift against what's actually shipped."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai import exercise_db as edb  # noqa: E402


class LoadTests(unittest.TestCase):
    def test_dataset_loads_and_is_nonempty(self):
        self.assertGreater(len(edb._load()), 500)


class MuscleMappingTests(unittest.TestCase):
    def test_known_muscles_map_to_a_pattern(self):
        self.assertEqual(edb.muscle_to_pattern("chest"), "push")
        self.assertEqual(edb.muscle_to_pattern("Quadriceps"), "legs")
        self.assertEqual(edb.muscle_to_pattern("lats"), "pull")
        self.assertEqual(edb.muscle_to_pattern("abdominals"), "core")

    def test_unknown_or_empty_muscle_returns_none(self):
        self.assertIsNone(edb.muscle_to_pattern("neck"))
        self.assertIsNone(edb.muscle_to_pattern(""))
        self.assertIsNone(edb.muscle_to_pattern(None))


class FindExerciseTests(unittest.TestCase):
    def test_exact_match_case_insensitive(self):
        ex = edb.find_exercise("barbell squat")
        self.assertIsNotNone(ex)
        self.assertEqual(ex["name"].lower(), "barbell squat")

    def test_substring_match_either_direction(self):
        # a logged name that's a superset of a dataset name
        ex = edb.find_exercise("bb barbell squat heavy")
        self.assertIsNotNone(ex)
        self.assertIn("squat", ex["name"].lower())

    def test_short_name_does_not_wildly_substring_match(self):
        self.assertIsNone(edb.find_exercise("ab"))

    def test_blank_or_unknown_name_returns_none(self):
        self.assertIsNone(edb.find_exercise(""))
        self.assertIsNone(edb.find_exercise("zzz_not_a_real_exercise_zzz"))


class ClassifyViaDbTests(unittest.TestCase):
    def test_classifies_a_dataset_exercise(self):
        pattern, muscle = edb.classify_via_db("barbell squat")
        self.assertEqual(pattern, "legs")
        self.assertTrue(muscle)

    def test_unknown_exercise_returns_none_none(self):
        self.assertEqual(edb.classify_via_db("not a real exercise"), (None, None))


class SubstitutesTests(unittest.TestCase):
    def test_substitutes_share_a_primary_muscle_and_exclude_self(self):
        subs = edb.find_substitutes("barbell squat", limit=5)
        self.assertTrue(subs)
        ex = edb.find_exercise("barbell squat")
        primary = set(ex["primaryMuscles"])
        for s in subs:
            self.assertTrue(set(s["primaryMuscles"]) & primary)
            self.assertNotEqual(s["name"].lower(), "barbell squat")

    def test_equipment_filter_is_respected(self):
        subs = edb.find_substitutes("barbell squat", equipment=["dumbbell"], limit=10)
        self.assertTrue(subs)
        for s in subs:
            self.assertEqual((s["equipment"] or "").lower(), "dumbbell")

    def test_unknown_exercise_returns_empty_list(self):
        self.assertEqual(edb.find_substitutes("not a real exercise"), [])

    def test_limit_is_respected(self):
        subs = edb.find_substitutes("barbell squat", limit=2)
        self.assertLessEqual(len(subs), 2)


if __name__ == "__main__":
    unittest.main()
