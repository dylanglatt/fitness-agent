"""Plan edits shared by Discord /swap + /move, the app API and the Muse MCP
server (data/plan_ops.py), run against a real temp SQLite DB and the actual
full-body template from scripts/set_full_body_plan.py."""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data import plan_ops
from data.database import Database
from data.plan_ops import PlanEditError
from data.plan_vocab import parse_target
from scripts.set_full_body_plan import TEMPLATE

# Week of Mon 2026-10-12 .. Sun 2026-10-18
MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 10, d) for d in range(12, 19))


def run(coro):
    return asyncio.run(coro)


class ParseTests(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(parse_target("Day B"), ("day", "b"))
        self.assertEqual(parse_target("b"), ("day", "b"))
        self.assertEqual(parse_target("long run"), ("run", "long"))
        self.assertEqual(parse_target("tempo"), ("run", "quality"))
        self.assertEqual(parse_target("run"), ("run", None))
        self.assertEqual(parse_target("off"), ("rest", None))
        self.assertEqual(parse_target("Monday"), ("weekday", "monday"))
        self.assertEqual(parse_target("reset"), ("reset", None))

    def test_library_targets(self):
        self.assertEqual(parse_target("pump"), ("library", "pump"))
        self.assertEqual(parse_target("Upper Pump"), ("library", "pump"))
        self.assertEqual(parse_target("pump short"), ("library", "pump short"))
        self.assertEqual(parse_target("quick pump"), ("library", "pump short"))
        self.assertEqual(
            plan_ops.split_target_and_date("pump short friday"),
            ("pump short", "friday"),
        )

    def test_ppl_targets_are_gone(self):
        # The plan is full body; a push/pull/legs override built a push-only
        # session with no prescription behind it.
        for t in ("push", "pull", "legs", "bench", "squat"):
            self.assertIsNone(parse_target(t), t)

    def test_dates(self):
        self.assertEqual(plan_ops.parse_date("today", WED), WED)
        self.assertEqual(plan_ops.parse_date("tomorrow", WED), THU)
        self.assertEqual(plan_ops.parse_date("monday", WED), MON)
        self.assertEqual(plan_ops.parse_date("Sunday", WED), SUN)
        self.assertEqual(plan_ops.parse_date("2026-10-20", WED), date(2026, 10, 20))
        with self.assertRaises(PlanEditError):
            plan_ops.parse_date("next blursday", WED)

    def test_weekday_means_upcoming_for_writes(self):
        self.assertEqual(
            plan_ops.parse_date("monday", WED, upcoming=True), date(2026, 10, 19)
        )
        self.assertEqual(plan_ops.parse_date("wednesday", WED, upcoming=True), WED)
        self.assertEqual(plan_ops.parse_date("friday", WED, upcoming=True), FRI)

    def test_split_target_and_date(self):
        split = plan_ops.split_target_and_date
        self.assertEqual(split("day b friday"), ("day b", "friday"))
        self.assertEqual(split("long run"), ("long run", "today"))
        self.assertEqual(split("day b"), ("day b", "today"))
        self.assertEqual(split("rest 2026-10-20"), ("rest", "2026-10-20"))
        self.assertEqual(split("reset tomorrow"), ("reset", "tomorrow"))


class ResolveTests(unittest.TestCase):
    def test_lettered_days(self):
        for letter, weekday in (("a", "monday"), ("b", "wednesday"), ("c", "friday")):
            got = plan_ops.resolve_target(TEMPLATE, f"day {letter}", TUE)
            self.assertEqual(got["prescription"], TEMPLATE[weekday]["prescription"])
            self.assertEqual(got["session_type"], "lift")

    def test_lift_means_the_owed_day(self):
        self.assertEqual(
            plan_ops.day_label(plan_ops.resolve_target(TEMPLATE, "lift", MON)), "A"
        )
        self.assertEqual(
            plan_ops.day_label(plan_ops.resolve_target(TEMPLATE, "lift", TUE)), "A"
        )
        self.assertEqual(
            plan_ops.day_label(plan_ops.resolve_target(TEMPLATE, "lift", THU)), "B"
        )
        self.assertEqual(
            plan_ops.day_label(plan_ops.resolve_target(TEMPLATE, "lift", SUN)), "C"
        )

    def test_runs(self):
        self.assertEqual(
            plan_ops.resolve_target(TEMPLATE, "long", WED)["focus"],
            TEMPLATE["sunday"]["focus"],
        )
        self.assertEqual(
            plan_ops.resolve_target(TEMPLATE, "quality", WED)["focus"],
            TEMPLATE["thursday"]["focus"],
        )
        # plain "run" on a lift day falls back to the easy run
        self.assertEqual(
            plan_ops.resolve_target(TEMPLATE, "run", WED)["focus"],
            TEMPLATE["tuesday"]["focus"],
        )

    def test_run_days_have_no_lift_label(self):
        for d in ("tuesday", "thursday", "sunday"):
            self.assertIsNone(plan_ops.day_label(TEMPLATE[d]), d)

    def test_pump_is_a_standalone_session(self):
        from data.session_library import LIBRARY
        for key in LIBRARY:
            got = plan_ops.resolve_target(TEMPLATE, key, THU)
            self.assertEqual(got["session_type"], "lift")
            self.assertEqual(got["prescription"], LIBRARY[key]["prescription"])
            # not a lettered template day
            self.assertIsNone(plan_ops.day_label(got), key)
            self.assertTrue(plan_ops.describe(got).startswith("LIFT — pump"), key)

    def test_unknown(self):
        with self.assertRaises(PlanEditError):
            plan_ops.resolve_target(TEMPLATE, "push", WED)
        with self.assertRaises(PlanEditError):
            plan_ops.resolve_target(TEMPLATE, "day d", WED)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
        run(self.db.initialize())
        run(self.db.save_plan("Full body", "test", TEMPLATE, activate=True))

    def eff(self, d):
        return run(self.db.get_effective_session_for_date(d.isoformat()))

    def test_pump_on_a_run_day_leaves_template_alone(self):
        from data.session_library import LIBRARY
        res = run(plan_ops.set_day(self.db, THU, "pump", source="test"))
        self.assertEqual(res["after"]["type"], "lift")
        self.assertIsNone(res["after"]["day_label"])
        s = self.eff(THU)
        self.assertTrue(s["is_override"])
        self.assertEqual(s["prescription"], LIBRARY["pump"]["prescription"])
        # Friday's Day C and the weekly template are untouched
        self.assertEqual(self.eff(FRI)["prescription"], TEMPLATE["friday"]["prescription"])
        self.assertFalse(self.eff(FRI)["is_override"])
        plan = run(self.db.get_active_plan())
        self.assertEqual(plan["weekly_template"], TEMPLATE)
        # and reset puts Thursday's run back
        run(plan_ops.set_day(self.db, THU, "reset", source="test"))
        self.assertEqual(self.eff(THU)["session_type"], "run")

    def test_set_day_copies_prescription_and_drops_stale_plan(self):
        run(
            self.db.save_planned_session(
                WED.isoformat(), "full_body", [{"name": "Hack squat"}]
            )
        )
        res = run(plan_ops.set_day(self.db, WED, "rest", source="test"))
        self.assertEqual(res["before"]["day_label"], "B")
        self.assertEqual(res["after"]["type"], "rest")
        self.assertIsNone(run(self.db.get_planned_session(WED.isoformat())))

        run(plan_ops.set_day(self.db, TUE, "day b", source="test"))
        s = self.eff(TUE)
        self.assertTrue(s["is_override"])
        self.assertEqual(s["prescription"], TEMPLATE["wednesday"]["prescription"])

    def test_setting_a_day_to_its_template_clears_the_override(self):
        run(plan_ops.set_day(self.db, WED, "rest", source="test"))
        run(plan_ops.set_day(self.db, WED, "day b", source="test"))
        self.assertFalse(self.eff(WED)["is_override"])

    def test_reset(self):
        run(plan_ops.set_day(self.db, FRI, "rest", source="test"))
        run(plan_ops.set_day(self.db, FRI, "reset", source="test"))
        self.assertFalse(self.eff(FRI)["is_override"])
        self.assertEqual(plan_ops.day_label(self.eff(FRI)), "C")

    def test_move_trades_sessions_and_moving_back_restores_template(self):
        run(self.db.save_planned_session(WED.isoformat(), "full_body", [{"name": "x"}]))
        run(plan_ops.move_session(self.db, WED, THU, source="test"))
        self.assertEqual(plan_ops.day_label(self.eff(THU)), "B")
        self.assertEqual(
            self.eff(THU)["prescription"], TEMPLATE["wednesday"]["prescription"]
        )
        self.assertEqual(self.eff(WED)["focus"], TEMPLATE["thursday"]["focus"])
        self.assertIsNone(run(self.db.get_planned_session(WED.isoformat())))

        run(plan_ops.move_session(self.db, THU, WED, source="test"))
        self.assertFalse(self.eff(WED)["is_override"])
        self.assertFalse(self.eff(THU)["is_override"])

    def test_repeated_moves_dont_chain_notes(self):
        run(plan_ops.move_session(self.db, MON, TUE, source="test"))
        run(plan_ops.move_session(self.db, TUE, SAT, source="test"))
        notes = self.eff(SAT)["notes"]
        self.assertEqual(notes.count("[edit]"), 1)
        self.assertTrue(notes.startswith(TEMPLATE["monday"].get("notes", "")[:20]))

    def test_notes_dont_chain_on_sessions_without_notes(self):
        # rest/run overrides start with empty notes; the [edit] marker must
        # still be stripped on the next move.
        run(plan_ops.set_day(self.db, THU, "rest", source="test"))
        run(plan_ops.move_session(self.db, THU, FRI, source="test"))
        run(plan_ops.move_session(self.db, FRI, SUN, source="test"))
        self.assertEqual(self.eff(SUN)["notes"].count("[edit]"), 1)

    def test_run_notes_stay_with_their_weekday(self):
        # Thursday's notes explain its placement after Day B; wrong elsewhere.
        run(plan_ops.move_session(self.db, WED, THU, source="test"))
        self.assertNotIn(
            TEMPLATE["thursday"].get("notes", "")[:30] or "zzz", self.eff(WED)["notes"]
        )
        # lift notes travel with the lift
        self.assertTrue(
            self.eff(THU)["notes"].startswith(TEMPLATE["wednesday"]["notes"][:20])
        )

    def test_rest_on_a_template_rest_day_is_not_an_override(self):
        run(plan_ops.set_day(self.db, SAT, "rest", source="test"))
        self.assertFalse(self.eff(SAT)["is_override"])

    def test_past_dates_refused_when_today_given(self):
        with self.assertRaisesRegex(PlanEditError, "past"):
            run(plan_ops.set_day(self.db, MON, "rest", source="test", today=WED))
        with self.assertRaisesRegex(PlanEditError, "past"):
            run(plan_ops.move_session(self.db, MON, THU, source="test", today=WED))
        run(
            plan_ops.set_day(self.db, WED, "rest", source="test", today=WED)
        )  # today is fine

    def test_move_same_day_rejected(self):
        with self.assertRaises(PlanEditError):
            run(plan_ops.move_session(self.db, WED, WED, source="test"))

    def test_no_plan(self):
        db = Database(os.path.join(tempfile.mkdtemp(), "empty.db"))
        run(db.initialize())
        # initialize() seeds a default plan; archive it
        plan = run(db.get_active_plan())
        if plan:
            import sqlite3

            con = sqlite3.connect(db.db_path)
            con.execute("UPDATE training_plans SET status='archived'")
            con.commit()
            con.close()
        with self.assertRaises(PlanEditError):
            run(plan_ops.set_day(db, WED, "rest", source="test"))


if __name__ == "__main__":
    unittest.main()
