"""
Seed the 2026-27 season: goals + training phases.

WHY THIS EXISTS
---------------
Three goals that compete for one recovery budget — a marathon PR, a bench PR
and fat loss — cannot all be primary at once. Run them concurrently and you
get mediocre progress on all three with no way to tell which one is being
starved. This encodes the sequence instead, so the morning brief knows what to
push this month and what to deliberately hold.

Baselines are measured, not guessed (2026-08-27):

  bench          225 x 5  ->  e1RM 262   (2026-07-29)
  trap deadlift  325 x 6  ->  e1RM 390   (2026-07-22)
  5K             22:58                   (2026-06-20)
  10K            46:32                   (2026-06-20)
  run volume     12-16 mi/wk over the last 12 weeks
  longest run    14.2 mi

Riegel off the 5K/10K projects a 3:34-3:40 marathon today, and those are
best efforts inside training runs rather than races, so a raced marathon now
is realistically 3:45-4:00. 3:15 is 7:26/mi, which is the current 5K pace —
that gap closes through the 5K first, which is why "5K sub-21" is a goal here
rather than a nice-to-have.

RACE DATE is assumed to be 2027-11-07, the first Sunday in November and the
traditional NYC Marathon date. NYRR had not published 2027 at the time of
writing. If it moves, edit RACE_DATE below and re-run — this script is
idempotent (phases upsert by name, goals are skipped if the title exists).

    cd ~/fitness-bot && venv/bin/python scripts/seed_season.py
    venv/bin/python scripts/seed_season.py --dry-run
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import obs  # noqa: E402
from config import Config  # noqa: E402
from data.database import Database  # noqa: E402

RACE_DATE = "2027-11-07"

GOALS = [
    dict(
        goal_type="strength",
        title="Bench press e1RM 315 lb",
        target_value=315,
        target_unit="lb",
        baseline_value=262,
        baseline_date="2026-07-29",
        deadline="2027-06-30",
        metadata={
            "note": "+53 lb on e1RM. Must land BEFORE the marathon block; "
            "adding load during a marathon build does not work.",
            "baseline_set": "225 x 5",
        },
    ),
    dict(
        goal_type="strength",
        title="Trap bar deadlift e1RM",
        target_value=None,
        target_unit="lb",
        baseline_value=390,
        baseline_date="2026-07-22",
        deadline="2027-06-30",
        metadata={
            "note": "No target set. Deadlift is well ahead of bench "
            "relatively, so bench is the priority lift.",
            "baseline_set": "325 x 6",
        },
    ),
    dict(
        goal_type="race",
        title="NYC Marathon sub-3:30",
        target_value=210,
        target_unit="min",
        baseline_value=None,
        baseline_date=None,
        deadline=RACE_DATE,
        metadata={
            "goal_pace": "8:00/mi",
            "stretch": "3:15 = 7:26/mi",
            "note": "3:30 is the honest target off current fitness. "
            "3:15 only becomes real if the 5K drops near 19:30.",
        },
    ),
    dict(
        goal_type="running",
        title="5K sub-21:00",
        target_value=21,
        target_unit="min",
        baseline_value=22.97,
        baseline_date="2026-06-20",
        deadline="2027-06-30",
        metadata={
            "note": "The leading indicator for marathon pace. Marathon "
            "time follows 5K speed; chase this first."
        },
    ),
]

PHASES = [
    dict(
        name="Strength Block 2026-27",
        focus="strength",
        start_date="2026-08-28",
        end_date="2027-04-30",
        primary_goal="Bench and trap bar deadlift e1RM. Progress load when the "
        "last session hit all reps cleanly and recovery is green.",
        secondary_mode="Running is aerobic base only. No PR attempts, no volume "
        "jumps, no chasing pace on easy days.",
        run_volume_target="20-30 mi/wk, one quality session, long run <= 10 mi",
        lift_target="4x/wk, bench 2x. ADD A SQUAT PATTERN — currently untrained.",
        nutrition_mode="Slight deficit (~10%) for recomp. Protein high. Never "
        "under-fuel a quality run to hold the deficit.",
        notes="Known gap: only 2 hack-squat sets since May 2026. All leg work "
        "is trap bar deadlift, which leaves the quads short for both the "
        "deadlift goal and late-race marathon fatigue.",
    ),
    dict(
        name="Base Transition 2027",
        focus="base",
        start_date="2027-05-01",
        end_date="2027-06-30",
        primary_goal="Raise running volume from 30 to 40 mi/wk without losing "
        "the strength built over the winter.",
        secondary_mode="Lifting drops to 3x/wk maintenance. Hold e1RM, stop "
        "adding load.",
        run_volume_target="30-40 mi/wk, long run building to 14 mi",
        lift_target="3x/wk, heavy but low volume. Maintenance, not progression.",
        nutrition_mode="Maintenance. The deficit ends here — you cannot build "
        "volume in a deficit.",
        notes="Deliberate handover point. Strength goals are assessed at the "
        "end of April; whatever the bench is on May 1 is what it will be "
        "at the marathon.",
    ),
    dict(
        name="Marathon Block 2027",
        focus="marathon",
        start_date="2027-07-01",
        end_date="2027-10-24",
        primary_goal="Marathon-specific running: 40-50 mi/wk, long runs to 20 "
        "mi, weekly quality at 8:00/mi goal pace.",
        secondary_mode="Lifting 2x/wk, heavy, low volume, maintenance only. "
        "Expect e1RM to plateau or dip slightly — that is "
        "correct, not a regression to fix.",
        run_volume_target="40-50 mi/wk, long run to 20 mi",
        lift_target="2x/wk full body, heavy singles/triples, no volume work",
        nutrition_mode="Maintenance, carbs periodized to the long run. Do NOT "
        "diet during this block.",
        notes="Goal pace 8:00/mi = 3:30. Interference is worst between heavy "
        "legs and hard/long runs — keep them >=24h apart in both "
        "directions.",
    ),
    dict(
        name="Taper + Race 2027",
        focus="taper",
        start_date="2027-10-25",
        end_date=RACE_DATE,
        primary_goal="Freshness. Volume down ~50%, intensity retained.",
        secondary_mode="One light full-body lift per week. Nothing new, nothing "
        "heavy, no novel movements.",
        run_volume_target="~50% of peak, keep some goal-pace work",
        lift_target="1x/wk light full body",
        nutrition_mode="Maintenance, then carb load the final 3 days.",
        notes=f"Race day {RACE_DATE}.",
    ),
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logger = obs.setup_logging("seed-season")
    config = Config()
    db = Database(config.DB_PATH)
    await db.initialize()

    existing = {g["title"] for g in await db.list_goals()}
    for g in GOALS:
        if g["title"] in existing:
            logger.info("goal exists, skipping: %s", g["title"])
            continue
        if args.dry_run:
            logger.info("DRY would create goal: %s", g["title"])
            continue
        gid = await db.create_goal(**g)
        logger.info("created goal #%s: %s", gid, g["title"])

    for p in PHASES:
        if args.dry_run:
            logger.info(
                "DRY would upsert phase: %s (%s -> %s)",
                p["name"],
                p["start_date"],
                p["end_date"],
            )
            continue
        await db.upsert_training_phase(**p)
        logger.info(
            "upserted phase: %s (%s -> %s)", p["name"], p["start_date"], p["end_date"]
        )

    active = await db.get_active_phase()
    nxt = await db.get_next_phase()
    logger.info("ACTIVE PHASE NOW: %s", (active or {}).get("name", "none"))
    logger.info("NEXT PHASE: %s", (nxt or {}).get("name", "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
