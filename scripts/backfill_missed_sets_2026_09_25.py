"""One-off: correct Friday 2026-09-25's lift session data.

This REPLACES the earlier version of this script (which assumed shoulder
press was simply missing). Looking at the actual droplet rows, the real
picture is messier:

  1. Several exercises are missing their 2nd/3rd sets entirely (incline
     press, sissy squat, chest-supported row, lat pulldown, tricep
     extension). Root cause: bare continuation replies like "Next set,
     same thing" have no digits or lift keywords in them, so the pre-filter
     regex (_LIFT_HINT) rejected them before the parser ever ran -- they
     never reached the database in any form. Fixed going forward in
     ai/coach.py (_LIFT_HINT now matches continuation phrases, and the
     parser inherits weight/reps for them, not just the exercise name).

  2. Plate-loaded shoulder press's two sets were never dropped -- they got
     silently MISATTRIBUTED to "Single leg lying curl" (sets 2 and 3),
     because the chat replies naming shoulder press didn't repeat the
     exercise name and the parser only had the last DB row for context.
     Same root cause diagnosed for the "3 missing lifts" complaint, fixed
     going forward via the last-assistant-message context added to the
     parser.

  3. Face pull's 2nd set and preacher curl's 2nd set got misattributed to
     "Overhead cable tricep extension" (as its sets 2 and 3) for the same
     reason as #2.

This script:
  - UPDATEs the 4 misattributed lift_sets rows (and their parent lifts
    rows) back to the correct exercise/set_number, instead of leaving
    wrong data under the wrong exercise AND separately inserting new rows
    (which would double-count).
  - INSERTs the sets that were genuinely never captured anywhere (#1 and
    the real "single leg lying curl" set 2 / "tricep extension" set 2 that
    got displaced by the misattribution in #2/#3).

One assumption worth flagging: shoulder press was described as "115 lbs on
each side" for set 1. This script logs weight_lb as the TOTAL (230, i.e.
2x115), matching how chest-supported row's "90 lbs on each side" was
recorded as 180 total elsewhere in this same session. Set 2 ("115 x 7,
failure") is assumed to be the same per-side load stated in shorthand
(also 230 total) -- the coach's own reply at the time ("same weight,
dialed in") supports this. If Dylan actually meant 115 lb TOTAL rather
than per-side, change SHOULDER_PRESS_TOTAL_LB below to 115 before running.

Numbers taken directly from the Discord chat transcript, cross-checked
against the droplet's actual lift_sets rows (ids noted per correction).

Run once on the droplet:
  venv/bin/python scripts/backfill_missed_sets_2026_09_25.py --check
  venv/bin/python scripts/backfill_missed_sets_2026_09_25.py --dry-run
  venv/bin/python scripts/backfill_missed_sets_2026_09_25.py
"""

import argparse
import asyncio
import sys
from pathlib import Path

import aiosqlite

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from data.database import Database  # noqa: E402
from integrations.notion import NotionClient  # noqa: E402

DATE = "2026-09-25"  # Friday

# Set this to 115 instead if Dylan confirms shoulder press was 115 lb TOTAL,
# not 115 lb per side (230 total). See the module docstring.
SHOULDER_PRESS_TOTAL_LB = 230.0

# ── Corrections: fix the exercise/set_number on rows that are already in
# the DB with the right numbers but the wrong label. `lift_sets_id` and
# `lift_id` are the exact ids from the droplet's actual data -- don't
# reuse this script for a different date/session without re-deriving them.
CORRECTIONS = [
    {
        "lift_sets_id": 507,
        "lift_id": 474,
        "old_exercise": "Single leg lying curl",
        "new_exercise": "Plate-loaded shoulder press",
        "set_number": 1,
        "reps": 10,
        "weight_lb": SHOULDER_PRESS_TOTAL_LB,
        "to_failure": 0,
        "notes": "",
    },
    {
        "lift_sets_id": 508,
        "lift_id": 475,
        "old_exercise": "Single leg lying curl",
        "new_exercise": "Plate-loaded shoulder press",
        "set_number": 2,
        "reps": 7,
        "weight_lb": SHOULDER_PRESS_TOTAL_LB,
        "to_failure": 1,
        "notes": "to failure",
    },
    {
        "lift_sets_id": 512,
        "lift_id": 479,
        "old_exercise": "Overhead cable tricep extension",
        "new_exercise": "Face pull",
        "set_number": 2,
        "reps": 12,
        "weight_lb": 42.5,
        "to_failure": 0,
        "notes": "",
    },
    {
        "lift_sets_id": 514,
        "lift_id": 481,
        "old_exercise": "Overhead cable tricep extension",
        "new_exercise": "Preacher curl",
        "set_number": 2,
        "reps": 12,
        "weight_lb": 50.0,
        "to_failure": 0,
        "notes": "",
    },
]

# ── Additions: sets that never made it into the DB in any form. Each
# becomes its own `lifts` parent row (matching the rest of this codebase's
# convention of one lifts row per logged message) plus one lift_sets row.
ADDITIONS = [
    {"exercise": "Incline dumbbell press", "set_number": 2, "reps": 11, "weight_lb": 70.0},
    {"exercise": "Incline dumbbell press", "set_number": 3, "reps": 11, "weight_lb": 70.0},
    {"exercise": "Sissy squat", "set_number": 2, "reps": 11, "weight_lb": 35.0},
    {"exercise": "Chest supported row", "set_number": 2, "reps": 8, "weight_lb": 180.0},
    {"exercise": "Chest supported row", "set_number": 3, "reps": 8, "weight_lb": 180.0},
    {"exercise": "Single leg lying curl", "set_number": 2, "reps": 10, "weight_lb": 45.0},
    {"exercise": "Lat pulldown", "set_number": 2, "reps": 10, "weight_lb": 130.0},
    {"exercise": "Overhead cable tricep extension", "set_number": 2, "reps": 10, "weight_lb": 42.5},
]

# Renumber this one existing row: it was logged as "set 1" (colliding with
# the warmup set, which is also numbered 1) when it was really the first
# WORKING set, i.e. set 2 overall (warmup=1, then three working sets).
RENUMBER = [
    {"lift_sets_id": 503, "lift_id": 470, "exercise": "Incline dumbbell press", "new_set_number": 2},
]

RAW = (
    "Manual correction (2026-09-25 session, applied 2026-09-25): fixed "
    "sets misattributed to the wrong exercise by the chat lift parser, and "
    "added sets that were silently dropped because bare 'same thing' "
    "replies never reached the logger. See scripts/backfill_missed_sets_"
    "2026_09_25.py for the full diagnosis."
)


async def check_existing(db: Database) -> None:
    rows = await db.get_lift_sets_for_date(DATE)
    print(f"lift_sets rows currently on {DATE}: {len(rows)}")
    for r in rows:
        print(f"  id={r['id']:<4} lift_id={r['lift_id']:<4} "
              f"{r.get('exercise')!r:38} set {r.get('set_number')}: "
              f"{r.get('weight_lb')} lb x {r.get('reps')}"
              + (" [TO FAILURE]" if r.get("to_failure") else ""))

    ids_seen = {r["id"] for r in rows}
    for c in CORRECTIONS:
        if c["lift_sets_id"] not in ids_seen:
            print(f"\nWARNING: expected lift_sets id={c['lift_sets_id']} "
                  f"(for the {c['old_exercise']!r} -> {c['new_exercise']!r} "
                  f"correction) but it's not in today's rows. Has this "
                  f"script already been run, or did the ids change? Re-run "
                  f"the raw dump before proceeding.")
    for r in RENUMBER:
        if r["lift_sets_id"] not in ids_seen:
            print(f"\nWARNING: expected lift_sets id={r['lift_sets_id']} "
                  f"for the renumber step but it's not present.")


async def apply_corrections(db: Database, dry_run: bool) -> None:
    async with aiosqlite.connect(db.db_path) as conn:
        for c in CORRECTIONS:
            print(f"[correct] lift_sets id={c['lift_sets_id']}: "
                  f"{c['old_exercise']!r} -> {c['new_exercise']!r} "
                  f"set {c['set_number']}, {c['weight_lb']} lb x {c['reps']}"
                  + (" [failure]" if c["to_failure"] else ""))
            if dry_run:
                continue
            await conn.execute(
                "UPDATE lift_sets SET exercise = ?, set_number = ?, "
                "reps = ?, weight_lb = ?, to_failure = ?, notes = ? "
                "WHERE id = ?",
                (c["new_exercise"], c["set_number"], c["reps"],
                 c["weight_lb"], c["to_failure"], c["notes"],
                 c["lift_sets_id"]),
            )
            # Keep the parent `lifts` row's exercise/details in sync too --
            # that table (not lift_sets) is what "LIFTS LOGGED TODAY" and
            # the plan-adherence exercise matching read from.
            details = (
                f"set {c['set_number']}: {c['reps']}x{c['weight_lb']:g}lb"
                + (" (to failure)" if c["to_failure"] else "")
            )
            await conn.execute(
                "UPDATE lifts SET exercise = ?, details = ? WHERE id = ?",
                (c["new_exercise"], details, c["lift_id"]),
            )
        for r in RENUMBER:
            print(f"[renumber] lift_sets id={r['lift_sets_id']} "
                  f"({r['exercise']}): set_number -> {r['new_set_number']}")
            if dry_run:
                continue
            await conn.execute(
                "UPDATE lift_sets SET set_number = ? WHERE id = ?",
                (r["new_set_number"], r["lift_sets_id"]),
            )
        if not dry_run:
            await conn.commit()


async def apply_additions(db: Database, notion: NotionClient, dry_run: bool) -> None:
    for a in ADDITIONS:
        details = f"set {a['set_number']}: {a['reps']}x{a['weight_lb']:g}lb"
        print(f"[add] {a['exercise']} set {a['set_number']}: "
              f"{a['weight_lb']} lb x {a['reps']}")
        if dry_run:
            continue

        lift_id = await db.log_lift(
            date=DATE, exercise=a["exercise"], details=details, raw=RAW,
        )
        set_db_id = await db.log_lift_set(
            lift_id=lift_id,
            date=DATE,
            exercise=a["exercise"],
            set_number=a["set_number"],
            reps=a["reps"],
            weight_lb=a["weight_lb"],
            source="backfill",
        )
        print(f"  -> lifts id={lift_id}, lift_sets id={set_db_id}")

        parent_lift_notion_id = None
        try:
            parent_lift_notion_id = await notion.log_lift(
                date=DATE, exercise=a["exercise"], sets=1,
                reps=a["reps"], weight_lb=a["weight_lb"],
                notes=details, lift_id=lift_id,
            )
        except Exception as e:
            print(f"  -> Notion Lift write failed ({type(e).__name__}: {e})")

        if notion.is_configured_lift_sets():
            try:
                await notion.log_lift_set(
                    date=DATE, exercise=a["exercise"],
                    set_number=a["set_number"], reps=a["reps"],
                    weight_lb=a["weight_lb"], source="backfill",
                    parent_lift_page_id=parent_lift_notion_id,
                    lift_set_id=set_db_id,
                )
            except Exception as e:
                print(f"  -> Notion Lift Set write failed ({type(e).__name__}: {e})")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    db = Database(Config.DB_PATH)
    await db.initialize()

    if args.check:
        await check_existing(db)
        return

    print(f"Shoulder press weight convention: {SHOULDER_PRESS_TOTAL_LB} lb "
          f"total per set (change SHOULDER_PRESS_TOTAL_LB at the top of "
          f"this script if that's wrong).\n")

    await apply_corrections(db, dry_run=args.dry_run)
    print()
    notion = NotionClient(Config)
    await apply_additions(db, notion, dry_run=args.dry_run)

    print("\nNote: Notion pages already created for the misattributed sets "
          "(under the wrong exercise name) are NOT corrected by this "
          "script -- only SQLite. Notion is a mirror; fix those 4 pages "
          "by hand if it matters, or leave them (SQLite is the source of "
          "truth per AGENTS.md).")

    if args.dry_run:
        print("\nDry run complete, nothing written.")
    else:
        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
