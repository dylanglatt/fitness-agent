"""
Shared vocabulary for plan edits (swap a day, move a session).

Pure parsing only: turns a user-typed target ("day b", "lift", "long run",
"rest", "wednesday") into a (kind, detail) pair. Resolving that pair against
the active weekly template lives in data/plan_ops.py, because the right answer
depends on the plan (which day is "Day B", which run day is the long run).

Used by the Discord /swap and /move commands (bot/commands.py), the app's
POST /swap-session and /move-session endpoints (api_server.py), and the Muse
MCP server (integrations/mcp_server.py), so all three accept the same words.

History: this used to map push / pull / legs onto lift overrides. The plan
moved to full body on 2026-09-15, and a "push" override on a full-body plan
produced a push-only session with no prescription behind it, so those
aliases are gone. Lift targets now resolve to a real template day.
Extra sessions outside the template (the "pump" day) resolve through
data/session_library.py instead.
"""

from __future__ import annotations

import re

from data.session_library import ALIASES as _LIBRARY_ALIASES

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

# kind -> accepted user inputs (already lowercased, whitespace-collapsed)
_ALIASES = {
    "lift": {
        "lift",
        "weights",
        "strength",
        "gym",
        "full body",
        "fullbody",
        "full-body",
    },
    "rest": {"rest", "off", "recovery", "active recovery", "rest day"},
    "cross": {
        "cross",
        "cross-train",
        "crosstrain",
        "cross train",
        "bike",
        "swim",
        "yoga",
    },
}

# run sub-kinds: the detail is a keyword matched against template run focus.
_RUN_ALIASES = {
    "run": None,
    "easy": "easy",
    "easy run": "easy",
    "z2": "easy",
    "zone 2": "easy",
    "aerobic": "easy",
    "quality": "quality",
    "quality run": "quality",
    "tempo": "quality",
    "intervals": "quality",
    "workout run": "quality",
    "speed": "quality",
    "long": "long",
    "long run": "long",
}

_DAY_RE = re.compile(r"^day\s*([a-z])$")

VALID_TARGETS_HINT = (
    "day a / day b / day c, lift, pump, pump short, run, easy, quality, "
    "long, rest, cross, "
    "a weekday name (copy that day's template session), or reset"
)


def parse_target(label: str) -> tuple[str, str | None] | None:
    """Return (kind, detail) for a user-typed target, or None if unknown.

    kinds:
      ("day", "b")          a lettered template lift day ("day b", "b")
      ("library", "pump")   a standalone session from data/session_library.py
      ("lift", None)        whichever lift day is owed (resolved in plan_ops)
      ("run", "long"|...)   a run, optionally a specific template run type
      ("rest", None)
      ("cross", None)
      ("weekday", "monday") copy that weekday's template session
      ("reset", None)       drop the override, back to the template
    """
    norm = " ".join((label or "").strip().lower().split())
    if not norm:
        return None
    if norm == "reset":
        return ("reset", None)
    m = _DAY_RE.match(norm)
    if m:
        return ("day", m.group(1))
    if len(norm) == 1 and norm.isalpha():
        return ("day", norm)
    if norm in WEEKDAYS:
        return ("weekday", norm)
    if norm in _LIBRARY_ALIASES:
        return ("library", _LIBRARY_ALIASES[norm])
    if norm in _RUN_ALIASES:
        return ("run", _RUN_ALIASES[norm])
    for kind, words in _ALIASES.items():
        if norm in words:
            return (kind, None)
    return None
