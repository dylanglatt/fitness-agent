"""
scripts/backfill_notion.py — one-shot import of historical WHOOP + Strava
data from SQLite into the Notion databases.

Prereqs (in this order):
  1. `python sync_history.py` must have populated SQLite (this script does
     NOT call the WHOOP/Strava APIs directly — it reads from data/fitness_bot.db).
  2. `python scripts/notion_test.py` must return 4x OK (all DBs configured
     and accessible by the integration).

Usage:
    python scripts/backfill_notion.py --dry-run           # print what would be written
    python scripts/backfill_notion.py --days 30 --dry-run # limit to last 30 days for smoke test
    python scripts/backfill_notion.py                     # do it live
    python scripts/backfill_notion.py --strava-only       # skip Daily Log
    python scripts/backfill_notion.py --whoop-only        # skip Runs
    python scripts/backfill_notion.py --start 2025-01-01 --end 2025-06-30

Safe to re-run: the script queries Notion up-front for already-imported
rows (Strava activity ids embedded in Runs.Notes as [strava:N], and Daily
Log rows by date) and skips them on subsequent runs. This means partial
failures are recoverable — just rerun.

Rate limiting: Notion's public limit is ~3 req/sec. We sleep 0.4s between
writes to stay comfortably under. With ~490 rows (221 Strava + 269 WHOOP),
expect 8–10 minutes for a full backfill.

What it does NOT backfill:
  - HR zone distribution on historical runs. That would require an extra
    /activities/{id}/zones call per Strava activity, which inflates
    runtime. Zones populate on runs going forward via webhook.
  - Daily Brief text for historical days. Those briefs were never written.
    The Daily Log rows have WHOOP numbers but the Brief column stays blank.
  - Historical lifts. The SQLite lifts table is empty in this install;
    there's nothing to backfill there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from config import Config  # noqa: E402
from integrations.notion import (  # noqa: E402
    NotionClient,
    NOTION_BASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_notion")

# Notion allows ~3 req/sec sustained. 0.4s between writes keeps us under
# that even counting the schedule-create overhead per row.
WRITE_DELAY_S = 0.4

# Re-used regex for pulling activity ids out of Notes markers like "[strava:12345]".
_STRAVA_MARKER = re.compile(r"\[strava:(\d+)\]")


# ── SQLite readers ──────────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _whoop_workouts_table_exists(con: sqlite3.Connection) -> bool:
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='whoop_workouts'"
    )
    return cur.fetchone() is not None


def read_strava(con: sqlite3.Connection, start: str | None, end: str | None) -> list[dict]:
    """Return Strava activities as raw-ish dicts compatible with
    log_strava_activity(), enriched with WHOOP workout HR + HR-zones when
    a matching WHOOP workout exists.

    JOIN strategy (mirrors database.get_correlated_runs_in_range):
      - Same calendar day (s.date = w.start_date)
      - Within ±30 min of start time (Strava and WHOOP timestamps for the
        same physical workout typically agree within seconds, but a 30-min
        window cushions clock skew, lap-button delays, and edits).

    Why prefer WHOOP HR over Strava HR: WHOOP captures HR continuously from
    the wrist with zone math tuned to the user's max HR. Strava HR depends
    on whether you paired a sensor at recording time and uses generic zone
    cutoffs. When WHOOP has the workout, its numbers are the right call.

    Strava HR (from Strava API) is used as a fallback when WHOOP didn't
    record the workout (e.g., you took the strap off for a swim).
    """
    have_whoop_workouts = _whoop_workouts_table_exists(con)

    if have_whoop_workouts:
        # The ±1800-second match is best-effort — Strava's start_date in
        # raw_json is UTC ISO, WHOOP's start_utc is also UTC ISO, so direct
        # epoch subtraction is meaningful. If raw_json lacks start_date we
        # fall back to date-only matching, which is fine for users with at
        # most one workout per day per sport (the common case for runners).
        q = """
            SELECT
                s.activity_id, s.date, s.sport_type, s.name,
                s.distance_m, s.moving_time_s, s.total_elevation_gain_m,
                s.average_hr AS strava_avg_hr,
                s.raw_json,
                w.average_hr  AS whoop_avg_hr,
                w.max_hr      AS whoop_max_hr,
                w.zone0_ms,   w.zone1_ms, w.zone2_ms,
                w.zone3_ms,   w.zone4_ms, w.zone5_ms,
                w.workout_id  AS whoop_workout_id
            FROM strava_activities s
            LEFT JOIN whoop_workouts w
              ON w.start_date = s.date
             AND ABS(
                   strftime('%s', w.start_utc)
                   - COALESCE(
                       strftime('%s', json_extract(s.raw_json, '$.start_date')),
                       strftime('%s', s.date || ' 00:00:00')
                     )
                 ) < 1800
        """
    else:
        q = "SELECT *, NULL AS whoop_avg_hr, NULL AS whoop_max_hr, "
        q += "NULL AS zone0_ms, NULL AS zone1_ms, NULL AS zone2_ms, "
        q += "NULL AS zone3_ms, NULL AS zone4_ms, NULL AS zone5_ms, "
        q += "NULL AS whoop_workout_id, average_hr AS strava_avg_hr "
        q += "FROM strava_activities s"

    where: list[str] = []
    params: list = []
    if start:
        where.append("s.date >= ?")
        params.append(start)
    if end:
        where.append("s.date <= ?")
        params.append(end)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY s.date ASC"

    activities: list[dict] = []
    matched_whoop = 0
    for row in con.execute(q, params):
        raw = row["raw_json"]
        if raw:
            try:
                act = json.loads(raw)
            except json.JSONDecodeError:
                act = {}
        else:
            act = {}

        act.setdefault("id", row["activity_id"])
        act.setdefault("name", row["name"])
        act.setdefault("sport_type", row["sport_type"])
        act.setdefault("distance", row["distance_m"])
        act.setdefault("moving_time", row["moving_time_s"])
        act.setdefault("total_elevation_gain", row["total_elevation_gain_m"])
        if not act.get("start_date_local") and row["date"]:
            act["start_date_local"] = f"{row['date']}T00:00:00"

        # ── HR resolution: WHOOP > Strava-detail > Strava-summary ───────────
        # raw_json may carry average_heartrate (from sync_history's enrich
        # path). WHOOP's avg HR is more authoritative when present, so we
        # override unconditionally rather than just-fill-on-null.
        if row["whoop_avg_hr"] is not None:
            act["average_heartrate"] = row["whoop_avg_hr"]
            matched_whoop += 1
        elif act.get("average_heartrate") is None and row["strava_avg_hr"] is not None:
            act["average_heartrate"] = row["strava_avg_hr"]

        # ── HR zones from WHOOP (zone0..zone5 milliseconds) ────────────────
        # We collapse zone0 + zone1 (WHOOP labels Z0=warmup-before-Z1) into
        # a single "Zone 1" so the Notion view stays five-zone like Strava
        # convention. Otherwise the user would see a Z0 column they didn't
        # ask for.
        zone_ms = [
            row["zone0_ms"], row["zone1_ms"], row["zone2_ms"],
            row["zone3_ms"], row["zone4_ms"], row["zone5_ms"],
        ]
        if any(z is not None for z in zone_ms):
            zone_ms = [z or 0 for z in zone_ms]
            total_ms = sum(zone_ms)
            if total_ms > 0:
                z1 = round(100.0 * (zone_ms[0] + zone_ms[1]) / total_ms, 1)
                z2 = round(100.0 * zone_ms[2] / total_ms, 1)
                z3 = round(100.0 * zone_ms[3] / total_ms, 1)
                z4 = round(100.0 * zone_ms[4] / total_ms, 1)
                z5 = round(100.0 * zone_ms[5] / total_ms, 1)
                # Stash under _whoop_zone_pcts so log_strava_activity (which
                # reads from positional kwargs) can pull them. We pop this
                # in the writer loop below.
                act["_whoop_zone_pcts"] = {
                    "zone_1_pct": z1, "zone_2_pct": z2, "zone_3_pct": z3,
                    "zone_4_pct": z4, "zone_5_pct": z5,
                }

        activities.append(act)

    if have_whoop_workouts:
        logger.info(
            f"  WHOOP-HR matched: {matched_whoop}/{len(activities)} Strava activities "
            "(rest fall back to Strava HR if available)"
        )
    else:
        logger.warning(
            "  whoop_workouts table doesn't exist — run `python sync_history.py "
            "--whoop-only` first to populate it, otherwise HR will be Strava-only."
        )
    return activities


def read_whoop_days(con: sqlite3.Connection, start: str | None, end: str | None) -> list[dict]:
    """Return one summary dict per day, joining recovery + sleep.

    A day only produces a row if we have EITHER a recovery OR a sleep
    record for it (usually we have both). Days with neither are skipped —
    there's nothing to show in Notion for those.
    """
    q = """
        SELECT
            COALESCE(r.date, s.date) AS date,
            r.recovery_score,
            r.hrv_rmssd_ms,
            r.resting_hr,
            s.total_asleep_hours,
            s.sleep_efficiency_pct
        FROM whoop_recovery r
        FULL OUTER JOIN whoop_sleep s ON r.date = s.date
    """
    # SQLite doesn't have FULL OUTER JOIN pre-3.39 on some systems; fall
    # back to a UNION pattern that's portable. Keeps this script runnable
    # on older Python / sqlite combos.
    q = """
        SELECT
            date,
            recovery_score,
            hrv_rmssd_ms,
            resting_hr,
            total_asleep_hours,
            sleep_efficiency_pct
        FROM (
            SELECT r.date AS date,
                   r.recovery_score,
                   r.hrv_rmssd_ms,
                   r.resting_hr,
                   s.total_asleep_hours,
                   s.sleep_efficiency_pct
            FROM whoop_recovery r
            LEFT JOIN whoop_sleep s ON r.date = s.date
            UNION
            SELECT s.date AS date,
                   r.recovery_score,
                   r.hrv_rmssd_ms,
                   r.resting_hr,
                   s.total_asleep_hours,
                   s.sleep_efficiency_pct
            FROM whoop_sleep s
            LEFT JOIN whoop_recovery r ON r.date = s.date
        )
        WHERE date IS NOT NULL
    """
    clauses: list[str] = []
    params: list = []
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    if clauses:
        q += " AND " + " AND ".join(clauses)
    q += " ORDER BY date ASC"

    days: list[dict] = []
    for row in con.execute(q, params):
        days.append(
            {
                "date": row["date"],
                "recovery_score": row["recovery_score"],
                "hrv": round(row["hrv_rmssd_ms"], 1) if row["hrv_rmssd_ms"] is not None else None,
                "rhr": row["resting_hr"],
                "sleep_hours": round(row["total_asleep_hours"], 1) if row["total_asleep_hours"] is not None else None,
                "sleep_efficiency": row["sleep_efficiency_pct"],
                "notes": "Backfilled by scripts/backfill_notion.py",
                "daily_brief": None,  # no historical brief to restore
            }
        )
    # Drop duplicate dates (the UNION can yield two rows when r+s both exist)
    seen: set[str] = set()
    deduped: list[dict] = []
    for d in days:
        if d["date"] in seen:
            continue
        seen.add(d["date"])
        deduped.append(d)
    return deduped


# ── Notion pre-flight: gather already-imported rows ─────────────────────────

async def _paginate_query(
    notion: NotionClient,
    db_id: str,
    filter_body: dict | None = None,
) -> list[dict]:
    """Paginate POST /v1/databases/{id}/query and return all result pages."""
    out: list[dict] = []
    start_cursor: str | None = None
    while True:
        body: dict = {"page_size": 100}
        if filter_body:
            body["filter"] = filter_body
        if start_cursor:
            body["start_cursor"] = start_cursor
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{NOTION_BASE}/databases/{db_id}/query",
                headers=notion._headers(),  # private method, but we own this integration
                json=body,
            )
        if resp.status_code != 200:
            logger.warning(f"Notion query {db_id} failed {resp.status_code}: {resp.text[:200]}")
            return out
        data = resp.json()
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            return out
        start_cursor = data.get("next_cursor")


async def existing_strava_ids(notion: NotionClient) -> set[str]:
    """Scan the Runs DB for [strava:N] markers in Notes. Returns the set of
    activity ids already imported, so subsequent runs can skip them."""
    if not notion.is_configured_runs():
        return set()
    pages = await _paginate_query(notion, notion.runs_db_id)
    ids: set[str] = set()
    for page in pages:
        props = page.get("properties", {})
        notes_prop = props.get("Notes", {})
        rt = notes_prop.get("rich_text") or []
        text = "".join(r.get("plain_text", "") for r in rt)
        for m in _STRAVA_MARKER.finditer(text):
            ids.add(m.group(1))
    return ids


async def existing_daily_dates(notion: NotionClient) -> set[str]:
    """Return the set of dates already present in Daily Log (via the Day
    column), so we don't double-write on re-run."""
    if not notion.is_configured_daily():
        return set()
    pages = await _paginate_query(notion, notion.daily_db_id)
    dates: set[str] = set()
    for page in pages:
        day = page.get("properties", {}).get("Day", {}).get("date") or {}
        start = day.get("start")
        if start:
            dates.add(start[:10])
    return dates


# ── The actual backfill ─────────────────────────────────────────────────────

async def run_backfill(args: argparse.Namespace) -> int:
    config = Config()
    notion = NotionClient(config)

    # Basic sanity — if the DBs we need aren't configured, bail early with a
    # clear message rather than failing row-by-row.
    if not args.whoop_only and not notion.is_configured_runs():
        logger.error("Runs DB not configured in .env — cannot backfill Strava.")
        return 2
    if not args.strava_only and not notion.is_configured_daily():
        logger.error("Daily Log DB not configured in .env — cannot backfill WHOOP.")
        return 2

    # Date window
    end: str | None = args.end
    start: str | None = args.start
    if args.days:
        # --days overrides --start. Use today as the inclusive end.
        today = datetime.now().date()
        start = (today - timedelta(days=args.days)).isoformat()
        end = today.isoformat()

    logger.info(f"Date window: {start or '(no start)'} → {end or '(no end)'}")
    logger.info(f"Mode: dry_run={args.dry_run} strava={not args.whoop_only} whoop={not args.strava_only}")

    con = _connect(config.DB_PATH)

    # Pre-flight: discover what's already imported so we can skip dupes.
    logger.info("Scanning Notion for already-imported rows…")
    done_strava: set[str] = set()
    done_dates: set[str] = set()
    if not args.dry_run:
        if not args.whoop_only:
            done_strava = await existing_strava_ids(notion)
            logger.info(f"  → {len(done_strava)} Strava activities already in Runs DB")
        if not args.strava_only:
            done_dates = await existing_daily_dates(notion)
            logger.info(f"  → {len(done_dates)} dates already in Daily Log")

    # ── Strava → Runs ───────────────────────────────────────────────────────
    strava_ok = strava_fail = 0
    if not args.whoop_only:
        activities = read_strava(con, start, end)
        to_write = [a for a in activities if str(a.get("id")) not in done_strava]
        logger.info(f"Strava: {len(activities)} activities in window, {len(to_write)} new to write")
        for i, act in enumerate(to_write, 1):
            label = f"[{i}/{len(to_write)}] {act.get('start_date_local','?')[:10]} {act.get('sport_type','?')}: {act.get('name','?')}"
            if args.dry_run:
                logger.info(f"DRY {label}")
                continue
            try:
                # WHOOP-derived zone percentages (preferred) take precedence;
                # if missing, fall back to Strava-derived zones from raw_json.
                whoop_zone_pcts = act.pop("_whoop_zone_pcts", None) if isinstance(act, dict) else None
                strava_zones_raw = act.pop("_zones", None) if isinstance(act, dict) else None
                # Route WeightTraining activities to the Lifts DB regardless
                # of whether we have WHOOP zones for them. Earlier this fell
                # through to the WHOOP-zones path below and stuffed lifts
                # into the Runs DB — which is wrong; the Lifts DB has its
                # own column shape and the Runs DB shouldn't carry strength
                # work mixed in with cardio. Lifts get logged without zones
                # because the Lifts DB schema doesn't have zone columns.
                from integrations.notion import _STRAVA_LIFT_TYPES
                sport_raw = act.get("sport_type", act.get("type", "Run"))
                if sport_raw in _STRAVA_LIFT_TYPES:
                    ok = await notion.log_strava_activity(act)  # routes to log_lift internally
                elif whoop_zone_pcts:
                    # Cardio with WHOOP zones — call log_run directly so we
                    # can pass the WHOOP-derived zone percentages instead of
                    # whatever Strava might have. We recompute name/distance/
                    # pace/etc. here so the row shape matches log_strava_activity.
                    from integrations.notion import (
                        _meters_to_miles, _meters_to_feet,
                        _seconds_to_minutes, _format_pace, _STRAVA_TYPE_MAP,
                    )
                    type_label = _STRAVA_TYPE_MAP.get(sport_raw, "Run")
                    distance_mi = _meters_to_miles(act.get("distance"))
                    duration_min = _seconds_to_minutes(act.get("moving_time"))
                    elevation_ft = _meters_to_feet(act.get("total_elevation_gain"))
                    pace = _format_pace(duration_min, distance_mi) if type_label in ("Run", "Hike", "Walk") else None
                    aid = act.get("id")
                    ok = await notion.log_run(
                        date=(act.get("start_date_local") or "")[:10],
                        name=act.get("name") or sport_raw,
                        type=type_label,
                        distance_mi=distance_mi,
                        pace=pace,
                        duration_min=duration_min,
                        avg_hr=act.get("average_heartrate"),
                        elevation_gain_ft=elevation_ft,
                        zone_1_pct=whoop_zone_pcts.get("zone_1_pct"),
                        zone_2_pct=whoop_zone_pcts.get("zone_2_pct"),
                        zone_3_pct=whoop_zone_pcts.get("zone_3_pct"),
                        zone_4_pct=whoop_zone_pcts.get("zone_4_pct"),
                        zone_5_pct=whoop_zone_pcts.get("zone_5_pct"),
                        source="WHOOP" if act.get("average_heartrate") else "Strava",
                        notes=f"[strava:{aid}]" if aid else None,
                    )
                else:
                    # Cardio without WHOOP zones — let log_strava_activity
                    # handle Strava zones (if any) the original way.
                    ok = await notion.log_strava_activity(act, zones=strava_zones_raw)
            except Exception as e:
                ok = False
                logger.warning(f"EXC {label} — {e}")
            if ok:
                strava_ok += 1
                logger.info(f"OK  {label}")
            else:
                strava_fail += 1
                # Notion-side failure (4xx/5xx) was already logged as a
                # warning by _create_page; this surfaces it at the row level
                # so the final summary count means something.
                logger.warning(f"FAIL {label} — Notion rejected the write (see warnings above)")
            await asyncio.sleep(WRITE_DELAY_S)

    # ── WHOOP → Daily Log ───────────────────────────────────────────────────
    whoop_ok = whoop_fail = 0
    if not args.strava_only:
        days = read_whoop_days(con, start, end)
        to_write = [d for d in days if d["date"] not in done_dates]
        logger.info(f"WHOOP: {len(days)} days in window, {len(to_write)} new to write")
        for i, summary in enumerate(to_write, 1):
            label = f"[{i}/{len(to_write)}] {summary['date']}  rec={summary['recovery_score']}  hrv={summary['hrv']}"
            if args.dry_run:
                logger.info(f"DRY {label}")
                continue
            try:
                ok = await notion.log_daily_entry(summary["date"], summary)
            except Exception as e:
                ok = False
                logger.warning(f"EXC {label} — {e}")
            if ok:
                whoop_ok += 1
                logger.info(f"OK  {label}")
            else:
                whoop_fail += 1
                logger.warning(f"FAIL {label} — Notion rejected the write (see warnings above)")
            await asyncio.sleep(WRITE_DELAY_S)

    con.close()
    logger.info("=" * 60)
    logger.info(f"Done. Strava: {strava_ok} ok / {strava_fail} failed.  WHOOP: {whoop_ok} ok / {whoop_fail} failed.")
    if strava_fail or whoop_fail:
        logger.info("Re-run the same command — failures are skipped on re-run, only the missing rows will be retried.")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Print what would be written, make no Notion writes.")
    p.add_argument("--days", type=int, default=0, help="Limit to the last N days. Overrides --start.")
    p.add_argument("--start", default=None, help="Start date (YYYY-MM-DD, inclusive).")
    p.add_argument("--end", default=None, help="End date (YYYY-MM-DD, inclusive).")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--strava-only", action="store_true", help="Skip WHOOP Daily Log backfill.")
    group.add_argument("--whoop-only", action="store_true", help="Skip Strava Runs backfill.")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_backfill(_parse_args())))
