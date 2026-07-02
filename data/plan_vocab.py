"""
Shared session-swap vocabulary.

Single source of truth for translating user-facing swap targets ("push",
"pull", "legs", "run", "rest", "cross") into the internal session_type
vocabulary the weekly_template and daily_plan_overrides use ('lift' / 'run' /
'rest' / 'cross_train'), with a readable focus label preserved.

Used by both the Discord /swap command (bot/commands.py) and the iOS app's
POST /swap-session endpoint (api_server.py) — extracted so the two can't
drift.
"""

SWAP_ALIASES = {
    # session_type → set of accepted user inputs
    "lift_push":  {"push", "lift_push", "bench"},
    "lift_pull":  {"pull", "lift_pull", "row"},
    "lift_legs":  {"legs", "lift_legs", "squat", "deadlift"},
    "lift":       {"lift", "weights", "strength"},
    "run":        {"run", "easy", "tempo", "intervals", "long"},
    "rest":       {"rest", "off", "recovery"},
    "cross_train": {"cross", "cross-train", "crosstrain", "bike", "swim", "yoga"},
}


def normalize_swap(label: str) -> tuple[str, str] | None:
    """Return (session_type, focus_label) for a user-typed swap target.

    Returns None if the input doesn't match any known category. The focus
    label is the title-cased original word so "pull" becomes
    session_type='lift', focus='Pull' — readable in both adherence + brief
    output.
    """
    norm = (label or "").strip().lower()
    for kind, aliases in SWAP_ALIASES.items():
        if norm in aliases:
            if kind.startswith("lift_"):
                sub = kind.split("_", 1)[1].title()  # Push / Pull / Legs
                return ("lift", sub)
            if kind == "lift":
                return ("lift", "lift")
            if kind == "cross_train":
                return ("cross_train", norm.title())
            return (kind, norm.title())
    return None
