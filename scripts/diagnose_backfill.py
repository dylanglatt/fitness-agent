"""
scripts/diagnose_backfill.py — figure out where data is dropping out.

Compares SQLite (source of truth) to Notion (destination) for both
Strava activities and WHOOP days, and checks heart-rate coverage in the
SQLite Strava data so we can tell whether HR is missing UPSTREAM (Strava
never gave us HR) or DOWNSTREAM (we have HR but it didn't reach Notion).

Usage:
    python scripts/diagnose_backfill.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from config import Config  # noqa: E402
from integrations.notion import NotionClient, NOTION_BASE  # noqa: E402


async def _all_pages(notion: NotionClient, db_id: str) -> list[dict]:
    out: list[dict] = []
    cursor: str | None = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{NOTION_BASE}/databases/{db_id}/query",
                headers=notion._headers(),
                json=body,
            )
        if r.status_code != 200:
            print(f"  [error] Notion query failed: {r.status_code} {r.text[:200]}")
            return out
        data = r.json()
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            return out
        cursor = data.get("next_cursor")


def _prop(page: dict, key: str) -> dict:
    return page.get("properties", {}).get(key, {}) or {}


async def main() -> int:
    cfg = Config()
    notion = NotionClient(cfg)
    con = sqlite3.connect(cfg.DB_PATH)
    con.row_factory = sqlite3.Row

    print("=" * 72)
    print("STRAVA — SQLite vs Notion (Runs DB)")
    print("=" * 72)

    sqlite_strava = list(
        con.execute(
            "SELECT activity_id, date, sport_type, name, average_hr, raw_json "
            "FROM strava_activities ORDER BY date"
        )
    )
    print(f"SQLite strava_activities rows: {len(sqlite_strava)}")
    if sqlite_strava:
        print(f"  date range: {sqlite_strava[0]['date']} → {sqlite_strava[-1]['date']}")

    # HR coverage in SQLite — split by column vs raw_json.
    col_has_hr = sum(1 for r in sqlite_strava if r["average_hr"] is not None)
    raw_has_hr = 0
    raw_null_hr = 0
    raw_no_key = 0
    for r in sqlite_strava:
        if not r["raw_json"]:
            continue
        try:
            j = json.loads(r["raw_json"])
        except json.JSONDecodeError:
            continue
        if "average_heartrate" in j:
            if j["average_heartrate"] is not None:
                raw_has_hr += 1
            else:
                raw_null_hr += 1
        else:
            raw_no_key += 1

    print(f"  HR coverage:")
    print(f"    average_hr column populated:        {col_has_hr}/{len(sqlite_strava)}")
    print(f"    raw_json average_heartrate present: {raw_has_hr}")
    print(f"    raw_json average_heartrate is null: {raw_null_hr}")
    print(f"    raw_json key absent:                {raw_no_key}")

    sport_counts = Counter(r["sport_type"] for r in sqlite_strava)
    print(f"  sport_type breakdown:")
    for sport, n in sport_counts.most_common():
        print(f"    {sport}: {n}")

    print()
    print("Querying Notion Runs DB...")
    runs_pages = await _all_pages(notion, notion.runs_db_id) if notion.is_configured_runs() else []
    print(f"Notion Runs rows: {len(runs_pages)}")

    notion_dates = Counter()
    notion_hr_populated = 0
    notion_strava_ids: set[str] = set()
    for p in runs_pages:
        d = _prop(p, "Date").get("date") or {}
        if d.get("start"):
            notion_dates[d["start"][:10]] += 1
        hr = _prop(p, "Avg HR").get("number")
        if hr is not None:
            notion_hr_populated += 1
        notes_rt = _prop(p, "Notes").get("rich_text") or []
        notes = "".join(rt.get("plain_text", "") for rt in notes_rt)
        # Pull [strava:N] markers
        import re
        for m in re.finditer(r"\[strava:(\d+)\]", notes):
            notion_strava_ids.add(m.group(1))

    print(f"  HR populated in Notion: {notion_hr_populated}/{len(runs_pages)}")
    print(f"  unique [strava:N] markers in Notes: {len(notion_strava_ids)}")

    # Which SQLite activities are MISSING from Notion?
    sqlite_ids = {str(r["activity_id"]) for r in sqlite_strava if r["activity_id"]}
    missing_in_notion = sqlite_ids - notion_strava_ids
    extra_in_notion = notion_strava_ids - sqlite_ids
    print(f"  SQLite ids missing from Notion: {len(missing_in_notion)}")
    print(f"  Notion ids not in SQLite:       {len(extra_in_notion)}")
    if missing_in_notion:
        sample = list(missing_in_notion)[:5]
        print(f"    sample missing ids: {sample}")
        # Show the date / sport for these so we can spot a pattern
        for aid in sample:
            row = next((r for r in sqlite_strava if str(r["activity_id"]) == aid), None)
            if row:
                print(f"      {aid}: {row['date']} {row['sport_type']} \"{row['name']}\"")

    print()
    print("=" * 72)
    print("WHOOP — SQLite vs Notion (Daily Log DB)")
    print("=" * 72)

    rec_dates = {r[0] for r in con.execute("SELECT DISTINCT date FROM whoop_recovery WHERE date IS NOT NULL")}
    sleep_dates = {r[0] for r in con.execute("SELECT DISTINCT date FROM whoop_sleep WHERE date IS NOT NULL")}
    union_dates = rec_dates | sleep_dates
    print(f"SQLite unique recovery dates: {len(rec_dates)}")
    print(f"SQLite unique sleep dates:    {len(sleep_dates)}")
    print(f"SQLite UNION (any data):      {len(union_dates)}")
    if union_dates:
        print(f"  date range: {min(union_dates)} → {max(union_dates)}")

    print()
    print("Querying Notion Daily Log DB...")
    daily_pages = await _all_pages(notion, notion.daily_db_id) if notion.is_configured_daily() else []
    print(f"Notion Daily Log rows: {len(daily_pages)}")

    notion_daily_dates: set[str] = set()
    for p in daily_pages:
        d = _prop(p, "Day").get("date") or {}
        if d.get("start"):
            notion_daily_dates.add(d["start"][:10])

    missing_days = sorted(union_dates - notion_daily_dates)
    extra_days = sorted(notion_daily_dates - union_dates)
    print(f"  Days in SQLite but not in Notion: {len(missing_days)}")
    print(f"  Days in Notion but not in SQLite: {len(extra_days)}")
    if missing_days[:10]:
        print(f"    first 10 missing days: {missing_days[:10]}")
    if missing_days[-10:] and len(missing_days) > 10:
        print(f"    last 10 missing days:  {missing_days[-10:]}")

    print()
    print("=" * 72)
    print("SCHEDULE DB sanity check")
    print("=" * 72)
    sched_pages = await _all_pages(notion, notion.schedule_db_id) if notion.is_configured_schedule() else []
    print(f"Notion Schedule rows: {len(sched_pages)}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
