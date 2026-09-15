"""
scripts/set_full_body_plan.py — switch the active training plan from the
default Push/Pull/Legs split to a full-body split.

Why: Dylan's actual PPL execution (per Notion Lifts history, checked
2026-09-15) trained legs in only ~8 sessions total vs 15 push / 14 pull —
leg day was the one that kept losing to running fatigue and schedule. Full
body forces some leg volume into every lifting session instead of relying
on one dedicated day that keeps getting skipped.

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
        "focus": "full body",
        "prescription": (
            "Squat or leg press 3x8, bench or DB press 3x8, barbell/cable row "
            "3x8, RDL or leg curl 2x10, overhead press 2x10, pull-up or lat "
            "pulldown 2x10, core 2-3 sets. ~50-60 min. Keep legs moderate, not "
            "max effort — you're running this week too."
        ),
        "notes": (
            "Full-body day 1 of 3. Rotate which lift is 'main' (heavier, lower "
            "reps) week to week so squat/bench/row/deadlift all get a turn."
        ),
    },
    "tuesday": {
        "session_type": "run",
        "focus": "easy aerobic",
        "prescription": (
            "Easy Z2 run 30-45 min, conversational pace, ~65-75% max HR."
        ),
        "notes": "Recovery from Monday's full-body session — keep it easy.",
    },
    "wednesday": {
        "session_type": "lift",
        "focus": "full body",
        "prescription": (
            "Deadlift or hip thrust 3x6, incline press 3x8, seated cable row "
            "3x8, leg press or split squat 2x10, lateral raise 2x12, curl "
            "2x10, core 2-3 sets. ~50-60 min."
        ),
        "notes": "Full-body day 2 of 3. If legs are still cooked from Monday, drop to 1-2 leg sets.",
    },
    "thursday": {
        "session_type": "run",
        "focus": "quality (tempo or intervals)",
        "prescription": (
            "The one hard run of the week: 4x1mi at threshold, or 20min "
            "steady tempo, or 8x400m at 5k pace. 10min warmup/cooldown."
        ),
        "notes": "If recovery is yellow, shorten it. If red, easy 30min Z2 instead.",
    },
    "friday": {
        "session_type": "lift",
        "focus": "full body",
        "prescription": (
            "Front squat or lunge 3x8, OHP 3x8, pull-up/pulldown 3x8, hamstring "
            "curl 2x10, dip or tricep pushdown 2x10, row 2x10, core 2-3 sets. "
            "~50-60 min."
        ),
        "notes": "Full-body day 3 of 3. Lightest leg volume of the week if Saturday's long run is looming.",
    },
    "saturday": {
        "session_type": "run",
        "focus": "long run",
        "prescription": (
            "Long easy run, Z2 pace, conversational throughout. Build ~10%/wk."
        ),
        "notes": "Volume driver for aerobic base. Keep Friday's leg work light so this isn't compromised.",
    },
    "sunday": {
        "session_type": "rest",
        "focus": "active recovery",
        "prescription": "Full rest or gentle mobility/walk/sauna. No running, no lifting.",
        "notes": "Actual rest is training.",
    },
}

PLAN_NOTES = (
    "Full-body plan (switched from PPL 2026-09). 3 full-body lifts + 3 runs + "
    "1 rest. Reasoning: Notion Lifts history showed legs trained in only ~8 "
    "sessions total vs 15 push / 14 pull under PPL — full body puts some leg "
    "volume in every session instead of relying on one day that kept getting "
    "skipped. Keep per-session leg volume moderate; ai/training_state.py's "
    "readiness engine will flag when a pattern needs lighter volume based on "
    "recent recency, not block the day outright."
)

PLAN_GOAL = (
    "Full-body strength maintenance alongside marathon-focused running — 3 "
    "full-body lifts + 3 runs (easy/quality/long) + 1 rest per week. "
    "Progressive overload on whichever lift is 'main' that day; legs get "
    "trained every session at moderate volume rather than one dedicated day."
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
