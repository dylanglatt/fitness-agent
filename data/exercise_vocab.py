"""
Canonical exercise names.

Two problems this solves, both observed in the live data (408 logged sets
carrying 84 distinct exercise names for roughly 35 real movements):

1. FRAGMENTATION corrupts the progression math. "barbell row" (34 sets) and
   "barbell rows" (9) were counted as two different lifts, as were "trap bar
   deadlift" (31) and "trapbar deadlift" (4), and five spellings of the rear
   delt fly. Split a lift in half and its e1RM history, PR detection and trend
   all silently understate.

2. UNRESOLVED PLACEHOLDERS pollute the vocabulary. When the chat parser could
   not identify a movement it wrote the equipment instead — "dumbbell
   exercise" (7 sets), "cable machine exercise" (5), and one memorable
   "dumbbell exercise (likely dumbbell bench press or similar)". Those are
   parse failures wearing an exercise's clothes. They must never be offered
   back to the coach as something Dylan trains.

Deliberately conservative: it merges spellings, plurals and parentheticals,
never genuinely different movements. "single arm dumbbell row" stays distinct
from "barbell row" because the load is not comparable, which is the whole
point of tracking e1RM per lift.
"""

from __future__ import annotations

import re

# Placeholder names the parser emits when it cannot identify the movement.
# These are parse failures, not exercises — surfacing them would teach the
# coach that "dumbbell exercise" is something Dylan does.
UNRESOLVED = {
    "dumbbell exercise",
    "cable machine exercise",
    "cable machine",
    "machine exercise",
    "medicine ball",
    "medicine ball exercise",
    "dumbbell single-leg exercise",
    "triceps",
    "cable tricep",
    "exercise",
}

# Applied to the END of the name only, longest suffix first, so "flyes" is
# tried before "es" and "lat pulldowns" does not become "lat pulldown s".
_PLURALS = [
    ("flyes", "fly"),
    ("flies", "fly"),
    ("crunches", "crunch"),
    ("presses", "press"),
    ("pushdowns", "pushdown"),
    ("pulldowns", "pulldown"),
    ("extensions", "extension"),
    ("raises", "raise"),
    ("curls", "curl"),
    ("twists", "twist"),
    ("rows", "row"),
    ("ups", "up"),
]

# Explicit merges for spellings and synonyms the mechanical rules cannot reach.
# Keys are already lowercased/whitespace-collapsed/depluralized.
ALIASES = {
    "trapbar deadlift": "trap bar deadlift",
    "seated row": "seated cable row",
    "cable row": "seated cable row",
    "rope pushdown": "rope tricep pushdown",
    "rope cable tricep pushdown": "rope tricep pushdown",
    "tricep pushdown": "rope tricep pushdown",
    "triceps pushdown": "rope tricep pushdown",
    "reverse fly": "rear delt fly",
    "rear delt flie": "rear delt fly",
    "chest supported rear delt fly": "rear delt fly",
    "chest supported reverse delt fly": "rear delt fly",
    "alternating hammer curl": "hammer curl",
    "dumbbell hammer curl": "hammer curl",
    "dumbbell curl": "dumbbell bicep curl",
    "flat dumbbell fly": "dumbbell fly",
    "dumbbell flye": "dumbbell fly",
    "dumbbell incline press": "incline dumbbell press",
    "push up": "push-up",
    "pull-up": "pull-up",
    "pull up": "pull-up",
    "dip": "dips",
    "overhead press (ohp)": "overhead press",
    "lat pulldown": "lat pulldown",
    # The crunch superset was written four different ways.
    "reverse crunch and full body crunch superset": "reverse crunch superset",
    "reverse crunch / full body crunch superset": "reverse crunch superset",
    "reverse crunch superset with full body crunch": "reverse crunch superset",
    "superset reverse crunch full body crunch": "reverse crunch superset",
}

_PARENS = re.compile(r"\s*\([^)]*\)")
_WS = re.compile(r"\s+")


def canonical_exercise(name: str | None) -> str | None:
    """Canonical name for an exercise, or None if it is a parse placeholder.

    None means "we do not know what this was" and the caller should exclude it
    rather than treat it as a movement.
    """
    n = (name or "").strip().lower()
    if not n:
        return None
    n = _PARENS.sub("", n)  # "overhead press (ohp)" -> "overhead press"
    n = n.replace("’", "'")
    n = _WS.sub(" ", n).strip(" .,-")
    if not n or n in UNRESOLVED:
        return None
    for suffix, repl in _PLURALS:
        if n.endswith(suffix):
            n = n[: -len(suffix)] + repl
            break
    n = ALIASES.get(n, n)
    if n in UNRESOLVED:
        return None
    return n or None


def display_name(canonical: str) -> str:
    """Title-ish form for output. Keeps short equipment words lowercase-safe
    by simply capitalizing the first letter — lifting names read badly in
    full Title Case ("Trap Bar Deadlift" vs "Trap bar deadlift")."""
    return canonical[:1].upper() + canonical[1:] if canonical else canonical
