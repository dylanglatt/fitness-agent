"""ai/exercise_db.py — lookup and substitution over the free-exercise-db
dataset (github.com/yuhonas/free-exercise-db, public domain / Unlicense).

Vendored as data/exercise_db.json (trimmed: no instructions/images, ~875
exercises). This module is the pure-logic layer over that static file:
loading, name matching, muscle->pattern mapping, and substitution lookup.
Nothing here talks to the DB or the LLM — same pattern as training_state.py
and session_planner.py.

Why this exists: classify_exercise() in training_state.py is a ~50-entry
hand-authored keyword list. It's fast and precise for the lifts Dylan
actually logs, but it returns (None, None) for anything outside that list —
any new exercise, any variation not anticipated. free-exercise-db has ~875
exercises with muscle/equipment/category data already attached, so it's a
natural fallback: try the hand-authored list first (it encodes real
judgment calls, e.g. "close grip" -> triceps not chest), fall back to the
dataset only when that list doesn't recognize the name.

It also unlocks something the keyword list can't: substitution. "What else
can I do for hamstrings with just dumbbells" is a lookup this dataset
answers directly.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "exercise_db.json"

# free-exercise-db muscle strings -> this app's PUSH/PULL/LEGS/CORE pattern
# vocabulary (see training_state.py's PUSH/PULL/LEGS/CORE). Judgment calls:
# "lower back" -> legs (deadlifts/back-extensions are posterior-chain work
# already classified as LEGS by the keyword list); "forearms"/"traps" -> pull
# (grip and shrug work rides along with pulling); "neck" has no clear
# pattern in this app's scheme and is left unmapped.
_MUSCLE_TO_PATTERN = {
    "abdominals": "core",
    "abductors": "legs",
    "adductors": "legs",
    "biceps": "pull",
    "calves": "legs",
    "chest": "push",
    "forearms": "pull",
    "glutes": "legs",
    "hamstrings": "legs",
    "lats": "pull",
    "lower back": "legs",
    "middle back": "pull",
    "quadriceps": "legs",
    "shoulders": "push",
    "traps": "pull",
    "triceps": "push",
}

# free-exercise-db muscle strings -> this app's display vocabulary (the
# muscle labels classify_exercise() already returns, e.g. "quads" not
# "quadriceps"). Anything absent from this dict is passed through as-is.
_MUSCLE_DISPLAY = {
    "quadriceps": "quads",
    "lower back": "hamstrings",
    "middle back": "back",
    "lats": "back",
}

# Categories worth suggesting as substitutes for a lifting session. Cardio
# and stretching entries share muscle names with strength work but aren't
# useful answers to "what else can I lift for this muscle".
_STRENGTH_CATEGORIES = {
    "strength", "powerlifting", "olympic weightlifting", "strongman",
}


@lru_cache(maxsize=1)
def _load() -> list[dict]:
    """Load and cache the vendored dataset. Empty list if missing/corrupt —
    callers treat "no data" the same as "no match", never an exception."""
    try:
        with open(_DB_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def muscle_to_pattern(muscle: Optional[str]) -> Optional[str]:
    """Map a free-exercise-db muscle name to push/pull/legs/core, or None."""
    return _MUSCLE_TO_PATTERN.get((muscle or "").strip().lower())


def _display_muscle(muscle: Optional[str]) -> Optional[str]:
    m = (muscle or "").strip().lower()
    if not m:
        return None
    return _MUSCLE_DISPLAY.get(m, m)


def find_exercise(name: str) -> Optional[dict]:
    """Best-effort match of a logged exercise name against the dataset.

    Tries, in order: exact (case-insensitive) name match, then substring
    match in either direction (handles "bb bench press" containing "bench
    press", and "bench press" being contained in "Barbell Bench Press").
    Returns the first/best hit or None — never raises, never guesses wildly
    (a bare one/two-letter name won't substring-match half the dataset
    because the shorter side of the comparison must be at least 4 chars).
    """
    n = (name or "").strip().lower()
    if not n:
        return None
    db = _load()
    for e in db:
        if (e.get("name") or "").strip().lower() == n:
            return e
    if len(n) < 4:
        return None
    for e in db:
        en = (e.get("name") or "").strip().lower()
        if not en:
            continue
        if n in en or (len(en) >= 4 and en in n):
            return e
    return None


def classify_via_db(name: str) -> tuple[Optional[str], Optional[str]]:
    """(pattern, muscle) for an exercise name, using the dataset — the
    fallback classify_exercise() reaches for once its own keyword list comes
    up empty. Pattern/muscle vocabulary matches classify_exercise()'s own."""
    ex = find_exercise(name)
    if not ex:
        return None, None
    primary = (ex.get("primaryMuscles") or [None])[0]
    return muscle_to_pattern(primary), _display_muscle(primary)


def find_substitutes(
    name: str, *, equipment: Optional[list[str]] = None, limit: int = 5
) -> list[dict]:
    """Other strength exercises sharing this one's primary muscle.

    equipment, if given, filters to exercises usable with what's on hand
    (case-insensitive match against the dataset's single-equipment field,
    e.g. ["dumbbell", "body only"]); pass None for no filter. Compound
    movements are listed first (more bang-per-set, the usual reason to
    substitute at all), then isolation. Returns a list of
    {"name", "equipment", "primaryMuscles"} dicts, nearest matches first,
    excluding the exercise itself.
    """
    ex = find_exercise(name)
    if not ex:
        return []
    primary = set(ex.get("primaryMuscles") or [])
    if not primary:
        return []
    equip_filter = (
        {e.strip().lower() for e in equipment} if equipment else None
    )
    matches = []
    for cand in _load():
        if cand is ex or cand.get("id") == ex.get("id"):
            continue
        if (cand.get("category") or "").lower() not in _STRENGTH_CATEGORIES:
            continue
        if not (set(cand.get("primaryMuscles") or []) & primary):
            continue
        if equip_filter is not None:
            if (cand.get("equipment") or "").strip().lower() not in equip_filter:
                continue
        matches.append(cand)
    matches.sort(key=lambda c: 0 if c.get("mechanic") == "compound" else 1)
    return [
        {
            "name": c.get("name"),
            "equipment": c.get("equipment"),
            "primaryMuscles": c.get("primaryMuscles"),
        }
        for c in matches[:limit]
    ]
