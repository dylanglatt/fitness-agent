"""One-off: correct lift session data for 2026-09-11, 2026-09-18, and
2026-09-23. Same two bugs as the 2026-09-25 session (see
scripts/backfill_missed_sets_2026_09_25.py for the full diagnosis, and
ai/coach.py for the code fix that stops this going forward):

  1. Bare continuation replies ("Next set, same thing") have no digits or
     lift keywords, so they never reached the logger -- sets silently
     dropped, no trace in the DB.
  2. Replies that don't restate the exercise name mid-conversation get
     misattributed to whatever exercise was last actually written to the
     DB, when that's a different, unrelated exercise.

One ambiguity, flagged rather than guessed silently: 2026-09-18's
shoulder press was described as "90 lbs each side." That same session
recorded chest row's "90 each side" as 180 total, but sissy squat's
"25 each side" as just 25 (not doubled) -- the convention wasn't even
consistent within the one day. This script defaults to doubling (180
total), matching the row. Change SHOULDER_PRESS_0918_TOTAL_LB below if
that's wrong.

Run once on the droplet:
  venv/bin/python scripts/backfill_2026_09_11_18_23.py --check
  venv/bin/python scripts/backfill_2026_09_11_18_23.py --dry-run
  venv/bin/python scripts/backfill_2026_09_11_18_23.py
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

SHOULDER_PRESS_0918_TOTAL_LB = 180.0  # see docstring; flip to 90.0 if wrong

# ── Corrections: rows already in the DB with the right numbers but the
# wrong exercise/set_number label. ids are exact, from the droplet dump.
CORRECTIONS = [
    # --- 2026-09-18: shoulder press set 1 misattributed to leg curl ---
    {
        "date": "2026-09-18", "lift_sets_id": 478, "lift_id": 445,
        "old_exercise": "Lying leg curl", "new_exercise": "Plate-loaded shoulder press",
        "set_number": 1, "reps": 10, "weight_lb": SHOULDER_PRESS_0918_TOTAL_LB,
        "to_failure": 0, "notes": "",
    },
    # --- 2026-09-23: all 3 shoulder press sets misattributed to leg press ---
    {
        "date": "2026-09-23", "lift_sets_id": 487, "lift_id": 454,
        "old_exercise": "Linear leg press", "new_exercise": "Plate-loaded shoulder press",
        "set_number": 1, "reps": 8, "weight_lb": 180.0,
        "to_failure": 0, "notes": "",
    },
    {
        "date": "2026-09-23", "lift_sets_id": 488, "lift_id": 455,
        "old_exercise": "Linear leg press", "new_exercise": "Plate-loaded shoulder press",
        "set_number": 2, "reps": 6, "weight_lb": 230.0,
        "to_failure": 0, "notes": "",
    },
    {
        "date": "2026-09-23", "lift_sets_id": 489, "lift_id": 456,
        "old_exercise": "Linear leg press", "new_exercise": "Plate-loaded shoulder press",
        "set_number": 3, "reps": 8, "weight_lb": 230.0,
        "to_failure": 0, "notes": "",
    },
]

# ── Renumbers: existing row is correctly attributed, just has the wrong
# set_number because a dropped/misattributed set threw off the sequence.
RENUMBER = [
    # 2026-09-11: shoulder press's real set 2 (80x10) was dropped; this
    # row is really set 3.
    {"date": "2026-09-11", "lift_sets_id": 430, "lift_id": 408,
     "exercise": "Shoulder press", "new_set_number": 3},
]

# ── Additions: sets that never made it into the DB in any form.
ADDITIONS = [
    {"date": "2026-09-11", "exercise": "Shoulder press", "set_number": 2, "reps": 10, "weight_lb": 80.0},

    {"date": "2026-09-18", "exercise": "Plate-loaded shoulder press", "set_number": 2, "reps": 10, "weight_lb": SHOULDER_PRESS_0918_TOTAL_LB},
    {"date": "2026-09-18", "exercise": "Seated chest supported row", "set_number": 3, "reps": 8, "weight_lb": 180.0},
    {"date": "2026-09-18", "exercise": "Lying leg curl", "set_number": 2, "reps": 10, "weight_lb": 75.0},
    {"date": "2026-09-18", "exercise": "Face pull", "set_number": 2, "reps": 12, "weight_lb": 42.5},
    {"date": "2026-09-18", "exercise": "Lat pulldown", "set_number": 2, "reps": 12, "weight_lb": 110.0},
    {"date": "2026-09-18", "exercise": "Preacher curl", "set_number": 2, "reps": 10, "weight_lb": 65.0},

    {"date": "2026-09-23", "exercise": "Single leg RDL", "set_number": 2, "reps": 8, "weight_lb": 30.0},
    {"date": "2026-09-23", "exercise": "Dumbbell hammer curl", "set_number": 2, "reps": 10, "weight_lb": 35.0},
    {"date": "2026-09-23", "exercise": "Cable lateral raise", "set_number": 2, "reps": 13, "weight_lb": 15.0},
    {"date": "2026-09-23", "exercise": "Calf raise", "set_number": 2, "reps": 10, "weight_lb": 135.0},
    {"date": "2026-09-23", "exercise": "Ab wheel", "set_number": 2, "reps": 10, "weight_lb": None},
    {"date": "2026-09-23", "exercise": "Ab wheel", "set_number": 3, "reps": 10, "weight_lb": None},
]

RAW = (
    "Manual correction (applied 2026-09-25): fixed sets misattributed to "
    "the wrong exercise by the chat lift parser, and added sets that were "
    "silently dropped because bare 'same thing' replies never reached the "
    "logger. See scripts/backfill_2026_09_11_18_23.py for the full "
    "diagnosis, cross-checked against the Discord transcript for each date."
)


async def check_existing(db: Database) -> None:
    for date in ["2026-09-11", "2026-09-18", "2026-09-23"]:
        rows = await db.get_lift_sets_for_date(date)
        print(f"=== {date} ({len(rows)} rows) ===")
        for r in rows:
            print(f"  id={r['id']:<4} lift_id={r['lift_id']:<4} "
                  f"{r.get('exercise')!r:34} set {r.get('set_number')}: "
                  f"{r.get('weight_lb')} lb x {r.get('reps')}")

    all_rows = []
    for date in ["2026-09-11", "2026-09-18", "2026-09-23"]:
        all_rows.extend(await db.get_lift_sets_for_date(date))
    ids_seen = {r["id"] for r in all_rows}

    for c in CORRECTIONS:
        if c["lift_sets_id"] not in ids_seen:
            print(f"\nWARNING: expected lift_sets id={c['lift_sets_id']} "
                  f"on {c['date']} for the {c['old_exercise']!r} -> "
                  f"{c['new_exercise']!r} correction, not found. Has this "
                  f"already run, or did ids change? Re-check before proceeding.")
    for r in RENUMBER:
        if r["lift_sets_id"] not in ids_seen:
            print(f"\nWARNING: expected lift_sets id={r['lift_sets_id']} "
                  f"on {r['date']} for the renumber step, not found.")


async def apply_corrections(db: Database, dry_run: bool) -> None:
    async with aiosqlite.connect(db.db_path) as conn:
        for c in CORRECTIONS:
            print(f"[correct {c['date']}] lift_sets id={c['lift_sets_id']}: "
                  f"{c['old_exercise']!r} -> {c['new_exercise']!r} "
                  f"set {c['set_number']}, {c['weight_lb']} lb x {c['reps']}")
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
            details = f"set {c['set_number']}: {c['reps']}x{c['weight_lb']:g}lb"
            await conn.execute(
                "UPDATE lifts SET exercise = ?, details = ? WHERE id = ?",
                (c["new_exercise"], details, c["lift_id"]),
            )
        for r in RENUMBER:
            print(f"[renumber {r['date']}] lift_sets id={r['lift_sets_id']} "
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
        w = a["weight_lb"]
        details = f"set {a['set_number']}: {a['reps']}x" + (f"{w:g}lb" if w is not None else "bw")
        print(f"[add {a['date']}] {a['exercise']} set {a['set_number']}: "
              f"{w} lb x {a['reps']}")
        if dry_run:
            continue

        lift_id = await db.log_lift(
            date=a["date"], exercise=a["exercise"], details=details, raw=RAW,
        )
        set_db_id = await db.log_lift_set(
            lift_id=lift_id,
            date=a["date"],
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
                date=a["date"], exercise=a["exercise"], sets=1,
                reps=a["reps"], weight_lb=a["weight_lb"],
                notes=details, lift_id=lift_id,
            )
        except Exception as e:
            print(f"  -> Notion Lift write failed ({type(e).__name__}: {e})")

        if notion.is_configured_lift_sets():
            try:
                await notion.log_lift_set(
                    date=a["date"], exercise=a["exercise"],
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

    print(f"2026-09-18 shoulder press weight convention: "
          f"{SHOULDER_PRESS_0918_TOTAL_LB} lb total per set (change "
          f"SHOULDER_PRESS_0918_TOTAL_LB at the top of this script if "
          f"that's wrong).\n")

    await apply_corrections(db, dry_run=args.dry_run)
    print()
    notion = NotionClient(Config)
    await apply_additions(db, notion, dry_run=args.dry_run)

    print("\nNote: Notion pages already created for the misattributed sets "
          "are NOT corrected by this script -- only SQLite. Fix those by "
          "hand if it matters, or leave them (SQLite is the source of "
          "truth per AGENTS.md).")

    if args.dry_run:
        print("\nDry run complete, nothing written.")
    else:
        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
