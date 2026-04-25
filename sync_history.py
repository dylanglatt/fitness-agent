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

    # Workouts (per-session HR, zones, strain). These are the rows the
    # correlated-runs query joins against — essential for any running
    # performance trend question.
    n = 0
    async for rec in whoop.iter_all_workouts(start=start_s, end=end_s):
        row = whoop.normalize_workout(rec)
        if row.get("workout_id"):
            await db.upsert_whoop_workout(row, rec)
            n += 1
            if n % 25 == 0:
                logger.info(f"  WHOOP workouts: {n} records…")
    logger.info(f"WHOOP workouts: {n} records upserted.")

    await db.set_sync_state(
        "whoop",
        datetime.utcnow().isoformat(timespec="seconds") + "Z",
        last_record_date=datetime.utcnow().strftime("%Y-%m-%d"),
        note="full backfill",
    )


async def backfill_strava(
    config: Config,
    db: Database,
    after_ts: int,
    *,
    enrich: bool = True,
    fetch_zones: bool = True,
):
    """Backfill Strava activities into SQLite.

    By default we enrich every activity by also calling /activities/{id}
    (gets Avg HR / Max HR — the list endpoint omits these) and
    /activities/{id}/zones (HR zone time distribution). That means up to
    2 API calls per activity instead of 1.

    Strava's rate limits (as of 2024): 100 req per 15-min window, 1000/day.
    With enrich+zones we sleep 0.2s between activities (= 5 req/sec ≈ 75 req
    in 15 min when you account for the burst — well under the cap). For a
    one-time backfill of ~250 activities, expect ~2 minutes.
    """
    strava = StravaClient(config)
    n = 0
    # The pagination call counts against the rate limit too; budget for it
    # by sleeping a beat per activity rather than per API call. 0.2s × 2
    # calls = 0.4s per activity = 150 activities per minute = 9000/hour, way
    # over the 4000/hour cap. The sleep below shifts that to ~250/min.
    pacing_s = 0.4 if (enrich and fetch_zones) else 0.2 if enrich else 0.0
    async for activity in strava.iter_all_activities(after=after_ts):
        if enrich:
            try:
                activity = await strava.enrich_activity(
                    activity, fetch_zones=fetch_zones
                )
            except Exception as e:
                logger.warning(
                    f"Strava enrichment failed for {activity.get('id')}: {e} "
                    "(falling back to summary-only data)"
                )
        await db.upsert_strava_activity(activity)
        n += 1
        if n % 25 == 0:
            logger.info(f"  Strava: {n} activities…")
        if pacing_s:
            await asyncio.sleep(pacing_s)
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
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip per-activity Detail+Zones enrichment. Faster but loses HR data.",
    )
    parser.add_argument(
        "--no-zones",
        action="store_true",
        help="Skip HR-zone fetch (still enriches with HR detail). Halves API cost.",
    )
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
        enrich = not args.no_enrich
        fetch_zones = enrich and not args.no_zones
        if enrich:
            logger.info(
                f"Per-activity enrichment ON (HR{' + zones' if fetch_zones else ''}). "
                "Expect ~2-4 minutes for a few hundred activities."
            )
        try:
            await backfill_strava(
                config, db, after_ts, enrich=enrich, fetch_zones=fetch_zones
            )
        except Exception as e:
            logger.error(f"Strava backfill failed: {e}", exc_info=True)

    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
