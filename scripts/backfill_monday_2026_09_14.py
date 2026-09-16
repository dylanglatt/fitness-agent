"""One-off: backfill Monday 2026-09-14's lift session that wasn't logged
at the time. Numbers confirmed directly with Dylan (chat, 2026-09-16).
Biceps omitted — no sets/reps/weight remembered, so nothing was invented.

Run once on the droplet: venv/bin/python scripts/backfill_monday_2026_09_14.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from data.database import Database  # noqa: E402
from integrations.notion import NotionClient  # noqa: E402

DATE = "2026-09-14"  # Monday

EXERCISES = [
    {
        "exercise": "Sissy hack squat",
        "sets": [
            {"set_number": 1, "reps": 8, "weight_lb": 45},
            {"set_number": 2, "reps": 8, "weight_lb": 45},
            {"set_number": 3, "reps": 8, "weight_lb": 45},
        ],
        "note": "45 lb per side of machine.",
    },
    {
        "exercise": "Single-leg RDL",
        "sets": [
            {"set_number": 1, "reps": 10, "weight_lb": 25},
            {"set_number": 2, "reps": 10, "weight_lb": 25},
            {"set_number": 3, "reps": 10, "weight_lb": 25},
        ],
        "note": "25 lb dumbbell in each hand, 10 reps each side per set.",
    },
    {
        "exercise": "Seated row",
        "sets": [
            {"set_number": 1, "reps": 10, "weight_lb": 120},
            {"set_number": 2, "reps": 10, "weight_lb": 140},
            {"set_number": 3, "reps": 10, "weight_lb": 140},
        ],
        "note": "",
    },
    {
        "exercise": "Bench press",
        "sets": [
            {"set_number": 1, "reps": 12, "weight_lb": 135, "note": "warmup"},
            {"set_number": 2, "reps": 6, "weight_lb": 205},
            {"set_number": 3, "reps": 6, "weight_lb": 205},
            {"set_number": 4, "reps": 6, "weight_lb": 205},
        ],
        "note": "Set 1 warmup; sets 2-4 are the working sets.",
    },
    {
        "exercise": "Face pulls",
        "sets": [
            {"set_number": 1, "reps": 10, "weight_lb": 45},
            {"set_number": 2, "reps": 10, "weight_lb": 45},
            {"set_number": 3, "reps": 10, "weight_lb": 45},
        ],
        "note": "",
    },
]

RAW = (
    "Manual backfill (forgot to log Monday, entered 2026-09-16): "
    + ", ".join(e["exercise"] for e in EXERCISES)
    + ". Biceps also done but numbers not remembered — omitted."
)


async def main() -> None:
    db = Database(Config.DB_PATH)
    await db.initialize()
    notion = NotionClient(Config)

    for ex in EXERCISES:
        sets = ex["sets"]
        details = "; ".join(
            f"set {s['set_number']}: {s['reps']}x{s['weight_lb']}lb"
            + (f" ({s['note']})" if s.get("note") else "")
            for s in sets
        )

        lift_id = await db.log_lift(
            date=DATE, exercise=ex["exercise"], details=details, raw=RAW,
        )
        print(f"[{ex['exercise']}] SQLite lifts row id={lift_id}: {details}")

        set_records = []
        for s in sets:
            set_db_id = await db.log_lift_set(
                lift_id=lift_id,
                date=DATE,
                exercise=ex["exercise"],
                set_number=s["set_number"],
                reps=s["reps"],
                weight_lb=s["weight_lb"],
                notes=s.get("note", ""),
                source="backfill",
            )
            set_records.append({**s, "set_db_id": set_db_id})
        print(f"  -> logged {len(set_records)} lift_sets rows")

        # Notion Lifts summary row: non-uniform sessions (bench, row) use the
        # heaviest/working set as the representative reps+weight — the real
        # per-set numbers live in Lift Sets, which is what progression reads.
        top = sets[-1]
        parent_lift_notion_id = None
        try:
            parent_lift_notion_id = await notion.log_lift(
                date=DATE,
                exercise=ex["exercise"],
                sets=len(sets),
                reps=top["reps"],
                weight_lb=top["weight_lb"],
                notes=f"{ex['note']} {details}".strip(),
                lift_id=lift_id,
            )
            print(f"  -> Notion Lift page: {parent_lift_notion_id}")
        except Exception as e:
            print(f"  -> Notion Lift write failed ({type(e).__name__}: {e}) "
                  f"— SQLite row is still committed.")

        if notion.is_configured_lift_sets():
            for rec in set_records:
                try:
                    await notion.log_lift_set(
                        date=DATE,
                        exercise=ex["exercise"],
                        set_number=rec["set_number"],
                        reps=rec["reps"],
                        weight_lb=rec["weight_lb"],
                        notes=rec.get("note", ""),
                        source="backfill",
                        parent_lift_page_id=parent_lift_notion_id,
                        lift_set_id=rec["set_db_id"],
                    )
                except Exception as e:
                    print(f"  -> Notion Lift Set write failed for set "
                          f"{rec['set_number']} ({type(e).__name__}: {e})")

    print("\nDone. Biceps not logged (no numbers given).")


if __name__ == "__main__":
    asyncio.run(main())
