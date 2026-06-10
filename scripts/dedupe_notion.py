#!/usr/bin/env python3
"""
De-duplicate Notion rows created by the pre-fix reconciliation/nightly-sync
double-writes (see the June 2026 logging audit).

WHAT IT DOES
------------
A row is a TRUE duplicate only when it shares an identical dedup marker with
another row in the same database:
    [strava:<id>]      — same Strava activity logged more than once
    [liftrow:<id>]     — same SQLite lift written more than once
    [liftsetrow:<id>]  — same SQLite per-set row written more than once
For each marker group with >1 row, the EARLIEST-created page is kept and the
rest are archived (moved to Notion trash — recoverable, not hard-deleted).

WHAT IT DELIBERATELY DOES NOT TOUCH
-----------------------------------
The mislabeled "two Set 1" rows (e.g. Overhead press 85x10 and 65x8 both tagged
Set 1 on the same day) are NOT duplicates — they carry DIFFERENT
[liftsetrow:<id>] markers and represent two real sets that were mis-numbered.
Deleting one would lose data. Those need renumbering, not dedup, and are left
alone here.

USAGE
-----
    # dry run (default) — prints what WOULD be archived, changes nothing
    python scripts/dedupe_notion.py
    python scripts/dedupe_notion.py --since 2026-05-01 --db all

    # actually archive the extras
    python scripts/dedupe_notion.py --apply

Run from the repo root so `config` and `integrations` import cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from config import Config  # noqa: E402
from integrations.notion import NotionClient, NOTION_BASE  # noqa: E402

# Any of these markers, if shared by two rows in the same DB, marks a duplicate.
_MARKERS = re.compile(r"\[(?:strava|liftrow|liftsetrow):\d+\]")


def _title_of(page: dict) -> str:
    """Best-effort human title — find the 'title'-typed property."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title") or [])
    return "(untitled)"


async def _archive_page(client: NotionClient, page_id: str) -> bool:
    """Archive (trash) a page. Recoverable from Notion's trash."""
    async with httpx.AsyncClient(timeout=20.0) as http:
        resp = await http.patch(
            f"{NOTION_BASE}/pages/{page_id}",
            headers=client._headers(),
            json={"archived": True},
        )
    if resp.status_code != 200:
        print(f"    ! archive failed {resp.status_code}: {resp.text[:160]}")
        return False
    return True


async def _dedupe_db(
    client: NotionClient, db_id: str, label: str, since: str, apply: bool
) -> tuple[int, int]:
    """Returns (groups_with_dupes, rows_archived) for one database."""
    pages = await client._query_pages_since(db_id, "Date", since)
    # marker -> list of (created_time, page_id, title)
    by_marker: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for p in pages:
        text = client._notes_text(p)
        for m in set(_MARKERS.findall(text)):  # set(): one vote per page per marker
            by_marker[m].append(
                (p.get("created_time", ""), p.get("id", ""), _title_of(p))
            )

    dupe_groups = {m: rows for m, rows in by_marker.items() if len(rows) > 1}
    if not dupe_groups:
        print(f"[{label}] no duplicate markers found (scanned {len(pages)} rows).")
        return 0, 0

    archived = 0
    print(f"[{label}] {len(dupe_groups)} marker(s) with duplicates:")
    for marker, rows in sorted(dupe_groups.items()):
        rows.sort(key=lambda r: r[0])  # earliest created_time first
        keep = rows[0]
        extras = rows[1:]
        print(f"  {marker}  ({len(rows)} copies) — keeping earliest {keep[0][:19]} \"{keep[2]}\"")
        for created, pid, title in extras:
            print(f"    {'ARCHIVE' if apply else 'would archive'}: {created[:19]}  {pid}  \"{title}\"")
            if apply:
                if await _archive_page(client, pid):
                    archived += 1
    return len(dupe_groups), archived


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually archive duplicates (default: dry run).")
    ap.add_argument("--since", default="2026-01-01",
                    help="Only scan rows with Date on/after this (YYYY-MM-DD).")
    ap.add_argument("--db", choices=["lifts", "liftsets", "runs", "all"],
                    default="all", help="Which database(s) to scan.")
    args = ap.parse_args()

    cfg = Config()
    client = NotionClient(cfg)

    targets: list[tuple[str, str, bool]] = [
        ("lifts", client.lifts_db_id, client.is_configured_lifts()),
        ("liftsets", client.lift_sets_db_id, client.is_configured_lift_sets()),
        ("runs", client.runs_db_id, client.is_configured_runs()),
    ]

    mode = "APPLY (archiving)" if args.apply else "DRY RUN (no changes)"
    print(f"=== Notion dedupe — {mode} — since {args.since} ===\n")

    total_groups = total_archived = 0
    for name, db_id, configured in targets:
        if args.db not in ("all", name):
            continue
        if not configured:
            print(f"[{name}] not configured — skipping.")
            continue
        g, a = await _dedupe_db(client, db_id, name, args.since, args.apply)
        total_groups += g
        total_archived += a
        print()

    if args.apply:
        print(f"Done. Archived {total_archived} duplicate row(s) across "
              f"{total_groups} marker group(s). Recoverable from Notion trash.")
    else:
        print(f"Dry run complete. {total_groups} marker group(s) have duplicates. "
              f"Re-run with --apply to archive the extras.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
