"""Full-history audit: replay every Discord message since 2026-05-13 (the
day the lift_sets per-set table shipped) through the FIXED chat lift parser
(ai/coach.py, post fix), and diff the reconstruction against what's actually
in the database.

This does NOT write anything. It's read-only -- it prints a per-date report
of MATCH / MISSING / MISATTRIBUTED-suspect so we can build a final backfill
script the same way we did for 2026-09-11/18/23/25, instead of guessing.

Requires a working ANTHROPIC_API_KEY in the environment (must be run where
that's real -- i.e. the droplet, not local dev).

Run:
  venv/bin/python scripts/audit_full_history.py                 # all dates
  venv/bin/python scripts/audit_full_history.py --date 2026-06-04  # one date
"""

import argparse
import asyncio
import gzip
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from data.database import Database  # noqa: E402
import ai.coach as coach_mod  # noqa: E402

DATA_FILE = Path(__file__).resolve().parent / "_audit_data" / "dm_history_2026-05-13_to_present.json.gz"

# Every date in the window that had a lift-related message, per the earlier
# regex scan of the DM history. 2026-09-11/18/23/25 are already handled by
# their own dedicated backfill scripts -- skip them here so this doesn't
# fight those corrections.
ALREADY_HANDLED = {"2026-09-11", "2026-09-18", "2026-09-23", "2026-09-25"}


def load_messages():
    with gzip.open(DATA_FILE, "rt") as f:
        msgs = json.load(f)
    msgs.sort(key=lambda m: m["ts"])
    return msgs


NY_TZ = pytz.timezone("America/New_York")


def local_date(ts: str) -> str:
    """Discord timestamps are UTC. The app assigns each lift's `date` using
    America/New_York (config.py TIMEZONE), so grouping by raw UTC calendar
    date silently shifts anything logged 8pm-midnight Eastern into the next
    day's bucket. Convert before taking the date, or the audit corrupts
    itself right at the boundary it most needs to get right."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(NY_TZ).strftime("%Y-%m-%d")


def group_by_date(msgs):
    by_date = {}
    for m in msgs:
        d = local_date(m["ts"])
        by_date.setdefault(d, []).append(m)
    return by_date


async def replay_date(coach_self, day_msgs):
    """Feed every Dylan message for one date through the real parser in
    order, maintaining recent_context/today_summary/last_assistant_message
    the same way chat() does. Returns the reconstructed set list."""
    reconstructed = []  # list of dicts: exercise, set_number, reps, weight_lb
    last_assistant_message = None
    per_exercise_counts = {}

    for m in day_msgs:
        if m["bot"]:
            last_assistant_message = m["content"]
            continue

        message = m["content"]
        recent_context = [
            {
                "exercise": r["exercise"],
                "set_number": r["set_number"],
                "reps": r["reps"],
                "weight_lb": r["weight_lb"],
            }
            for r in reconstructed[-2:][::-1]
        ]
        today_summary = []
        for ex, cnt in per_exercise_counts.items():
            last = [r for r in reconstructed if r["exercise"] == ex][-1]
            today_summary.append({
                "exercise": ex,
                "sets_done": cnt,
                "last_weight_lb": last["weight_lb"],
                "last_reps": last["reps"],
            })

        try:
            parsed = await coach_mod.Coach._try_parse_lift(
                coach_self,
                message,
                recent_context=recent_context,
                today_summary=today_summary,
                last_assistant_message=last_assistant_message,
            )
        except Exception as e:
            print(f"    [parse error on {message!r}: {e}]", file=sys.stderr)
            parsed = None

        if parsed:
            ex = parsed["exercise"]
            set_num = parsed.get("set_number")
            if set_num is None:
                set_num = per_exercise_counts.get(ex, 0) + 1
            per_exercise_counts[ex] = max(per_exercise_counts.get(ex, 0), set_num)
            reconstructed.append({
                "exercise": ex,
                "set_number": set_num,
                "reps": parsed.get("reps"),
                "weight_lb": parsed.get("weight_lb"),
                "raw_message": message,
            })

    return reconstructed


def diff_report(date, reconstructed, db_rows):
    print(f"\n{'=' * 70}\n{date}\n{'=' * 70}")
    print(f"Reconstructed (should exist): {len(reconstructed)} sets")
    print(f"In DB now:                    {len(db_rows)} sets")

    db_by_key = {}
    for r in db_rows:
        db_by_key.setdefault(r.get("exercise"), []).append(r)

    recon_by_key = {}
    for r in reconstructed:
        recon_by_key.setdefault(r["exercise"], []).append(r)

    all_exercises = set(db_by_key) | set(recon_by_key)
    any_diff = False
    for ex in sorted(all_exercises):
        db_sets = sorted(db_by_key.get(ex, []), key=lambda r: r.get("set_number") or 0)
        recon_sets = sorted(recon_by_key.get(ex, []), key=lambda r: r["set_number"] or 0)
        if len(db_sets) == len(recon_sets) and all(
            (d.get("reps"), d.get("weight_lb")) == (r["reps"], r["weight_lb"])
            for d, r in zip(db_sets, recon_sets)
        ):
            continue
        any_diff = True
        print(f"\n  {ex}:")
        print(f"    DB:            {[(d.get('set_number'), d.get('reps'), d.get('weight_lb')) for d in db_sets]}")
        print(f"    Reconstructed: {[(r['set_number'], r['reps'], r['weight_lb']) for r in recon_sets]}")

    if not any_diff:
        print("\n  No differences -- this date looks clean.")
    return any_diff


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Audit a single date (YYYY-MM-DD) only")
    args = parser.parse_args()

    if not Config.ANTHROPIC_API_KEY:
        print("ERROR: no ANTHROPIC_API_KEY in this environment -- run this "
              "on the droplet, not local dev.", file=sys.stderr)
        sys.exit(1)

    import anthropic
    coach_self = SimpleNamespace(
        claude=anthropic.AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY),
        cheap_model="claude-haiku-4-5-20251001",
        db=None,
    )

    db = Database(Config.DB_PATH)
    await db.initialize()

    msgs = load_messages()
    by_date = group_by_date(msgs)
    dates = sorted(d for d in by_date if d not in ALREADY_HANDLED)
    if args.date:
        dates = [args.date] if args.date in by_date else []

    flagged_dates = []
    for date in dates:
        reconstructed = await replay_date(coach_self, by_date[date])
        if not reconstructed:
            continue  # nothing lift-like this date per the fixed parser
        db_rows = await db.get_lift_sets_for_date(date)
        has_diff = diff_report(date, reconstructed, db_rows)
        if has_diff:
            flagged_dates.append(date)

    print(f"\n\n{'#' * 70}")
    print(f"SUMMARY: {len(flagged_dates)} date(s) with differences out of {len(dates)} checked")
    print(flagged_dates)


if __name__ == "__main__":
    asyncio.run(main())
