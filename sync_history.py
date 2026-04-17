"""
One-shot history backfill.

Pulls every WHOOP recovery / sleep / cycle record and every Strava activity
ever recorded for this user, and writes them to the SQLite database.

Safe to re-run — all writes are UPSERTs keyed on stable IDs (sleep id, cycle
id, strava activity id) or date (for recovery/sleep/cycle rolled up per day).
Re-running just refreshes any records WHOOP or Strava updated after the fact.

Usage:
    python sync_history.py                # pull everything
    python sync_history.py --days 30      # only last 30 days (for testing)
    python sync_history.py --whoop-only   # skip Strava
    python sync_history.py --strava-only  # skip WHOOP
"""

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from config import Config
from data.database import Database
from integrations.whoop import WhoopClient
from integrations.strava import StravaClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sync_history")


def _iso_z(dt: datetime) -> str:
    """WHOOP expects ISO 8601 with millisecond precision and Z suffix."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def backfill_whoop(config: Config, db: Database, start: datetime, end: datetime):
    whoop = WhoopClient(config)
    start_s = _iso_z(start)
    end_s = _iso_z(end)

    # Recovery
    n = 0
    async for rec in whoop.iter_all_recovery(start=start_s, end=end_s):
        date, row = whoop.normalize_recovery(rec)
        if date:
            await db.upsert_whoop_recovery(date, row, rec)
            n += 1
            if n % 25 == 0:
                logger.info(f"  WHOOP recovery: {n} records…")
    logger.info(f"WHOOP recovery: {n} records upserted.")

    # Sleep
    n = 0
    async for rec in whoop.iter_all_sleep(start=start_s, end=end_s):
        date, row = whoop.normalize_sleep(rec)
        if date:
            await db.upsert_whoop_sleep(date, row, rec)
            n += 1
            if n % 25 == 0:
                logger.info(f"  WHOOP sleep: {n} records…")
    logger.info(f"WHOOP sleep: {n} records upserted.")

    # Cycle (strain)
    n = 0
    async for rec in whoop.iter_all_cycles(start=start_s, end=end_s):
        date, row = whoop.normalize_cycle(rec)
        if date:
            await db.upsert_whoop_cycle(date, row, rec)
            n += 1
            if n % 25 == 0:
                logger.info(f"  WHOOP cycle: {n} records…")
    logger.info(f"WHOOP cycle: {n} records upserted.")

    await db.set_sync_state(
        "whoop",
        datetime.utcnow().isoformat(timespec="seconds") + "Z",
        last_record_date=datetime.utcnow().strftime("%Y-%m-%d"),
        note="full backfill",
    )


async def backfill_strava(config: Config, db: Database, after_ts: int):
    strava = StravaClient(config)
    n = 0
    async for activity in strava.iter_all_activities(after=after_ts):
        await db.upsert_strava_activity(activity)
        n += 1
        if n % 50 == 0:
            logger.info(f"  Strava: {n} activities…")
    logger.info(f"Strava: {n} activities upserted.")

    await db.set_sync_state(
        "strava",
        datetime.utcnow().isoformat(timespec="seconds") + "Z",
        last_record_date=datetime.utcnow().strftime("%Y-%m-%d"),
        note="full backfill",
    )


async def main():
    parser = argparse.ArgumentParser(description="Backfill WHOOP + Strava history.")
    parser.add_argument("--days", type=int, default=None,
                        help="Only pull this many days back (default: everything)")
    parser.add_argument("--whoop-only", action="store_true")
    parser.add_argument("--strava-only", action="store_true")
    args = parser.parse_args()

    config = Config()
    db = Database(config.DB_PATH)
    await db.initialize()

    end = datetime.utcnow()
    if args.days is not None:
        start = end - timedelta(days=args.days)
        after_ts = int(start.timestamp())
    else:
        # WHOOP supports fetching from Jan 1, 2015 at the earliest.
        # Going further back than that is cheap — if there's no data, you
        # just get empty pages and exit.
        start = datetime(2020, 1, 1)
        after_ts = 0  # Strava: 0 means "from the beginning"

    logger.info(
        f"Backfill window: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}"
    )

    if not args.strava_only:
        logger.info("── Backfilling WHOOP ─────────────────────────────")
        try:
            await backfill_whoop(config, db, start, end)
        except Exception as e:
            logger.error(f"WHOOP backfill failed: {e}", exc_info=True)

    if not args.whoop_only:
        logger.info("── Backfilling Strava ────────────────────────────")
        try:
            await backfill_strava(config, db, after_ts)
        except Exception as e:
            logger.error(f"Strava backfill failed: {e}", exc_info=True)

    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
