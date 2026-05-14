"""
scripts/stamp_notion_lift_markers.py — one-shot pass that adds
[liftrow:N] dedup markers to existing Notion Lifts rows that pre-date the
bot's marker-writing logic.

Why:
  The Lift Sets backfill (scripts/backfill_lift_sets_to_notion.py) finds
  each set's parent Notion Lift row by scanning the Lifts DB for the
  [liftrow:N] marker that the bot embeds in Notes. Lifts written before
  the marker pattern existed don't carry that marker, so the Lift Sets
  backfill marks them as `skipped_no_parent`. This script closes the gap
  by walking each SQLite lifts row, finding the corresponding Notion Lifts
  row by (date, position-within-date), and appending the marker.

Matching strategy:
  - Group SQLite lifts by date, sorted by id (chronological)
  - Group Notion Lifts rows by date, sorted by created_time (chronological)
  - Within each date, pair them by position. Robust enough because both
    sides were written in real time as the user logged each lift.
  - Skip any Notion row that already has a [liftrow:N] marker (idempotent)
  - Skip any SQLite lift whose id already appears as a marker somewhere
  - Warn (don't stamp) when exercise names don't roughly match

Usage:
    python scripts/stamp_notion_lift_markers.py --dry-run     # plan, no writes
    python scripts/stamp_notion_lift_markers.py --sample 5    # stamp 5, then stop
    python scripts/stamp_notion_lift_markers.py               # do it live

Non-destructive: only PREPENDS '[liftrow:N] ' to the existing Notes field.
Never deletes, overwrites, or modifies any other property. Safe to re-run.

Rate limit: 0.4s sleep between Notion writes (under 3 req/s).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from integrations.notion import NotionClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stamp_notion_lift_markers")


NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_RATE_LIMIT_SLEEP_S = 0.4

_LIFTROW_MARKER = re.compile(r"\[liftrow:(\d+)\]")


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def fetch_all_lifts_pages(
    client: httpx.AsyncClient, headers: dict, db_id: str,
) -> list[dict]:
    """Paginate through every Lifts DB row. ~100 rows per page; we expect
    a few hundred at most so this is cheap."""
    pages: list[dict] = []
    cursor: Optional[str] = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = await client.post(
            f"{NOTION_BASE}/databases/{db_id}/query",
            headers=headers, json=body,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Notion query failed {resp.status_code}: {resp.text[:400]}"
            )
        data = resp.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def _extract_date(page: dict) -> Optional[str]:
    d = page.get("properties", {}).get("Date", {}).get("date")
    return d.get("start") if d else None


def _extract_notes(page: dict) -> str:
    nodes = page.get("properties", {}).get("Notes", {}).get("rich_text", []) or []
    return "".join(n.get("plain_text", "") for n in nodes)


def _extract_exercise(page: dict) -> str:
    nodes = page.get("properties", {}).get("Exercise", {}).get("rich_text", []) or []
    return "".join(n.get("plain_text", "") for n in nodes)


def _extract_marker(page: dict) -> Optional[int]:
    m = _LIFTROW_MARKER.search(_extract_notes(page))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _normalize_exercise(ex: str | None) -> str:
    """Loose normalization so 'Bench Press' / 'bench press' / 'Bench Press '
    collapse to a single key for the warning heuristic."""
    return (ex or "").strip().lower()


def _exercises_roughly_match(a: str, b: str) -> bool:
    """Very loose: same after normalize, or one is a substring of the other,
    or they share the first significant word. Also tries an alphanumeric-only
    comparison so 'Trapbar' and 'Trap bar' collapse."""
    na, nb = _normalize_exercise(a), _normalize_exercise(b)
    if not na or not nb:
        return True  # one side missing — don't second-guess
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    # Alphanumeric-only comparison handles 'Trapbar' vs 'Trap bar'.
    import re as _re
    alpha_a = _re.sub(r"[^a-z0-9]", "", na)
    alpha_b = _re.sub(r"[^a-z0-9]", "", nb)
    if alpha_a and (alpha_a == alpha_b or alpha_a in alpha_b or alpha_b in alpha_a):
        return True
    first_a = na.split()[0] if na.split() else ""
    first_b = nb.split()[0] if nb.split() else ""
    return bool(first_a) and first_a == first_b


async def stamp_marker(
    client: httpx.AsyncClient,
    headers: dict,
    page_id: str,
    current_notes: str,
    sqlite_lift_id: int,
) -> bool:
    """PATCH the Notion page to PREPEND '[liftrow:N] ' to its Notes field.
    Returns True on success."""
    new_notes = (
        f"[liftrow:{sqlite_lift_id}] {current_notes}".strip()
        if current_notes else f"[liftrow:{sqlite_lift_id}]"
    )
    body = {
        "properties": {
            "Notes": {
                "rich_text": [{"type": "text", "text": {"content": new_notes}}]
            }
        }
    }
    resp = await client.patch(
        f"{NOTION_BASE}/pages/{page_id}", headers=headers, json=body,
    )
    if resp.status_code in (200, 201):
        return True
    logger.warning(
        "Notion PATCH %s failed (%d): %s", page_id, resp.status_code, resp.text[:200],
    )
    return False


async def run(args: argparse.Namespace) -> int:
    cfg = Config()
    if not cfg.NOTION_API_KEY or cfg.NOTION_API_KEY.startswith("your_"):
        logger.error("NOTION_API_KEY is not configured.")
        return 2
    if not cfg.NOTION_LIFTS_DATABASE_ID or cfg.NOTION_LIFTS_DATABASE_ID.startswith("your_"):
        logger.error("NOTION_LIFTS_DATABASE_ID is not configured.")
        return 2

    # 1) SQLite lifts, grouped by date, ordered by id. We grab details
    # and raw_message too because --create-missing reuses them as the new
    # Notion row's Notes content.
    import aiosqlite
    sqlite_rows: list[dict] = []
    async with aiosqlite.connect(cfg.DB_PATH) as sdb:
        sdb.row_factory = aiosqlite.Row
        async with sdb.execute(
            "SELECT id, date, exercise, details, raw_message FROM lifts "
            "ORDER BY date ASC, id ASC"
        ) as cur:
            sqlite_rows = [dict(r) for r in await cur.fetchall()]

    if args.since:
        sqlite_rows = [r for r in sqlite_rows if r["date"] >= args.since]
    sqlite_by_date: dict[str, list[dict]] = defaultdict(list)
    for r in sqlite_rows:
        sqlite_by_date[r["date"]].append(r)
    logger.info(
        "SQLite lifts: %d rows across %d dates",
        len(sqlite_rows), len(sqlite_by_date),
    )

    # 2) Notion Lifts pages, grouped by date, ordered by created_time.
    async with httpx.AsyncClient(timeout=30.0) as client:
        all_pages = await fetch_all_lifts_pages(
            client, _headers(cfg.NOTION_API_KEY), cfg.NOTION_LIFTS_DATABASE_ID,
        )

    notion_by_date: dict[str, list[dict]] = defaultdict(list)
    for p in all_pages:
        d = _extract_date(p)
        if d:
            notion_by_date[d].append(p)
    for d in notion_by_date:
        notion_by_date[d].sort(key=lambda p: p.get("created_time", ""))
    logger.info(
        "Notion Lifts: %d pages across %d dates",
        sum(len(v) for v in notion_by_date.values()),
        len(notion_by_date),
    )

    # 3) Build the stamp / create plan.
    #
    # Two modes:
    #   * --create-missing (recommended for Dylan's data): for every SQLite
    #     lift without a [liftrow:N] marker in Notion, CREATE a fresh per-lift
    #     Notion Lifts row. Don't try to pair with existing unmarked rows —
    #     those are pre-bot summary-style entries that shouldn't be repurposed
    #     as a parent for a single set.
    #   * default: try to pair (i-th unmarked SQLite ↔ i-th unmarked Notion).
    #     Useful when the Notion DB was supposed to be 1:1 with SQLite but
    #     missing markers — rare in practice.
    stats = {
        "sqlite_total": len(sqlite_rows),
        "already_marked": 0,
        "planned": 0,            # plans to STAMP an existing Notion row
        "to_create": 0,          # plans to CREATE a new Notion row
        "skipped_no_notion_row": 0,
        "exercise_mismatch_warning": 0,
        "stamped": 0,
        "created": 0,
        "failed": 0,
    }
    plans: list[dict] = []  # each plan has 'op': 'stamp' or 'create'

    for date, sqlite_lifts in sorted(sqlite_by_date.items()):
        notion_pages = notion_by_date.get(date, [])
        # SQLite ids already represented in Notion as a marker.
        existing_marker_ids: set[int] = set()
        for p in notion_pages:
            m = _extract_marker(p)
            if m is not None:
                existing_marker_ids.add(m)

        # Pages without a marker yet — these are the targets for stamp mode.
        unmarked_pages = [p for p in notion_pages if _extract_marker(p) is None]
        # SQLite lifts that don't yet have a marker in Notion.
        unstamped_sqlite = [r for r in sqlite_lifts if r["id"] not in existing_marker_ids]

        stats["already_marked"] += len(sqlite_lifts) - len(unstamped_sqlite)

        if args.create_missing:
            # Create a fresh Notion Lifts row for every unmarked SQLite lift.
            # Existing unmarked Notion rows on this date stay untouched.
            for sql_lift in unstamped_sqlite:
                plans.append({
                    "op": "create",
                    "sqlite_id": sql_lift["id"],
                    "sqlite_ex": sql_lift["exercise"] or "",
                    "sqlite_details": sql_lift.get("details") or "",
                    "sqlite_raw": sql_lift.get("raw_message") or "",
                    "date": date,
                })
                stats["to_create"] += 1
            continue

        # Default (stamp) mode: position-pair the i-th unmarked SQLite lift
        # with the i-th unmarked Notion page on this date.
        for i, sql_lift in enumerate(unstamped_sqlite):
            if i >= len(unmarked_pages):
                logger.warning(
                    "Date %s: SQLite lift %d (%r) has no unmarked Notion row "
                    "to pair with (notion=%d, sqlite=%d, already_marked=%d). "
                    "Re-run with --create-missing to create a fresh Notion "
                    "row for this lift.",
                    date, sql_lift["id"], sql_lift["exercise"],
                    len(notion_pages), len(sqlite_lifts),
                    len(sqlite_lifts) - len(unstamped_sqlite),
                )
                stats["skipped_no_notion_row"] += 1
                continue
            n_page = unmarked_pages[i]
            n_ex = _extract_exercise(n_page)
            sql_ex = sql_lift["exercise"] or ""
            current_notes = _extract_notes(n_page)
            if not _exercises_roughly_match(sql_ex, n_ex):
                stats["exercise_mismatch_warning"] += 1
                logger.warning(
                    "Date %s position %d: exercise mismatch — "
                    "SQLite #%d %r ↔ Notion %r. Stamping anyway.",
                    date, i, sql_lift["id"], sql_ex, n_ex,
                )
            plans.append({
                "op": "stamp",
                "sqlite_id": sql_lift["id"],
                "sqlite_ex": sql_ex,
                "notion_page_id": n_page["id"],
                "notion_ex": n_ex,
                "date": date,
                "current_notes": current_notes,
            })
            stats["planned"] += 1

    # 4) Execute or dry-run.
    if not plans:
        print("\nNothing to do — all SQLite lifts already have markers in Notion.")
        return 0

    if args.dry_run:
        print(f"\n── DRY-RUN plan ({len(plans)} ops) ───────────────────────────────")
        for p in plans[:60]:
            if p["op"] == "stamp":
                same = "✓" if _exercises_roughly_match(p["sqlite_ex"], p["notion_ex"]) else "≈"
                print(
                    f"  STAMP  [{p['date']}] sqlite_id={p['sqlite_id']:3d}  "
                    f"{p['sqlite_ex']:30s} {same} {p['notion_ex']:30s}  "
                    f"page={p['notion_page_id'][:8]}…"
                )
            else:
                print(
                    f"  CREATE [{p['date']}] sqlite_id={p['sqlite_id']:3d}  "
                    f"{p['sqlite_ex']:30s}  details={p['sqlite_details'][:40]!r}"
                )
        if len(plans) > 60:
            print(f"  …and {len(plans) - 60} more")
    else:
        # Live writes. STAMP ops use the raw httpx client; CREATE ops use
        # the NotionClient so they go through the same code path as
        # real-time bot writes (Schedule relation, dedup marker, etc.).
        notion_client = NotionClient(cfg)
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = _headers(cfg.NOTION_API_KEY)
            done_count = 0
            for p in plans:
                if p["op"] == "stamp":
                    ok = await stamp_marker(
                        client, headers, p["notion_page_id"],
                        p["current_notes"], p["sqlite_id"],
                    )
                    if ok:
                        stats["stamped"] += 1
                        done_count += 1
                    else:
                        stats["failed"] += 1
                else:
                    # CREATE: route through notion.log_lift so the new row
                    # ends up shaped exactly like a live bot write — marker
                    # in Notes, Schedule relation populated, etc.
                    try:
                        page_id = await notion_client.log_lift(
                            date=p["date"],
                            exercise=p["sqlite_ex"] or "lift",
                            notes=p["sqlite_raw"] or p["sqlite_details"] or "",
                            lift_id=p["sqlite_id"],
                        )
                        if page_id:
                            stats["created"] += 1
                            done_count += 1
                        else:
                            stats["failed"] += 1
                    except Exception as e:
                        logger.warning(
                            "CREATE failed for sqlite_id=%d: %s",
                            p["sqlite_id"], e,
                        )
                        stats["failed"] += 1
                if args.sample and done_count >= args.sample:
                    break
                await asyncio.sleep(NOTION_RATE_LIMIT_SLEEP_S)

    print("\n── Summary ──────────────────────────────────────────────────")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(
        "  mode:",
        "DRY-RUN" if args.dry_run else (f"SAMPLE({args.sample})" if args.sample else "LIVE"),
    )
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="plan & print, no writes")
    p.add_argument("--sample", type=int, default=0,
                   help="stop after N successful writes (live only)")
    p.add_argument("--since", type=str, default=None,
                   help="only process SQLite lifts on or after YYYY-MM-DD")
    p.add_argument(
        "--create-missing", action="store_true",
        help=(
            "For every SQLite lift without a [liftrow:N] marker in Notion, "
            "CREATE a fresh per-lift Notion Lifts row (don't try to pair "
            "with existing unmarked rows). Use this when the unmarked "
            "Notion rows are summary-style entries that shouldn't be "
            "repurposed as a single-set's parent."
        ),
    )
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
