"""Tests for recovery prescription.

The failure: the brief never told Dylan to DO recovery work. The prompt said
"only suggest adding recovery work if the block is genuinely empty", so a user
with 91 sauna sessions in nine months got acknowledgement and never direction.

Protocol constants trace to knowledge/04_hrv_recovery.md §6 and §7, which the
RAG retriever has never surfaced because its dependencies are missing.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai.recovery_planner import (  # noqa: E402
    COLD,
    SAUNA,
    plan_recovery,
    render_recovery_prescription,
)

ACCESS = {SAUNA: "easy", COLD: "trip", "steam": "trip"}


def _mods(plan):
    return [r["modality"] for r in plan["recommendations"]]


class RecoveryPlannerTests(unittest.TestCase):
    def test_it_prescribes_rather_than_acknowledges(self):
        plan = plan_recovery(
            session_kind="lift",
            session_sub="push",
            band="green",
            phase_focus="strength",
            recent={SAUNA: 1},
            access=ACCESS,
        )
        self.assertIn(SAUNA, _mods(plan))
        block = render_recovery_prescription(plan)
        self.assertIn("15-20 min", block)
        self.assertIn("after the lift", block)

    def test_post_lift_cold_plunge_is_blocked_in_a_strength_phase(self):
        """Cold within hours of lifting blunts the adaptation he is training
        for — this is the rule that matters most given his bench goal."""
        plan = plan_recovery(
            session_kind="lift",
            session_sub="push",
            band="green",
            phase_focus="strength",
            recent={},
            access=ACCESS,
        )
        self.assertNotIn(COLD, _mods(plan))
        self.assertTrue(any("NO cold plunge" in w for w in plan["warnings"]))

    def test_cold_after_a_hard_run_is_fine(self):
        """The blunting concern is strength-specific, not endurance."""
        plan = plan_recovery(
            session_kind="run",
            session_sub="long",
            band="green",
            phase_focus="marathon",
            recent={},
            access=ACCESS,
        )
        self.assertIn(COLD, _mods(plan))
        self.assertEqual(plan["warnings"], [])

    def test_sauna_goes_after_a_quality_run_never_before(self):
        plan = plan_recovery(
            session_kind="run",
            session_sub="quality",
            band="green",
            recent={},
            access=ACCESS,
        )
        sauna = next(r for r in plan["recommendations"] if r["modality"] == SAUNA)
        self.assertIn("AFTER", sauna["when"])
        self.assertIn("dehydrated", sauna["reason"])

    def test_red_recovery_contraindicates_heat(self):
        plan = plan_recovery(session_kind="rest", band="red", access=ACCESS)
        self.assertNotIn(SAUNA, _mods(plan))
        self.assertTrue(any("skip the sauna" in w for w in plan["warnings"]))
        self.assertIn("breathing", _mods(plan))

    def test_frequency_gap_is_named_with_numbers(self):
        low = plan_recovery(
            session_kind="rest", band="green", recent={SAUNA: 1}, access=ACCESS
        )
        self.assertIn("1 sauna session", low["recommendations"][0]["reason"])
        at_target = plan_recovery(
            session_kind="rest", band="green", recent={SAUNA: 4}, access=ACCESS
        )
        self.assertIn("already at the", at_target["recommendations"][0]["reason"])

    def test_trip_only_modalities_say_whether_the_trip_is_worth_it(self):
        plan = plan_recovery(
            session_kind="run",
            session_sub="long",
            band="green",
            recent={},
            access=ACCESS,
        )
        cold = next(r for r in plan["recommendations"] if r["modality"] == COLD)
        self.assertIn("worth the trip", cold["reason"].lower())

    def test_rest_day_is_the_highest_value_sauna_slot(self):
        plan = plan_recovery(
            session_kind="rest", band="green", recent={}, access=ACCESS
        )
        sauna = next(r for r in plan["recommendations"] if r["modality"] == SAUNA)
        self.assertIn("rest day", sauna["when"])

    def test_no_access_no_recommendation(self):
        plan = plan_recovery(session_kind="rest", band="green", access={})
        self.assertEqual(plan["recommendations"], [])
        self.assertEqual(render_recovery_prescription(plan), "")


if __name__ == "__main__":
    unittest.main()
