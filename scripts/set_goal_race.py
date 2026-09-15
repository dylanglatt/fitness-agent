"""
scripts/set_goal_race.py — set (or replace) the active goal race that
ai/race_periodization.py periodizes training around.

Usage (run ON THE DROPLET — this mutates the live DB, not a local copy):
    venv/bin/python scripts/set_goal_race.py "NY Marathon" 2027-11-07 marathon --peak-mileage 45
    venv/bin/python scripts/set_goal_race.py "NY Marathon" 2027-11-07 marathon --peak-mileage 45 --dry-run

Distance must be one of: 5k, 10k, half_marathon, marathon (controls taper/
peak/build lengths — see ai/race_periodization.py). --peak-mileage is
optional but unlocks concrete weekly-mileage numbers in the brief instead of
relative guidance ("climbing toward peak" vs "climbing toward peak (45 mi/wk)").
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from data.database import Database  # noqa: E402
from ai.race_periodization import training_block, days_to_race  # noqa: E402
from datetime import date  # noqa: E402

VALID_DISTANCES = ("5k", "10k", "half_marathon", "marathon")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("name", help='Race name, e.g. "NY Marathon"')
    p.add_argument("race_date", help="YYYY-MM-DD")
    p.add_argument(
        "distance", nargs="?", default="marathon", choices=VALID_DISTANCES,
        help="Controls taper/peak/build lengths (default: marathon)",
    )
    p.add_argument("--peak-mileage", type=float, default=None, dest="peak_mileage")
    p.add_argument("--target-time", default="", dest="target_time")
    p.add_argument("--notes", default="")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    try:
        race_date = datetime.fromisoformat(args.race_date).date()
    except ValueError:
        print(f"Bad date {args.race_date!r} — use YYYY-MM-DD.", file=sys.stderr)
        sys.exit(1)

    db = Database(Config.DB_PATH)
    await db.initialize()

    current = await db.get_active_goal_race()
    if current:
        print(f"Current active goal race: {current['name']!r} on {current['race_date']} (id={current['id']})")
    else:
        print("No active goal race currently set.")

    out = days_to_race(date.today(), race_date)
    block = training_block(out, args.distance)
    print(f"\n{args.name}: {out} days out — currently {block.upper()} phase")

    if args.dry_run:
        print("--dry-run: nothing written.")
        return

    race_id = await db.save_goal_race(
        args.name,
        args.race_date,
        args.distance,
        peak_weekly_mi=args.peak_mileage,
        target_time=args.target_time,
        notes=args.notes,
        activate=True,
    )
    print(f"\nActivated goal race '{args.name}' (id={race_id}). Old goal race archived.")


if __name__ == "__main__":
    asyncio.run(main())
