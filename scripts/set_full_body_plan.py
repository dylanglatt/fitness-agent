"""
scripts/set_full_body_plan.py — switch the active training plan from the
default Push/Pull/Legs split to a full-body split.

Why: Dylan's actual PPL execution (per Notion Lifts history, checked
2026-09-15) trained legs in only ~8 sessions total vs 15 push / 14 pull —
leg day was the one that kept losing to running fatigue and schedule. Full
body forces some leg volume into every lifting session instead of relying
on one dedicated day that keeps getting skipped.

This template (v2, 2026-09-15) replaced the original generic placeholder
version. It's built from Dylan's actual logged lifts (trap bar deadlift as
the main leg movement, bench/OHP/incline as the push pool, barbell
row/lat pulldown as the pull pool) plus hack squat and sissy squat added
in to close a real gap: almost no quad-dominant work existed under PPL,
just posterior-chain (deadlift + leg curl). Full spec — set/rep ranges,
RIR targets, rest times, superset pairings, exercise-priority tiers for
time-crunched days, and the periodic max-test protocol — discussed and
confirmed with Dylan directly; see PLAN_NOTES below for the condensed
version that actually ships to the DB.

This does NOT touch ai/training_state.py's readiness logic (already updated
separately to treat full-body sessions as volume guidance per pattern rather
than a single spacing-gated pattern) — it only replaces the weekly_template
row that /plan and the morning brief read from.

Usage (run ON THE DROPLET — this mutates the live DB, not a local copy):
    venv/bin/python scripts/set_full_body_plan.py           # apply
    venv/bin/python scripts/set_full_body_plan.py --dry-run # preview only
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from data.database import Database  # noqa: E402

TEMPLATE = {
    "monday": {
        "session_type": "lift",
        "focus": "full body (hinge + horizontal push/pull)",
        "prescription": (
            "Day A — hinge + horizontal push/pull. Trap-bar deadlift 3x4-6 "
            "(main lift, 2.5-4min rest, 1-3 RIR); Barbell bench press 3x5-8 "
            "(main press, 2.5-4min rest, 1-3 RIR); Chest-supported row 3x6-10 "
            "(2-3min rest, 1-2 RIR); Hack squat 2x8-10 (moderate, not "
            "maximal); Lat pulldown 2x8-12, controlled stretch; Cable "
            "lateral raise 2x12-20 superset with curls; Incline DB or cable "
            "curl 2x10-15 superset with laterals; Face pull 2x15-20 "
            "superset with abs; Reverse crunch 2x12-20. ~21 working sets, "
            "60-75 min."
        ),
        "notes": (
            "Deadlift + bench + row are the priorities, never skip those. "
            "Everything after hack squat should move fairly quickly — "
            "supersets (laterals+curls, face pulls+abs) are what keep this "
            "under control. Don't superset the trap-bar deadlift with "
            "anything. If short on time, cut abs/calves/face pulls/one "
            "isolation exercise first, never the main lower lift or main "
            "press/pull. Progression: double progression on the big lifts "
            "(e.g. bench 3x5-8: hit 8,8,8 across all sets before adding "
            "weight); isolation work progresses once you clear the top of "
            "the rep range on every set."
        ),
    },
    "tuesday": {
        "session_type": "run",
        "focus": "easy aerobic",
        "prescription": (
            "Easy Z2 run 30-45 min, conversational pace, ~65-75% max HR."
        ),
        "notes": (
            "Keep this genuinely easy — recovery from Monday's session. "
            "If you're building endurance this is the run to gradually add "
            "mileage to, not speed."
        ),
    },
    "wednesday": {
        "session_type": "lift",
        "focus": "full body (quad + vertical push/pull)",
        "prescription": (
            "Day B — quad + vertical push/pull. This is the hardest leg "
            "day of the week. Hack squat 3x6-8 (main lower lift, 2.5-4min "
            "rest, 1-3 RIR, don't grind these); Plate-loaded shoulder press "
            "3x6-10 (2-3min rest, 1-2 RIR); Lat pulldown 3x6-10 (2-3min "
            "rest, 1-2 RIR); Romanian deadlift 2x8-10 (don't grind these); "
            "Weighted dips 2x6-10, chest/triceps; Leg curl 2x10-15; Hammer "
            "curl 2x10-15 superset with laterals; Cable lateral raise "
            "2x12-20; Calf raise 2x10-15, optional if time; Ab wheel or "
            "plank 2 sets. ~20-22 sets."
        ),
        "notes": (
            "Would not take hack squats or RDLs to failure — 1-3 RIR on "
            "the hack squat, 1-2 RIR on the RDL. Curls/laterals/face-pull/"
            "leg-curl accessories can go harder, 0-1 RIR on the last set. "
            "If this session is wrecking Thursday's run quality, first fix "
            "is cutting RDL to 1-2 sets or backing off hack squat intensity "
            "slightly, not skipping it entirely."
        ),
    },
    "thursday": {
        "session_type": "run",
        "focus": "quality (tempo or intervals)",
        "prescription": (
            "The one hard run of the week: 4x1mi at threshold, or 20min "
            "steady tempo, or 8x400m at 5k pace. 10min warmup/cooldown."
        ),
        "notes": (
            "Placed after Day B on purpose — gym performance stays fresh "
            "and running intensity is controllable, rather than trying to "
            "squat hard on fatigued legs. If recovery is yellow, shorten "
            "it; if red, easy 30min Z2 instead."
        ),
    },
    "friday": {
        "session_type": "lift",
        "focus": "full body (upper hypertrophy + low-fatigue legs)",
        "prescription": (
            "Day C — upper hypertrophy + low-fatigue legs. Deliberately "
            "not another heavy leg day. Incline DB press or plate-loaded "
            "incline press 3x8-12; Seated or chest-supported row 3x8-12; "
            "Sissy squat 2x10-15, controlled; Leg curl 2x10-15; "
            "Plate-loaded shoulder press 2x10-12; Lat pulldown 2x10-12; "
            "Overhead cable triceps extension 2x10-15 superset with curls; "
            "Preacher or cable curl 2x10-15; Rear-delt fly or face pull "
            "2x15-20 superset with abs; Reverse crunch 2x12-20. ~20 sets."
        ),
        "notes": (
            "Hypertrophy stimulus without ruining Sunday's long run — legs "
            "should be much fresher here than on a traditional split since "
            "Saturday is off. Sissy squat and leg curl stay controlled, "
            "not pushed to failure, since Sunday's volume matters more "
            "than this session's."
        ),
    },
    "saturday": {
        "session_type": "rest",
        "focus": "active recovery",
        "prescription": "Full rest or gentle mobility/walk/sauna. No running, no lifting.",
        "notes": "Actual rest is training. Don't sneak another hard workout in here.",
    },
    "sunday": {
        "session_type": "run",
        "focus": "long run",
        "prescription": (
            "Long easy run, Z2 pace, conversational throughout. Build ~10%/wk."
        ),
        "notes": (
            "Volume driver for aerobic base. Friday being upper-focused "
            "plus Saturday's rest is what makes this run possible on "
            "fresh legs."
        ),
    },
}

PLAN_NOTES = (
    "Full-body plan v2 (2026-09). 3 full-body lifts + 3 runs + 1 rest, "
    "~20-22 working sets/session, 60-75 min. Built from Dylan's actual "
    "logged lifts (trap bar deadlift, bench, OHP, barbell row, lat "
    "pulldown) with hack squat + sissy squat added to close a real gap: "
    "under PPL, legs had almost no quad-dominant work, just posterior "
    "chain (deadlift + leg curl). Day A (Mon) = deadlift/bench/row "
    "priority. Day B (Wed) = hardest leg day, hack squat is the main "
    "lower lift, placed before Thursday's quality run so gym performance "
    "stays fresh and running intensity stays controllable. Day C (Fri) = "
    "upper hypertrophy + deliberately low-fatigue legs (sissy squat, "
    "controlled) so Sunday's long run isn't compromised. RIR: 1-3 on "
    "heavy compounds (trap bar/hack squat/bench/shoulder press), 1-2 on "
    "rows/pulldowns/RDL/dips, 0-1 on isolation (curls/laterals/face "
    "pulls/leg curls) — you don't need to fail trap-bar deadlifts to "
    "grow. Rest: 2.5-4min heavy compounds, 2-3min rows/pulldowns/RDL/"
    "dips, 60-90s accessories; supersets (laterals+curls, face pulls+abs, "
    "triceps+biceps) keep sessions under 75min. Time-crunch priority: "
    "never skip main lower lift + main press + main pull; cut abs/"
    "calves/face pulls/one isolation exercise first. Weekly volume "
    "lands roughly: chest ~10 sets, back/lats ~13, quads ~7-8 direct, "
    "hamstrings/glutes ~9, delts ~8-10, biceps 6 direct + pulling, "
    "triceps 4 direct + pressing, abs ~6. "
    "PERIODIC MAX TEST: every 8-12 weeks, if recovery is good, swap "
    "Monday's Day A into a testing day — work up to a heavy top set "
    "(1-3RM) on trap-bar deadlift and bench press instead of the normal "
    "3x4-6/3x5-8, log it as usual. This is a baseline check, not a "
    "regular fixture — skip it if a race is close, recovery is off, or "
    "it's been under 8 weeks since the last one. The e1RM/PR tracking "
    "already in the brief will pick these up automatically."
)

PLAN_GOAL = (
    "Full-body strength (hypertrophy + real progressive overload on the "
    "big three: trap bar deadlift, bench, hack squat) alongside "
    "marathon-focused running — 3 full-body lifts (Mon/Wed/Fri) + 3 runs "
    "(easy/quality/long) + 1 rest per week, ~60-75min sessions. Day B "
    "(Wed) carries the heaviest leg work and sits before the quality run "
    "on purpose; Day C (Fri) is deliberately low-fatigue on legs so the "
    "Sunday long run isn't compromised. Test bench/deadlift maxes every "
    "8-12 weeks to confirm progress rather than guessing from working sets."
)


async def main(dry_run: bool) -> None:
    db = Database(Config.DB_PATH)
    await db.initialize()

    current = await db.get_active_plan()
    if current:
        print(f"Current active plan: {current['name']!r} (id={current['id']})")
    else:
        print("No active plan currently set.")

    if dry_run:
        print("\n--dry-run: would archive the above and activate 'Full body' with:")
        for day, sess in TEMPLATE.items():
            print(f"  {day}: {sess['session_type']} / {sess['focus']}")
        return

    plan_id = await db.save_plan(
        name="Full body",
        goal=PLAN_GOAL,
        weekly_template=TEMPLATE,
        notes=PLAN_NOTES,
        activate=True,
    )
    print(f"\nActivated new plan 'Full body' (id={plan_id}). Old plan archived.")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
