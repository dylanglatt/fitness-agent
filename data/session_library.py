"""
Standalone lift sessions that live outside the weekly template.

The weekly template (Day A/B/C) is the plan. These are extras Dylan can drop
onto any date with `/swap pump [date]` (same word works in the app's
/swap-session endpoint and Muse's set_day). Picking one never edits the
template or renames a lettered day: it writes a daily_plan_overrides row
like any other swap, and the full-body planner builds the session from the
prescription below with real computed weights.

Rules for entries here:
  * session_type is "lift" and the focus contains "pump", so
    ai/training_state.classify_planned_session tags it ("lift", "upper_pump")
    instead of guessing a push/pull/legs pattern from the focus words.
  * Keep "push", "pull", "legs", "core", "upper" and "full body" OUT of the
    focus text: any of those flips the classifier to a different branch.
  * No "Day X" phrasing in focus or prescription, or plan_ops.day_label will
    label it as a template day.
  * Exercise names match what's already logged, so weights come from history.
"""

from __future__ import annotations

LIBRARY: dict[str, dict] = {
    "pump": {
        "session_type": "lift",
        "focus": "pump (chest, back, shoulders, arms; no lower body)",
        "prescription": (
            "Pump session, no lower body. Incline DB press 3x10-12 (90s rest, "
            "2 RIR); Lat pulldown 3x12 superset with cable fly or pec deck "
            "3x15 (60s rest, 1-2 RIR); Plate-loaded shoulder press 2x12 "
            "superset with chest-supported row 3x12 (60s rest, 1-2 RIR); "
            "Cable lateral raise 3x15-20 superset with face pull 3x15-20 "
            "(45s rest, 0-1 RIR); Incline DB or cable curl 3x12-15 superset "
            "with overhead cable triceps extension 3x12-15 (45s rest, 0-1 "
            "RIR); Weighted dips or push-ups 2 sets near failure (finisher). "
            "~26 sets, 45-50 min."
        ),
        "notes": (
            "Extra session, not part of the weekly rotation. Use it when legs "
            "are sore or before an event. Everything is seated or "
            "chest-supported so legs and low back carry no load. Short rest "
            "and supersets are the point; chase the pump, not top-end load. "
            "Nothing here replaces a template lift day's progression."
        ),
    },
    "pump short": {
        "session_type": "lift",
        "focus": "pump, short pre-event version (chest, back, shoulders, arms)",
        "prescription": (
            "Short pump, about 25 min. Incline DB press 3x12 superset with "
            "lat pulldown 3x12 (60s rest, 2-3 RIR); Cable lateral raise 3x15 "
            "then cable curl 3x15 then overhead cable triceps extension 3x15 "
            "as a tri-set (45s rest, 1-2 RIR); Push-ups 2 sets, 1-2 short of "
            "failure."
        ),
        "notes": (
            "Pre-event version: look full, don't train hard. Nothing to "
            "failure, no new movements, so there's no soreness tomorrow. "
            "Finish 1-2 hours before you go out; carbs, salt and water do as "
            "much for the look as the sets."
        ),
    },
}

# user input -> LIBRARY key (lowercased, whitespace-collapsed)
ALIASES: dict[str, str] = {
    "pump": "pump",
    "upper pump": "pump",
    "pump day": "pump",
    "pump short": "pump short",
    "short pump": "pump short",
    "quick pump": "pump short",
    "pump quick": "pump short",
    "pre-event pump": "pump short",
    "pre event pump": "pump short",
    "date pump": "pump short",
}


def get(key: str) -> dict:
    """Fresh copy of a library session's fields (callers may mutate)."""
    return dict(LIBRARY[key])
