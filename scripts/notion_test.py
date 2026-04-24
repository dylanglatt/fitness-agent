"""
scripts/notion_test.py — verify Notion API credentials + database access.

Run this right after pasting your NOTION_API_KEY and the four database IDs
into .env. For each configured database it answers:

  1. Is the token valid? (401 means no)
  2. Is the database id right AND has the integration been invited?
     (404 means no — a very common snag; Notion integrations can't read a
      database they haven't been explicitly connected to via ... -> Connections)
  3. Can we create a row? (schema mismatch on properties is the common
     failure here — the column names in integrations/notion.py must match
     your database's actual columns.)

Usage:
    cd ~/Desktop/Projects/fitness-bot
    python scripts/notion_test.py          # read-only ping of all 4 DBs
    python scripts/notion_test.py --write  # also create one test row per DB
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Make "import config" / "import integrations.notion" resolve when this file
# is run directly from the scripts/ subfolder.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from integrations.notion import NotionClient  # noqa: E402


def _setup_hint() -> str:
    return (
        "Fill these in .env then rerun:\n"
        "    NOTION_API_KEY=secret_...\n"
        "    NOTION_SCHEDULE_DATABASE_ID=<32-char id>\n"
        "    NOTION_LIFTS_DATABASE_ID=<32-char id>\n"
        "    NOTION_RUNS_DATABASE_ID=<32-char id>\n"
        "    NOTION_DAILY_DATABASE_ID=<32-char id>\n"
        "\n"
        "How to get each:\n"
        "  API key: https://www.notion.so/profile/integrations -> + New integration\n"
        "           -> Internal -> copy the 'Internal Integration Secret'.\n"
        "  Database ids: open each database (Schedule/Lifts/Runs/Daily) -> copy\n"
        "                URL -> the 32-char chunk before the '?' is the id.\n"
        "  Then: open each database -> ... -> Connections -> add your integration.\n"
    )


async def main(write_test_rows: bool) -> int:
    config = Config()
    notion = NotionClient(config)

    print("-" * 72)
    print("Notion connectivity check (Schedule + Lifts + Runs + Daily Log)")
    print("-" * 72)

    if not notion.is_configured():
        print("x No Notion database is configured.\n")
        print(_setup_hint())
        return 2

    # ── Ping pass ────────────────────────────────────────────────────────────
    results = await notion.ping_all()
    any_failures = False
    for label, (ok, msg) in results.items():
        prefix = "OK " if ok else "x  "
        print(f"{prefix}{label:<9} - {msg}")
        if not ok:
            any_failures = True

    if any_failures:
        print()
        print("One or more databases failed the read-only check. Fix above and rerun.")
        return 1

    if not write_test_rows:
        print()
        print("Read-only check passed. Rerun with --write to try creating test rows.")
        return 0

    # ── Write-path smoke test ────────────────────────────────────────────────
    # Surfaces schema mismatches (column-name drift) now, rather than at 7am
    # tomorrow when the morning brief fires.
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"\nAttempting test writes for date={today}...")

    # Schedule first — Lifts/Runs/Daily will relate to this row.
    schedule_ok = True
    try:
        page_id = await notion.log_schedule(
            date=today,
            training_group="Other",
            workout="Other",
            notes="Delete me - created by scripts/notion_test.py",
        )
        print("OK Schedule test row created.")
        if not page_id:
            schedule_ok = False
    except Exception as e:
        schedule_ok = False
        print(f"x  Schedule test write failed: {e}")

    daily_ok = True
    try:
        await notion.log_daily_entry(
            date=today,
            summary={
                "recovery_score": 0,
                "hrv": 0,
                "rhr": 0,
                "sleep_hours": 0,
                "sleep_efficiency": 0,
                "notes": "Delete me - created by scripts/notion_test.py",
                "daily_brief": "(test row - connectivity check)",
            },
        )
        print("OK Daily Log test row created.")
    except Exception as e:
        daily_ok = False
        print(f"x  Daily Log test write failed: {e}")

    lifts_ok = True
    try:
        await notion.log_lift(
            date=today,
            exercise="Bench press",
            workout="Push",
            sets=3,
            reps=10,
            weight_lb=135,
            rpe=7,
            notes="Connectivity test - delete me",
        )
        print("OK Lifts test row created.")
    except Exception as e:
        lifts_ok = False
        print(f"x  Lifts test write failed: {e}")

    runs_ok = True
    try:
        await notion.log_run(
            date=today,
            name="[notion_test.py] Sample run",
            type="Run",
            distance_mi=3.1,
            pace="8:52/mi",
            duration_min=27.5,
            avg_hr=148,
            elevation_gain_ft=45,
            zone_1_pct=5,
            zone_2_pct=68,
            zone_3_pct=20,
            zone_4_pct=5,
            zone_5_pct=2,
            source="Manual",
            notes="Connectivity test - delete me",
        )
        print("OK Runs test row created.")
    except Exception as e:
        runs_ok = False
        print(f"x  Runs test write failed: {e}")

    print()
    print("Open each database in Notion and verify the test rows look right,")
    print("then delete them. If a column came through blank unexpectedly,")
    print("the Notion DB column name or type doesn't match what")
    print("integrations/notion.py writes.")
    print()
    print("Also: the Schedule row for today should now show the Lifts/Runs/")
    print("Daily Log relations populated automatically via the Notion Relations.")

    return 0 if (schedule_ok and daily_ok and lifts_ok and runs_ok) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Also attempt to create test rows in all four databases.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.write)))
