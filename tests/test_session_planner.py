"""Tests for deterministic session planning.

The three reported failures these lock down:
  "It constantly guesses what I should lift."     -> plan from the library
  "I have to tell it what to do next."            -> real progression rule
  "It tells me to end my workouts early."         -> covered in coach tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai.session_planner import (  # noqa: E402
    load_increment, next_load, plan_session,
)


def _sess(date, *sets):
    return {"date": date, "sets": [{"weight_lb": w, "reps": r} for w, r in sets]}


class NextLoadTests(unittest.TestCase):
    def test_no_history_asks_for_a_working_weight(self):
        w, reason, action = next_load([], 6, "Bench press")
        self.assertIsNone(w)
        self.assertEqual(action, "pick")

    def test_hitting_all_reps_progresses(self):
        s = [_sess("2026-08-20", (205, 6), (205, 6), (205, 6))]
        w, reason, action = next_load(s, 6, "Bench press")
        self.assertEqual(w, 210)
        self.assertEqual(action, "progress")
        self.assertIn("205", reason)

    def test_missing_reps_repeats_instead_of_adding_weight(self):
        """The old code added +5 unconditionally, walking into a failed set
        and then telling him to add weight again."""
        s = [_sess("2026-08-20", (205, 6), (205, 5), (205, 4))]
        w, reason, action = next_load(s, 6, "Bench press")
        self.assertEqual(w, 205)
        self.assertEqual(action, "repeat")

    def test_stalling_twice_deloads(self):
        s = [
            _sess("2026-08-13", (205, 5), (205, 4)),
            _sess("2026-08-20", (205, 5), (205, 4)),
        ]
        w, reason, action = next_load(s, 6, "Bench press")
        self.assertEqual(action, "deload")
        self.assertLess(w, 205)
        self.assertIn("stalled twice", reason)

    def test_warmups_do_not_decide_progression(self):
        """Working sets are those at the session's heaviest weight; a light
        warmup at full reps must not count as hitting the top load."""
        s = [_sess("2026-08-20", (135, 10), (225, 4))]
        w, reason, action = next_load(s, 6, "Bench press")
        self.assertEqual(w, 225)
        self.assertEqual(action, "repeat")

    def test_increments_scale_with_the_lift(self):
        self.assertEqual(load_increment("Trap bar deadlift"), 10)
        self.assertEqual(load_increment("Bench press"), 5)
        self.assertEqual(load_increment("Cable lateral raise"), 2.5)
        s = [_sess("2026-08-20", (325, 6))]
        self.assertEqual(next_load(s, 6, "Trap bar deadlift")[0], 335)

    def test_recovery_band_overrides_progression(self):
        s = [_sess("2026-08-20", (205, 6), (205, 6))]
        self.assertEqual(next_load(s, 6, "Bench press", intensity_band="yellow")[1:],
                         next_load(s, 6, "Bench press", intensity_band="yellow")[1:])
        w, _r, action = next_load(s, 6, "Bench press", intensity_band="yellow")
        self.assertEqual((w, action), (205, "repeat"))
        w, _r, action = next_load(s, 6, "Bench press", intensity_band="red")
        self.assertEqual(action, "deload")


LIBRARY = [
    {"exercise": "Overhead press", "n_sets": 46, "n_sessions": 15, "last_date": "2026-08-25"},
    {"exercise": "Bench press", "n_sets": 54, "n_sessions": 18, "last_date": "2026-08-25"},
    {"exercise": "Cable lateral raise", "n_sets": 4, "n_sessions": 2, "last_date": "2026-08-25"},
    {"exercise": "Dips", "n_sets": 7, "n_sessions": 3, "last_date": "2026-08-12"},
]


class PlanSessionTests(unittest.TestCase):
    def test_empty_library_returns_nothing_to_invent_from(self):
        self.assertEqual(plan_session("legs", [], {}), [])

    def test_goal_lift_leads_even_when_not_most_trained(self):
        """Overhead press has more sets than bench, but bench is the goal."""
        plan = plan_session("push", LIBRARY, {}, goal_lifts=["bench press"])
        self.assertEqual(plan[0]["name"], "Bench press")
        self.assertEqual(plan[0]["notes"], "GOAL LIFT")
        self.assertEqual(plan[0]["sets"], 4)

    def test_compounds_precede_isolation(self):
        names = [e["name"] for e in plan_session("push", LIBRARY, {})]
        self.assertLess(names.index("Bench press"), names.index("Cable lateral raise"))

    def test_rep_targets_by_movement_class(self):
        plan = {e["name"]: e for e in plan_session("push", LIBRARY, {})}
        self.assertEqual(plan["Bench press"]["reps"], "6")
        self.assertEqual(plan["Cable lateral raise"]["reps"], "12")

    def test_maintenance_phase_cuts_volume(self):
        primary = plan_session("push", LIBRARY, {}, is_primary_phase=True)
        maint = plan_session("push", LIBRARY, {}, is_primary_phase=False)
        self.assertGreater(sum(e["sets"] for e in primary),
                           sum(e["sets"] for e in maint))
        self.assertTrue(all(e["sets"] == 2 for e in maint))

    def test_load_comes_from_history_not_a_guess(self):
        hist = {"bench press": [_sess("2026-08-20", (205, 6), (205, 6), (205, 6))]}
        plan = plan_session("push", LIBRARY, hist, goal_lifts=["bench press"])
        bench = plan[0]
        self.assertEqual(bench["target_weight_lb"], 210)
        self.assertEqual(bench["action"], "progress")
        # A lift with no history is honest about it rather than inventing a load.
        dips = next(e for e in plan if e["name"] == "Dips")
        self.assertIsNone(dips["target_weight_lb"])
        self.assertEqual(dips["action"], "pick")

    def test_red_day_trims_sets(self):
        normal = plan_session("push", LIBRARY, {})
        red = plan_session("push", LIBRARY, {}, intensity_band="red")
        self.assertLess(sum(e["sets"] for e in red), sum(e["sets"] for e in normal))


if __name__ == "__main__":
    unittest.main()


class RepInferenceTests(unittest.TestCase):
    """A plan that silently changes your rep ranges is not history-driven —
    and 8 reps scored against a target of 6 reads as a clean session when it
    was a missed one."""

    def test_reps_follow_history_not_the_movement_class(self):
        from ai.session_planner import infer_target_reps
        ohp = [_sess("2026-08-07", (105, 8), (105, 8)),
               _sess("2026-08-14", (110, 8), (110, 8))]
        self.assertEqual(infer_target_reps(ohp, 6), 8)

    def test_falls_back_to_the_default_without_history(self):
        from ai.session_planner import infer_target_reps
        self.assertEqual(infer_target_reps([], 6), 6)
        self.assertEqual(infer_target_reps([_sess("2026-08-14", (None, None))], 10), 10)

    def test_planner_uses_the_inferred_target(self):
        hist = {"overhead press": [
            _sess("2026-08-07", (105, 8), (105, 8)),
            _sess("2026-08-14", (110, 8), (110, 6)),
        ]}
        plan = {e["name"]: e for e in plan_session("push", LIBRARY, hist)}
        ohp = plan["Overhead press"]
        self.assertEqual(ohp["reps"], "8")
        # 6 of 8 on the top set is a MISS -> repeat, not progress.
        self.assertEqual(ohp["action"], "repeat")
        self.assertEqual(ohp["target_weight_lb"], 110)
