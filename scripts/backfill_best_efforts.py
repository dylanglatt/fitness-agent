"""
Backfill Strava best efforts into strava_best_efforts.

Why this is needed: `best_efforts` only appears on the DETAILED activity
response, so any activity that was ingested from the list endpoint (or whose
enrichment call silently failed — which it did, at debug level, for months)
has no efforts stored. This walks the activities already in SQLite, fetches
the detail for runs only, and populates the table.

Best efforts are the objective running-progression signal that still works
even though every activity arrives via WHOOP's Strava upload with the
heart-rate stream stripped (`has_heartrate: false`, `external_id`
`stripped_...`), which nulls avg HR, HR zones and suffer_score.

Run it ON THE DROPLET, where the live SQLite lives:

    cd ~/fitness-bot && venv/bin/python scripts/backfill_best_efforts.py

Options:
    --days N     only activities in the last N days (default: all)
    --limit N    stop after N detail fetches (default: 400)
    --dry-run    fetch and report, write nothing

Idempotent and safe to re-run: writes are upserts keyed on
(activity_id, name), and activities that already have efforts are skipped
unless --force is passed.

Rate limits: Strava allows 100 requests per 15 minutes. We sleep between
fetches to stay well under that, so a full backfill of a year of running
takes a few minutes. That is fine — run it once.
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import aiosqlite  # noqa: E402

import obs  # noqa: E402
from config import Config  # noqa: E402
from data.database import Database  # noqa: E402
from integrations.strava import StravaClient  # noqa: E402

logger = logging.getLogger("backfill_best_efforts")

# Seconds between detail fetches. 100 req / 15 min = one per 9s at the cap;
# 1.2s is ~50 req/min, which bursts under the short-window limit for a few
# minutes and is comfortably inside the daily one.
THROTTLE_S = 1.2


async def _candidates(db_path: str, days: int | None, force: bool) -> list[tuple]:
    """Runs in SQLite that don't yet have best efforts stored."""
    where = ["sport_type LIKE '%Run%'"]
    params: list = []
    if days:
        where.append("date >= date('now', ?)")
        params.append(f"-{days} days")
    if not force:
        where.append(
            "activity_id NOT IN (SELECT DISTINCT activity_id FROM strava_best_efforts)"
        )
    sql = (
        "SELECT activity_id, date FROM strava_activities "
        f"WHERE {' AND '.join(where)} ORDER BY date DESC"
    )
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(sql, params)
        return [tuple(r) for r in await cur.fetchall()]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    obs.setup_logging("backfill-best-efforts")
    config = Config()
    db = Database(config.DB_PATH)
    await db.initialize()
    strava = StravaClient(config)

    rows = await _candidates(config.DB_PATH, args.days, args.force)
    if not rows:
        logger.info("Nothing to backfill — every run already has efforts stored.")
        return 0
    todo = rows[: args.limit]
    logger.info(
        "%d run(s) missing best efforts; fetching detail for %d (throttle %.1fs).",
        len(rows),
        len(todo),
        THROTTLE_S,
    )

    written = skipped = failed = 0
    for i, (activity_id, date) in enumerate(todo, 1):
        try:
            detail = await strava.get_activity_detail(int(activity_id))
        except Exception as e:
            failed += 1
            obs.source_failed(
                logger, "strava_activity_detail", "backfill", e, activity_id=activity_id
            )
            await asyncio.sleep(THROTTLE_S)
            continue

        efforts = (detail or {}).get("best_efforts") or []
        if not efforts:
            skipped += 1
        elif args.dry_run:
            written += 1
        else:
            async with aiosqlite.connect(config.DB_PATH) as conn:
                await db._upsert_best_efforts_conn(conn, int(activity_id), date, detail)
                await conn.commit()
            written += 1

        if i % 25 == 0 or i == len(todo):
            logger.info(
                "  %d/%d — %d with efforts, %d without, %d failed",
                i,
                len(todo),
                written,
                skipped,
                failed,
            )
        await asyncio.sleep(THROTTLE_S)

    verb = "would write" if args.dry_run else "wrote"
    logger.info(
        "Done: %s efforts for %d activities; %d had none; %d fetch failures.",
        verb,
        written,
        skipped,
        failed,
    )
    if not args.dry_run:
        for row in await db.get_best_effort_summary(days=3650):
            logger.info(
                "  %-14s n=%-3d best=%ss last=%ss on %s",
                row["name"],
                row["n"],
                row["best_s"],
                row["last_s"],
                row["last_date"],
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
