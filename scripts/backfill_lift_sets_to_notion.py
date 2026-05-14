"""
scripts/backfill_lift_sets_to_notion.py — one-shot mirror of SQLite
lift_sets rows into Notion's Lift Sets database, linked to the parent
Lifts rows via the "Parent Lift" relation.

Prereqs (in this order):
  1. scripts/backfill_lift_sets.py has been run — SQLite lift_sets table
     is populated from the historical lifts rows.
  2. NOTION_LIFT_SETS_DATABASE_ID is set in .env and the integration has
     been added to that database (••• → Connections in Notion).
  3. The Lift Sets DB has the columns expected by notion.log_lift_set
     (Name, Date, Exercise, Set Number, Reps, Weight (lb), Equipment,
     To Failure, RPE, Notes, Source, Parent Lift). Mismatched names
     cause silent property drops — _set_select etc. only write keys
     that exist on the database; the row still gets created with
     whatever did match.

Usage:
    python scripts/backfill_lift_sets_to_notion.py --dry-run    # plan, no writes
    python scripts/backfill_lift_sets_to_notion.py --sample 5   # write first 5, then stop
    python scripts/backfill_lift_sets_to_notion.py --since 2026-01-01
    python scripts/backfill_lift_sets_to_notion.py              # do it live

Safe to re-run: existing rows are detected via [liftsetrow:<id>] markers
in the Notes column and skipped. Partial-failure recovery is just rerun.

Rate limiting: Notion's public limit is ~3 req/sec. We sleep 0.4s between
writes to stay comfortably under. For ~300 sets expect ~2 minutes.

What this script does NOT do:
  - Update existing Lift Sets rows. If you fix a SQLite lift_sets row
    after Notion has it, the Notion row stays as it was. (Out of scope —
    the source of truth here is SQLite; Notion is the human view.)
  - Re-link orphaned rows. If a Lift Sets row got written without a
    Parent Lift relation (because the parent write failed at the time),
    this script will skip it as already-existing. To re-link, delete
    the orphan in Notion and rerun.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

# Allow running from repo root: `python scripts/backfill_lift_sets_to_notion.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from data.database import Database
from integrations.notion import NotionClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_lift_sets_to_notion")


# Dedup markers we scan Notion Notes for.
_LIFTROW_MARKER = re.compile(r"\[liftrow:(\d+)\]")
_LIFTSETROW_MARKER = re.compile(r"\[liftsetrow:(\d+)\]")

NOTION_RATE_LIMIT_SLEEP_S = 0.4  # ~2.5 req/s, comfortably under Notion's 3/s limit


async def build_parent_index(notion: NotionClient, since: str | None) -> dict[int, str]:
    """Scan Notion Lifts DB and return {sqlite_lift_id: notion_page_id}.

    We can't query Notion by an arbitrary text marker — instead we pull
    every Lifts row in the relevant date window and pluck the [liftrow:N]
    marker out of Notes. Slower than an indexed lookup but only runs at
    backfill time.
    """
    if not notion.is_configured_lifts():
        logger.error("Lifts DB not configured — can't resolve parent rows.")
        return {}

    pages = await notion._query_pages_since(
        notion.lifts_db_id, "Date", since or "1970-01-01",
    )
    index: dict[int, str] = {}
    for page in pages:
        text = notion._notes_text(page)
        for m in _LIFTROW_MARKER.finditer(text):
            try:
                lift_id = int(m.group(1))
                # Keep the FIRST match — if duplicates exist in Notion,
                # the earlier write is the canonical one.
                index.setdefault(lift_id, page["id"])
            except (ValueError, KeyError):
                continue
    logger.info("Found %d Notion Lifts rows with [liftrow:N] markers.", len(index))
    return index


async def build_existing_set_marker_index(
    notion: NotionClient, since: str | None,
) -> set[int]:
    """Scan Notion Lift Sets DB and return the set of SQLite lift_set ids
    that already exist there (via [liftsetrow:M] markers in Notes)."""
    pages = await notion._query_pages_since(
        notion.lift_sets_db_id, "Date", since or "1970-01-01",
    )
    ids: set[int] = set()
    for page in pages:
        text = notion._notes_text(page)
        for m in _LIFTSETROW_MARKER.finditer(text):
            try:
                ids.add(int(m.group(1)))
            except ValueError:
                continue
    logger.info("Found %d existing Lift Sets rows with [liftsetrow:M] markers.", len(ids))
    return ids


async def run(args: argparse.Namespace) -> int:
    cfg = Config()
    db = Database(cfg.DB_PATH)
    await db.initialize()

    notion = NotionClient(cfg)
    if not notion.is_configured_lift_sets():
        logger.error(
            "NOTION_LIFT_SETS_DATABASE_ID is not set (or is a placeholder). "
            "Add it to your .env and share the database with the integration."
        )
        return 2

    # Collect all SQLite lift_sets rows (since cutoff if requested).
    all_lifts = await db.iter_lifts_for_backfill()
    lift_date_by_id = {row["id"]: row["date"] for row in all_lifts}
    lift_exercise_by_id = {row["id"]: row["exercise"] for row in all_lifts}

    # We can't iter lift_sets directly without a helper; use the per-exercise
    # query with a wide pattern. Simpler: pull all lift_sets via SQL directly.
    import aiosqlite
    rows: list[dict] = []
    async with aiosqlite.connect(cfg.DB_PATH) as sdb:
        sdb.row_factory = aiosqlite.Row
        sql = (
            "SELECT id, lift_id, date, exercise, set_number, reps, weight_lb, "
            "       equipment, to_failure, rpe, notes, source "
            "FROM lift_sets "
        )
        params: tuple = ()
        if args.since:
            sql += "WHERE date >= ? "
            params = (args.since,)
        sql += "ORDER BY date ASC, lift_id ASC, set_number ASC"
        async with sdb.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    logger.info(
        "Candidate lift_sets rows: %d (since=%s)",
        len(rows), args.since or "all",
    )

    # Build the Notion-side indexes ONCE up front.
    parent_index = await build_parent_index(notion, args.since)
    existing_set_ids = await build_existing_set_marker_index(notion, args.since)

    stats = {
        "scanned": 0,
        "skipped_existing": 0,
        "skipped_no_parent": 0,
        "written": 0,
        "failed": 0,
    }
    sample_lines: list[str] = []

    for row in rows:
        stats["scanned"] += 1
        set_id = row["id"]
        lift_id = row["lift_id"]
        if set_id in existing_set_ids:
            stats["skipped_existing"] += 1
            continue
        parent_page = parent_index.get(lift_id)
        if not parent_page:
            stats["skipped_no_parent"] += 1
            logger.warning(
                "No Notion parent Lift found for SQLite lift_id=%d (set %d on %s). "
                "Run the SQLite→Notion lifts reconciliation first.",
                lift_id, set_id, row["date"],
            )
            continue

        sample = (
            f"  [{row['date']}] {row['exercise']:25s} set {row['set_number']}: "
            f"{row['weight_lb']}lb × {row['reps']} reps  "
            f"[{row.get('equipment') or '-'}, fail={bool(row['to_failure'])}, "
            f"src={row['source']}, lift_set_id={set_id}, parent={parent_page[:8]}…]"
        )

        if args.dry_run or (args.sample and stats["written"] >= args.sample):
            sample_lines.append(sample)
            if args.dry_run is False and args.sample and len(sample_lines) >= args.sample:
                break
            continue

        try:
            page_id = await notion.log_lift_set(
                date=row["date"],
                exercise=row["exercise"],
                set_number=row["set_number"],
                reps=row["reps"],
                weight_lb=row["weight_lb"],
                equipment=row["equipment"],
                to_failure=bool(row["to_failure"]),
                rpe=row["rpe"],
                notes=row["notes"] or "",
                source=row["source"],
                parent_lift_page_id=parent_page,
                lift_set_id=set_id,
            )
            if page_id:
                stats["written"] += 1
                if len(sample_lines) < 10:
                    sample_lines.append(sample + f"  → {page_id[:8]}…")
            else:
                stats["failed"] += 1
        except Exception as e:
            stats["failed"] += 1
            logger.warning("Write failed for set_id=%d: %s", set_id, e)

        await asyncio.sleep(NOTION_RATE_LIMIT_SLEEP_S)

        if args.sample and stats["written"] >= args.sample:
            break

    # ── Report ──────────────────────────────────────────────────────────
    print("\n── Notion backfill summary ──────────────────────────────────")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(
        "  mode:",
        "DRY-RUN" if args.dry_run else (f"SAMPLE({args.sample})" if args.sample else "LIVE"),
    )
    if sample_lines:
        print("\n── Sample rows ────────────────────────────────────────────")
        for line in sample_lines:
            print(line)
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="plan & print, no writes")
    p.add_argument("--sample", type=int, default=0, help="stop after writing N rows")
    p.add_argument("--since", type=str, default=None,
                   help="only process rows on or after YYYY-MM-DD")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
