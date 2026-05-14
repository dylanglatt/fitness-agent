"""
scripts/backfill_lift_sets.py — one-shot reparse of the historical lifts
table into structured per-set rows in lift_sets.

Why:
  Before lift_sets existed, lifts.details was a text field ('4x6 @ 205',
  'set 1/4 · 205 lb × 6', etc.). That works for the recap but is useless
  for SQL questions like "what was my top set on bench at 6 reps over the
  last 8 weeks". This script reads every existing lifts row and writes the
  corresponding structured set rows.

Strategy:
  - /liftstart rows (raw_message starts with '[liftstart]') have a
    deterministic details format: 'set N/M · W lb × R'. Regex-parse those.
    Source = 'liftstart'.
  - Chat-path rows have free-form details. Send raw_message to Haiku and
    ask for a JSON list of sets (handles non-uniform sets like
    '205x6, 195x5, 90 per side x 6'). Source = 'backfill'.
  - Idempotent: skip any lift row that already has lift_sets entries.

Usage:
    python scripts/backfill_lift_sets.py --dry-run    # parse and print, no writes
    python scripts/backfill_lift_sets.py --sample 5   # show 5 sample parses then exit
    python scripts/backfill_lift_sets.py              # do it live
    python scripts/backfill_lift_sets.py --since 2026-01-01  # limit date range

Cost: ~$0.0001/row on Haiku × N chat-path rows. <$0.50 total for most users.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

# Allow running from repo root: `python scripts/backfill_lift_sets.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import AsyncAnthropic

from config import Config
from data.database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_lift_sets")


# ── Deterministic parser for /liftstart rows ────────────────────────────────
#
# /liftstart writes details strings of the form:
#   'set 1/4 · 205 lb × 6'        (full)
#   'set 1/4 · bw lb × 6'         (bodyweight)
#   'set 1/4 · 205 lb × ?'        (weight known, reps unknown — rare)
#
# We don't try to recover failure / RPE here; that data isn't in details.

_LIFTSTART_DETAILS = re.compile(
    r"set\s+(?P<set>\d+)\s*/\s*(?P<total>\d+)\s*[·\-]\s*"
    r"(?P<weight>[\d.]+|bw|bodyweight)\s*lb\s*[×x]\s*(?P<reps>\d+|\?)",
    re.IGNORECASE,
)


def parse_liftstart_details(details: str) -> Optional[dict]:
    """Parse a deterministic /liftstart details string into one set dict.

    Returns None if the string doesn't match the expected format.
    """
    m = _LIFTSTART_DETAILS.search(details or "")
    if not m:
        return None
    w_raw = m.group("weight").lower()
    weight_lb: Optional[float]
    if w_raw in ("bw", "bodyweight"):
        weight_lb = None
    else:
        try:
            weight_lb = float(w_raw)
        except ValueError:
            weight_lb = None
    r_raw = m.group("reps")
    reps = int(r_raw) if r_raw.isdigit() else None
    return {
        "set_number": int(m.group("set")),
        "reps": reps,
        "weight_lb": weight_lb,
    }


# ── Chat-path parser (Haiku) ────────────────────────────────────────────────

_PARSE_PROMPT_TEMPLATE = """
You're parsing a workout-log message into a structured list of sets.

Message: "{message}"
Already-extracted exercise: "{exercise}"
Already-extracted details summary: "{details}"

Return JSON ONLY with this schema:
{{
  "sets": [
    {{
      "set_number": <int, 1-indexed>,
      "reps": <int or null>,
      "weight_lb": <number or null — pounds; convert from kg if message says kg>,
      "equipment": "Barbell" | "Dumbbell" | "Machine" | "Cable" | "Bodyweight" | "Trap bar" | null,
      "to_failure": <true or false>,
      "rpe": <number 1-10 or null>,
      "notes": "<short string, e.g. 'per side', 'drop set', or empty>"
    }}
  ]
}}

Rules:
  - One element per SET performed. "4x6 @ 205" → 4 elements, each 6 reps @ 205 lb.
  - "205x6, 195x5, 90 per side x 6" → 3 elements with the actual reps/weight per set.
    "per side" means it's a barbell or dumbbell load per side; record the per-side
    number in weight_lb and put "per side" in notes.
  - "(failure)" or "to failure" or "went to failure" → to_failure: true on that set.
  - If a number is genuinely unknown, use null — don't guess.
  - For dumbbells, weight_lb is the per-dumbbell load ("60 lb DB" → weight_lb: 60).
  - Default equipment heuristic: bench/squat/deadlift/OHP/row → Barbell unless
    "DB"/"dumbbell" appears; lateral raise/curl unspecified → null.
  - If the message doesn't actually describe completed sets (it's a plan, a
    question, or a non-lift), return {{"sets": []}}.

Return ONLY the JSON object, no commentary.
""".strip()


async def parse_chat_message_with_haiku(
    client: AsyncAnthropic,
    model: str,
    exercise: str,
    details: str,
    raw_message: str,
) -> list[dict]:
    """Ask Haiku to expand a chat-path lift entry into per-set rows."""
    prompt = _PARSE_PROMPT_TEMPLATE.format(
        message=(raw_message or details or "").replace('"', "'")[:1000],
        exercise=exercise,
        details=details,
    )
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        start = raw.find("{")
        end = raw.rfind("}") + 1
        data = json.loads(raw[start:end])
        sets = data.get("sets") or []
        cleaned: list[dict] = []
        for i, s in enumerate(sets, start=1):
            if not isinstance(s, dict):
                continue
            cleaned.append({
                "set_number": int(s.get("set_number") or i),
                "reps": _safe_int(s.get("reps")),
                "weight_lb": _safe_float(s.get("weight_lb")),
                "equipment": s.get("equipment") if s.get("equipment") in {
                    "Barbell", "Dumbbell", "Machine", "Cable",
                    "Bodyweight", "Trap bar",
                } else None,
                "to_failure": bool(s.get("to_failure")),
                "rpe": _safe_float(s.get("rpe")),
                "notes": (s.get("notes") or "")[:200],
            })
        return cleaned
    except Exception as e:
        logger.warning("Haiku parse failed on lift '%s': %s", exercise, e)
        return []


def _safe_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Main ────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> int:
    cfg = Config()
    db = Database(cfg.DB_PATH)
    await db.initialize()  # ensures lift_sets exists

    client = AsyncAnthropic(api_key=cfg.ANTHROPIC_API_KEY)
    chat_model = getattr(cfg, "CHAT_MODEL", "claude-haiku-4-5-20251001")

    rows = await db.iter_lifts_for_backfill()
    if args.since:
        rows = [r for r in rows if r["date"] >= args.since]

    logger.info("Candidate lift rows: %d (since=%s)", len(rows), args.since or "all")

    stats = {
        "scanned": 0,
        "skipped_existing": 0,
        "liftstart_parsed": 0,
        "chat_parsed": 0,
        "unparsable": 0,
        "sets_written": 0,
    }
    samples: list[dict] = []

    for row in rows:
        stats["scanned"] += 1
        lift_id = row["id"]
        if await db.lift_sets_exist_for(lift_id):
            stats["skipped_existing"] += 1
            continue

        raw = row.get("raw_message") or ""
        details = row.get("details") or ""
        exercise = row.get("exercise") or "Unknown"
        date = row["date"]

        # ── Deterministic /liftstart path ────────────────────────────────
        if raw.startswith("[liftstart]"):
            parsed = parse_liftstart_details(details)
            if parsed:
                stats["liftstart_parsed"] += 1
                sets_to_write = [{
                    **parsed,
                    "equipment": None,
                    "to_failure": False,
                    "rpe": None,
                    "notes": "",
                    "_source": "liftstart",
                }]
            else:
                stats["unparsable"] += 1
                logger.warning(
                    "liftstart row %d (%s) details didn't match regex: %r",
                    lift_id, exercise, details[:120],
                )
                sets_to_write = []
        else:
            # ── Chat-path: ask Haiku ─────────────────────────────────────
            sets_to_write = await parse_chat_message_with_haiku(
                client, chat_model, exercise, details, raw,
            )
            if sets_to_write:
                stats["chat_parsed"] += 1
                for s in sets_to_write:
                    s["_source"] = "backfill"
            else:
                stats["unparsable"] += 1
                logger.warning(
                    "chat-path row %d (%s, %s) unparsable. raw=%r details=%r",
                    lift_id, date, exercise, raw[:120], details[:120],
                )

        # ── Persist or sample-print ─────────────────────────────────────
        for s in sets_to_write:
            stats["sets_written"] += 1
            if args.dry_run or (args.sample and len(samples) < args.sample):
                samples.append({
                    "lift_id": lift_id,
                    "date": date,
                    "exercise": exercise,
                    **s,
                })
                if args.sample and len(samples) >= args.sample:
                    break
            else:
                await db.log_lift_set(
                    lift_id=lift_id,
                    date=date,
                    exercise=exercise,
                    set_number=s["set_number"],
                    reps=s["reps"],
                    weight_lb=s["weight_lb"],
                    equipment=s["equipment"],
                    to_failure=s["to_failure"],
                    rpe=s["rpe"],
                    notes=s["notes"],
                    source=s["_source"],
                )

        if args.sample and len(samples) >= args.sample:
            break

    # ── Report ──────────────────────────────────────────────────────────
    print("\n── Backfill summary ─────────────────────────────────────────")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(
        "  mode:",
        "DRY-RUN" if args.dry_run else (f"SAMPLE({args.sample})" if args.sample else "LIVE"),
    )
    if samples:
        print("\n── Sample parses (first %d) ─────────────────────────────────" % len(samples))
        for s in samples:
            print(
                f"  [{s['date']}] lift_id={s['lift_id']} {s['exercise']:25s} "
                f"set {s['set_number']}: "
                f"{s['weight_lb']}lb × {s['reps']} reps  "
                f"[{s.get('equipment') or '-'}, fail={s['to_failure']}, "
                f"rpe={s['rpe']}, src={s['_source']}, notes={s['notes'] or '-'}]"
            )
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="parse & print, no writes")
    p.add_argument("--sample", type=int, default=0,
                   help="parse only this many rows and print, no writes")
    p.add_argument("--since", type=str, default=None,
                   help="only process rows on or after YYYY-MM-DD")
    args = p.parse_args()
    if args.sample and not args.dry_run:
        # --sample implies dry-run-on-the-first-N for spot checking
        args.dry_run = True
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
