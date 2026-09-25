"""One-off: backfill two exercises from Friday 2026-09-25's lift session that
never made it into the database. The chat acknowledged both as "logged" at
the time, but the coach's chat-side lift parser silently misattributed them
to "Single leg lying curl" (the last exercise actually written to the DB)
because Dylan's replies didn't repeat the exercise name and the parser only
had the last DB row for context, not the coach's own preceding question.
See the ai/coach.py / ai/prompts.py fixes from the same date for the parser
and reply-grounding changes that stop this going forward.

Numbers taken directly from the Discord chat transcript:
  - Plate-loaded shoulder press: set 1 115 lb x 10, set 2 115 lb x 7 (failure)
  - Single-leg lying curl: set 1 45 lb x 10 (each side), set 2 45 lb x 10 (each side)
    (this exercise WAS correctly parsed and logged with these exact numbers
    at the time -- included here only for verification via --check; do not
    re-insert it blindly, see main() below.)

Run once on the droplet: venv/bin/python scripts/backfill_missed_sets_2026_09_25.py
Add --dry-run to preview without writing.
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from data.database import Database  # noqa: E402
from integrations.notion import NotionClient  # noqa: E402

DATE = "2026-09-25"  # Friday

# Only the exercise that's actually missing gets inserted. Single-leg lying
# curl was logged correctly at the time (just not being recognized in the
# session recap due to a name-matching issue, not a missing-data issue) --
# --check below verifies that before this script touches anything, so we
# don't double-log it.
MISSING_EXERCISE = {
    "exercise": "Plate-loaded shoulder press",
    "sets": [
        {"set_number": 1, "reps": 10, "weight_lb": 115},
        {"set_number": 2, "reps": 7, "weight_lb": 115, "note": "to failure"},
    ],
}

# For --check only: what should already be in the DB. If this comes back
# short, the leg curl sets are ALSO missing and need a second exercise
# block added here before running for real -- don't guess numbers, ask Dylan.
EXPECTED_ALREADY_LOGGED = {
    "exercise": "Single leg lying curl",
    "sets": [
        {"set_number": 1, "reps": 10, "weight_lb": 45},
        {"set_number": 2, "reps": 10, "weight_lb": 45},
    ],
}

RAW = (
    "Manual backfill (2026-09-25 session): plate-loaded shoulder press sets "
    "were acknowledged as logged in chat but never written to the database "
    "-- misattributed to Single leg lying curl by the chat lift parser. "
    "Numbers confirmed from the Discord transcript, not re-entered by Dylan."
)


async def check_existing(db: Database) -> None:
    """Print what's actually in lift_sets for today, for both exercises,
    before writing anything. Run with --check (no writes) first.
    """
    rows = await db.get_lift_sets_for_date(DATE)
    print(f"lift_sets rows currently on {DATE}: {len(rows)}")
    for r in rows:
        print(f"  - {r.get('exercise')!r} set {r.get('set_number')}: "
              f"{r.get('weight_lb')} lb x {r.get('reps')}")

    missing_name = MISSING_EXERCISE["exercise"].lower()
    already_there = [r for r in rows if (r.get("exercise") or "").lower() == missing_name]
    if already_there:
        print(f"\nWARNING: {MISSING_EXERCISE['exercise']!r} already has "
              f"{len(already_there)} row(s) logged. Re-running the backfill "
              f"would create duplicates -- do not run without --dry-run/"
              f"--check first, and remove any exercise block that's already "
              f"present.")
    else:
        print(f"\nOK: no existing rows for {MISSING_EXERCISE['exercise']!r} "
              f"on {DATE} -- safe to backfill.")

    curl_name = EXPECTED_ALREADY_LOGGED["exercise"].lower()
    curl_rows = [r for r in rows if (r.get("exercise") or "").lower() == curl_name]
    if len(curl_rows) < len(EXPECTED_ALREADY_LOGGED["sets"]):
        print(f"\nNOTE: expected {len(EXPECTED_ALREADY_LOGGED['sets'])} "
              f"{EXPECTED_ALREADY_LOGGED['exercise']!r} rows already logged, "
              f"found {len(curl_rows)}. If it's actually missing too, add a "
              f"second block to MISSING_EXERCISE-style handling below instead "
              f"of assuming these numbers.")


async def backfill_exercise(db: Database, notion: NotionClient, ex: dict, dry_run: bool) -> None:
    sets = ex["sets"]
    details = "; ".join(
        f"set {s['set_number']}: {s['reps']}x{s['weight_lb']}lb"
        + (f" ({s['note']})" if s.get("note") else "")
        for s in sets
    )
    print(f"[{ex['exercise']}] {details}")

    if dry_run:
        print("  -> dry-run, nothing written")
        return

    lift_id = await db.log_lift(
        date=DATE, exercise=ex["exercise"], details=details, raw=RAW,
    )
    print(f"  -> SQLite lifts row id={lift_id}")

    set_records = []
    for s in sets:
        set_db_id = await db.log_lift_set(
            lift_id=lift_id,
            date=DATE,
            exercise=ex["exercise"],
            set_number=s["set_number"],
            reps=s["reps"],
            weight_lb=s["weight_lb"],
            notes=s.get("note", ""),
            source="backfill",
        )
        set_records.append({**s, "set_db_id": set_db_id})
    print(f"  -> logged {len(set_records)} lift_sets rows")

    top = sets[-1]
    parent_lift_notion_id = None
    try:
        parent_lift_notion_id = await notion.log_lift(
            date=DATE,
            exercise=ex["exercise"],
            sets=len(sets),
            reps=top["reps"],
            weight_lb=top["weight_lb"],
            notes=details,
            lift_id=lift_id,
        )
        print(f"  -> Notion Lift page: {parent_lift_notion_id}")
    except Exception as e:
        print(f"  -> Notion Lift write failed ({type(e).__name__}: {e}) "
              f"-- SQLite row is still committed.")

    if notion.is_configured_lift_sets():
        for rec in set_records:
            try:
                await notion.log_lift_set(
                    date=DATE,
                    exercise=ex["exercise"],
                    set_number=rec["set_number"],
                    reps=rec["reps"],
                    weight_lb=rec["weight_lb"],
                    notes=rec.get("note", ""),
                    source="backfill",
                    parent_lift_page_id=parent_lift_notion_id,
                    lift_set_id=rec["set_db_id"],
                )
            except Exception as e:
                print(f"  -> Notion Lift Set write failed for set "
                      f"{rec['set_number']} ({type(e).__name__}: {e})")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would be written, write nothing.")
    parser.add_argument("--check", action="store_true",
                         help="Only print today's existing lift_sets rows and exit.")
    args = parser.parse_args()

    db = Database(Config.DB_PATH)
    await db.initialize()

    if args.check:
        await check_existing(db)
        return

    notion = NotionClient(Config)
    await backfill_exercise(db, notion, MISSING_EXERCISE, dry_run=args.dry_run)
    print("\nDone." if not args.dry_run else "\nDry run complete, nothing written.")


if __name__ == "__main__":
    asyncio.run(main())
