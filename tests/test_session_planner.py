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
    load_increment,
    next_load,
    plan_session,
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
        self.assertEqual(
            next_load(s, 6, "Bench press", intensity_band="yellow")[1:],
            next_load(s, 6, "Bench press", intensity_band="yellow")[1:],
        )
        w, _r, action = next_load(s, 6, "Bench press", intensity_band="yellow")
        self.assertEqual((w, action), (205, "repeat"))
        w, _r, action = next_load(s, 6, "Bench press", intensity_band="red")
        self.assertEqual(action, "deload")


LIBRARY = [
    {
        "exercise": "Overhead press",
        "n_sets": 46,
        "n_sessions": 15,
        "last_date": "2026-08-25",
    },
    {
        "exercise": "Bench press",
        "n_sets": 54,
        "n_sessions": 18,
        "last_date": "2026-08-25",
    },
    {
        "exercise": "Cable lateral raise",
        "n_sets": 4,
        "n_sessions": 2,
        "last_date": "2026-08-25",
    },
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
        self.assertGreater(
            sum(e["sets"] for e in primary), sum(e["sets"] for e in maint)
        )
        self.assertTrue(all(e["sets"] == 2 for e in maint))

    def test_load_comes_from_history_not_a_guess(self):
        """Double progression: 6 clean reps in a 6-8 range earns a REP, not a
        load jump. The weight moves when the top of the range is cleared."""
        hist = {"bench press": [_sess("2026-08-20", (205, 6), (205, 6), (205, 6))]}
        plan = plan_session("push", LIBRARY, hist, goal_lifts=["bench press"])
        bench = plan[0]
        self.assertEqual(bench["target_weight_lb"], 205)
        self.assertEqual(bench["reps"], "7")
        self.assertEqual(bench["action"], "add_rep")
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

        ohp = [
            _sess("2026-08-07", (105, 8), (105, 8)),
            _sess("2026-08-14", (110, 8), (110, 8)),
        ]
        self.assertEqual(infer_target_reps(ohp, 6), 8)

    def test_falls_back_to_the_default_without_history(self):
        from ai.session_planner import infer_target_reps

        self.assertEqual(infer_target_reps([], 6), 6)
        self.assertEqual(infer_target_reps([_sess("2026-08-14", (None, None))], 10), 10)

    def test_planner_uses_the_inferred_target(self):
        hist = {
            "overhead press": [
                _sess("2026-08-07", (105, 8), (105, 8)),
                _sess("2026-08-14", (110, 8), (110, 6)),
            ]
        }
        plan = {e["name"]: e for e in plan_session("push", LIBRARY, hist)}
        ohp = plan["Overhead press"]
        self.assertEqual(ohp["reps"], "8")
        # 6 of 8 on the top set is a MISS -> repeat, not progress.
        self.assertEqual(ohp["action"], "repeat")
        self.assertEqual(ohp["target_weight_lb"], 110)


class SessionShapeTests(unittest.TestCase):
    """Dylan's actual push day: bench 1x10 @135 then 3x6 @205, OHP 3x10 @95,
    incline DB press 3x8 @65, single-arm cable lateral raise 3x12 each side,
    single-arm tricep pushdown 3x12 @20."""

    def test_warmup_matches_how_he_actually_warms_up(self):
        from ai.session_planner import warmup_for

        wu = warmup_for(205, "bench press")
        self.assertEqual(wu, {"weight_lb": 135, "reps": 10})

    def test_no_warmup_for_isolation_or_missing_load(self):
        from ai.session_planner import warmup_for

        self.assertIsNone(warmup_for(20, "single arm tricep pushdown"))
        self.assertIsNone(warmup_for(None, "bench press"))

    def test_unilateral_movements_are_flagged(self):
        from ai.session_planner import is_unilateral

        self.assertTrue(is_unilateral("Single arm cable lateral raise"))
        self.assertTrue(is_unilateral("Single-leg RDL"))
        self.assertFalse(is_unilateral("Bench press"))

    def test_set_count_follows_history(self):
        from ai.session_planner import infer_target_sets

        three = [_sess("2026-08-14", (205, 6), (205, 6), (205, 6))]
        self.assertEqual(infer_target_sets(three, 4), 3)
        self.assertEqual(infer_target_sets([], 4), 4)

    def test_full_push_day_reproduces_his_shape(self):
        lib = [
            {
                "exercise": "Bench press",
                "n_sets": 54,
                "n_sessions": 18,
                "last_date": "2026-08-25",
            },
            {
                "exercise": "Overhead press",
                "n_sets": 46,
                "n_sessions": 15,
                "last_date": "2026-08-25",
            },
            {
                "exercise": "Incline dumbbell press",
                "n_sets": 13,
                "n_sessions": 5,
                "last_date": "2026-08-25",
            },
            {
                "exercise": "Single arm cable lateral raise",
                "n_sets": 9,
                "n_sessions": 3,
                "last_date": "2026-08-25",
            },
            {
                "exercise": "Single arm tricep pushdown",
                "n_sets": 9,
                "n_sessions": 3,
                "last_date": "2026-08-25",
            },
        ]
        hist = {
            "bench press": [
                _sess("2026-08-18", (135, 10), (205, 6), (205, 6), (205, 6))
            ],
            "overhead press": [_sess("2026-08-18", (95, 10), (95, 10), (95, 10))],
            "incline dumbbell press": [_sess("2026-08-18", (65, 8), (65, 8), (65, 8))],
            "single arm cable lateral raise": [
                _sess("2026-08-18", (10, 12), (10, 12), (10, 12))
            ],
            "single arm tricep pushdown": [
                _sess("2026-08-18", (20, 12), (20, 12), (20, 12))
            ],
        }
        plan = {
            e["name"]: e
            for e in plan_session("push", lib, hist, goal_lifts=["bench press"])
        }
        bench = plan["Bench press"]
        # 6 clean reps sits at the BOTTOM of the 6-8 range: same weight, one
        # more rep. Linear progression would have said 210 and stalled.
        self.assertEqual((bench["sets"], bench["reps"]), (3, "7"))
        self.assertEqual(bench["target_weight_lb"], 205)
        self.assertEqual(bench["rep_range"], "6-8")
        self.assertEqual(bench["warmup"], {"weight_lb": 135, "reps": 10})

        ohp = plan["Overhead press"]
        self.assertEqual((ohp["sets"], ohp["reps"]), (3, "11"))
        self.assertEqual(ohp["target_weight_lb"], 95)

        inc = plan["Incline dumbbell press"]
        self.assertEqual((inc["sets"], inc["reps"]), (3, "9"))  # 8-10 range
        # Only the opening lift gets a warmup set — a "45 lb bar" warmup on a
        # dumbbell press is nonsense, and it is not how he trains.
        self.assertIsNone(inc["warmup"])
        self.assertIsNone(plan["Overhead press"]["warmup"])

        lat = plan["Single arm cable lateral raise"]
        self.assertTrue(lat["per_side"])
        self.assertEqual(lat["reps"], "13")  # 12-14 range, clean at 12 -> +1 rep
        self.assertIsNone(lat["warmup"])


class DoubleProgressionTests(unittest.TestCase):
    """The ladder a real coach runs: reps climb inside the range, then the
    weight climbs and the reps reset. Adding load every session is novice
    programming — it stalls, and the old rule then repeated the same weight
    forever."""

    def _step(self, sessions):
        from ai.session_planner import next_prescription

        return next_prescription(sessions, "Bench press", rep_range=(6, 8))

    def test_the_full_ladder(self):
        s = [_sess("2026-08-01", (205, 6), (205, 6), (205, 6))]
        p = self._step(s)
        self.assertEqual(
            (p["weight_lb"], p["target_reps"], p["action"]), (205, 7, "add_rep")
        )

        s.append(_sess("2026-08-08", (205, 7), (205, 7), (205, 7)))
        p = self._step(s)
        self.assertEqual(
            (p["weight_lb"], p["target_reps"], p["action"]), (205, 8, "add_rep")
        )

        s.append(_sess("2026-08-15", (205, 8), (205, 8), (205, 8)))
        p = self._step(s)
        self.assertEqual(
            (p["weight_lb"], p["target_reps"], p["action"]), (210, 6, "add_weight")
        )
        self.assertIn("Cleared the top of the range", p["reason"])

    def test_a_ragged_set_holds_the_weight(self):
        """8, 8, 6 is not clearing the range — the worst set decides."""
        s = [_sess("2026-08-15", (205, 8), (205, 8), (205, 6))]
        p = self._step(s)
        self.assertEqual(p["action"], "add_rep")
        self.assertEqual(p["weight_lb"], 205)

    def test_missing_the_bottom_repeats_then_deloads(self):
        s = [_sess("2026-08-08", (215, 5), (215, 4), (215, 4))]
        self.assertEqual(self._step(s)["action"], "repeat")
        s.append(_sess("2026-08-15", (215, 5), (215, 4), (215, 4)))
        p = self._step(s)
        self.assertEqual(p["action"], "deload")
        self.assertEqual(p["weight_lb"], 195)

    def test_three_flat_sessions_at_the_same_weight_is_a_stall(self):
        """Not a failure — just no movement. A coach backs off and rebuilds
        rather than letting it sit there for months."""
        s = [
            _sess(f"2026-08-{d:02d}", (205, 7), (205, 7), (205, 7)) for d in (1, 8, 15)
        ]
        p = self._step(s)
        self.assertEqual(p["action"], "deload")
        self.assertIn("without moving", p["reason"])

    def test_rep_range_starts_from_how_he_actually_trains(self):
        from ai.session_planner import rep_range_for

        self.assertEqual(
            rep_range_for([_sess("2026-08-15", (205, 6), (205, 6))], 8), (6, 8)
        )
        self.assertEqual(
            rep_range_for([_sess("2026-08-15", (95, 10), (95, 10))], 6), (10, 12)
        )
        self.assertEqual(rep_range_for([], 6), (6, 8))


class ProgressionPaceTests(unittest.TestCase):
    """A per-set rule can run for a year and quietly miss the goal by 40 lb.
    Nothing in the system would ever have said so."""

    def _ramp(self, start_e1rm_weight, weeks, gain_per_week):
        out = []
        for i in range(weeks):
            w = start_e1rm_weight + gain_per_week * i
            out.append(
                {
                    "date": f"2026-{1 + i // 4:02d}-{1 + (i % 4) * 7:02d}",
                    "sets": [{"weight_lb": round(w), "reps": 6}],
                }
            )
        return out

    def test_behind_pace_is_named_with_the_numbers(self):
        from ai.session_planner import progression_pace

        # ~1 lb/week on the bar, needing far more to reach 315 e1RM.
        pace = progression_pace(
            self._ramp(200, 12, 1.0), 315, "2027-06-30", "2026-08-28"
        )
        self.assertIsNotNone(pace)
        self.assertFalse(pace["on_pace"])
        self.assertIn("BEHIND pace", pace["note"])
        self.assertIn("Needs", pace["note"])

    def test_on_pace_says_so(self):
        from ai.session_planner import progression_pace

        pace = progression_pace(
            self._ramp(200, 12, 3.0), 260, "2027-06-30", "2026-08-28"
        )
        self.assertTrue(pace["on_pace"])
        self.assertIn("On pace", pace["note"])

    def test_flat_progress_is_called_out(self):
        from ai.session_planner import progression_pace

        pace = progression_pace(
            self._ramp(200, 12, 0.0), 315, "2027-06-30", "2026-08-28"
        )
        self.assertIn("flat or falling", pace["note"])

    def test_not_enough_history_returns_none(self):
        from ai.session_planner import progression_pace

        self.assertIsNone(
            progression_pace(
                [_sess("2026-08-01", (205, 6))], 315, "2027-06-30", "2026-08-28"
            )
        )
