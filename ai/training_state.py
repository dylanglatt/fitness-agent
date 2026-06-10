"""
Deterministic training-state + readiness logic for the recommendation engine.

These are PURE functions — no DB, no network, no LLM — so the scheduling
decision is reliable and unit-testable. The coach assembles recent logged data,
builds a training state, and asks assess_readiness() which movement patterns are
recovered enough to train today. The LLM only *explains* the result.

Phase 0+1 of docs/recommendation_engine_plan.md.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

# ── Movement-pattern taxonomy ────────────────────────────────────────────────
PUSH, PULL, LEGS, CORE = "push", "pull", "legs", "core"
LIFT_PATTERNS = (PUSH, PULL, LEGS)  # core is accessory; not spacing-gated

# Exercise-name keyword -> (pattern, primary muscle). First match wins; the list
# is ordered so more-specific phrases (e.g. "leg curl") are checked before
# broader ones. Extend freely — unknown exercises classify as (None, None).
_EXERCISE_KEYWORDS: list[tuple[str, tuple[str, str]]] = [
    # legs
    ("front squat", (LEGS, "quads")), ("back squat", (LEGS, "quads")),
    ("split squat", (LEGS, "quads")), ("squat", (LEGS, "quads")),
    ("leg press", (LEGS, "quads")), ("leg extension", (LEGS, "quads")),
    ("lunge", (LEGS, "quads")), ("step-up", (LEGS, "quads")),
    ("romanian deadlift", (LEGS, "hamstrings")), ("rdl", (LEGS, "hamstrings")),
    ("deadlift", (LEGS, "hamstrings")), ("leg curl", (LEGS, "hamstrings")),
    ("hip thrust", (LEGS, "glutes")), ("glute", (LEGS, "glutes")),
    ("calf", (LEGS, "calves")),
    # push
    ("incline", (PUSH, "chest")), ("bench", (PUSH, "chest")),
    ("chest press", (PUSH, "chest")), ("chest fly", (PUSH, "chest")),
    ("push-up", (PUSH, "chest")), ("pushup", (PUSH, "chest")),
    ("dip", (PUSH, "chest")),
    ("overhead press", (PUSH, "shoulders")), ("ohp", (PUSH, "shoulders")),
    ("shoulder press", (PUSH, "shoulders")), ("military press", (PUSH, "shoulders")),
    ("lateral raise", (PUSH, "shoulders")), ("front raise", (PUSH, "shoulders")),
    ("tricep", (PUSH, "triceps")), ("pushdown", (PUSH, "triceps")),
    ("skull crusher", (PUSH, "triceps")), ("close grip", (PUSH, "triceps")),
    # pull
    ("pull-up", (PULL, "back")), ("pullup", (PULL, "back")),
    ("chin-up", (PULL, "back")), ("chinup", (PULL, "back")),
    ("lat pulldown", (PULL, "back")), ("pulldown", (PULL, "back")),
    ("pull down", (PULL, "back")), ("row", (PULL, "back")),
    ("face pull", (PULL, "rear delts")), ("rear delt", (PULL, "rear delts")),
    ("bicep", (PULL, "biceps")), ("curl", (PULL, "biceps")),
    ("shrug", (PULL, "traps")),
    # core
    ("ab wheel", (CORE, "core")), ("hanging leg", (CORE, "core")),
    ("russian twist", (CORE, "core")), ("plank", (CORE, "core")),
    ("crunch", (CORE, "core")), ("sit-up", (CORE, "core")),
    ("situp", (CORE, "core")), ("core", (CORE, "core")),
]

# Parser's coarse Workout select (Push/Pull/Legs) -> pattern, used only as a
# fallback when the exercise name doesn't match a keyword.
_WORKOUT_TAG_TO_PATTERN = {"push": PUSH, "pull": PULL, "legs": LEGS}

# Run types.
RUN_EASY, RUN_QUALITY, RUN_LONG = "easy", "quality", "long"


def classify_exercise(
    name: str, workout_tag: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    """Return (pattern, muscle) for an exercise name.

    Falls back to the coarse parser Workout tag when the name is unknown, then
    (None, None). Pure string logic — easy to unit-test and extend.
    """
    n = (name or "").strip().lower()
    if n:
        for kw, (pat, mus) in _EXERCISE_KEYWORDS:
            if kw in n:
                return pat, mus
    tag = (workout_tag or "").strip().lower()
    if tag in _WORKOUT_TAG_TO_PATTERN:
        return _WORKOUT_TAG_TO_PATTERN[tag], None
    return None, None


def classify_run(
    activity: dict,
    *,
    long_run_min_mi: float = 8.0,
    quality_pace_min_per_mi: float = 8.0,
    quality_hr: float = 160.0,
) -> Optional[str]:
    """Classify a cardio activity as 'long' / 'quality' / 'easy'.

    Returns None for non-run activities. Thresholds are defaults from the design
    plan (§7) and are tunable. Long is distance-driven; quality is pace- or
    HR-driven; everything else is easy aerobic.
    """
    sport = (activity.get("sport_type") or "").lower()
    if "run" not in sport:
        return None
    dist_mi = (activity.get("distance_m") or 0) / 1609.34
    if dist_mi >= long_run_min_mi:
        return RUN_LONG
    speed = activity.get("average_speed_mps") or 0
    if speed:
        pace_min_per_mi = 1609.34 / speed / 60.0
        if pace_min_per_mi <= quality_pace_min_per_mi:
            return RUN_QUALITY
    if (activity.get("average_hr") or 0) >= quality_hr:
        return RUN_QUALITY
    return RUN_EASY


def _parse_date(s) -> Optional[date]:
    if isinstance(s, date):
        return s
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)[:10]).date()
    except ValueError:
        return None


def build_training_state(
    lifts: list[dict],
    activities: list[dict],
    today: date,
) -> dict:
    """Compute per-pattern recency + recent volume from logged data.

    `lifts`: rows with at least 'date' and 'exercise' (optional 'workout').
    `activities`: rows with 'date', 'sport_type' (+ distance/speed/hr).
    Recency is at day granularity — the SQLite log stores dates, not times,
    which is fine for 48h-class spacing.

    Returns a dict (see keys below). All days_ago are integers (0 = today).
    """
    patterns: dict[str, dict] = {}
    for row in lifts or []:
        d = _parse_date(row.get("date"))
        if not d or d > today:
            continue
        pat, _ = classify_exercise(row.get("exercise"), row.get("workout"))
        if pat is None:
            continue
        days_ago = (today - d).days
        slot = patterns.setdefault(pat, {"last_date": d, "days_ago": days_ago, "count_7d": 0})
        if d > slot["last_date"]:
            slot["last_date"], slot["days_ago"] = d, days_ago
        if days_ago <= 6:
            slot["count_7d"] += 1

    runs: dict[str, dict] = {}
    last_hard_run_days_ago: Optional[int] = None
    for act in activities or []:
        rtype = classify_run(act)
        if rtype is None:
            continue
        d = _parse_date(act.get("date"))
        if not d or d > today:
            continue
        days_ago = (today - d).days
        slot = runs.setdefault(rtype, {"last_date": d, "days_ago": days_ago, "count_7d": 0})
        if d > slot["last_date"]:
            slot["last_date"], slot["days_ago"] = d, days_ago
        if days_ago <= 6:
            slot["count_7d"] += 1
        if rtype in (RUN_QUALITY, RUN_LONG):
            if last_hard_run_days_ago is None or days_ago < last_hard_run_days_ago:
                last_hard_run_days_ago = days_ago

    last_legs_days_ago = patterns.get(LEGS, {}).get("days_ago")
    return {
        "patterns": patterns,
        "runs": runs,
        "last_hard_run_days_ago": last_hard_run_days_ago,
        "last_legs_days_ago": last_legs_days_ago,
    }


def classify_planned_session(session: Optional[dict]) -> tuple[str, Optional[str]]:
    """Map a plan/override session dict -> (kind, sub) where kind is
    'lift' | 'run' | 'rest' and sub is the pattern (push/pull/legs) or run type.
    Uses session_type + focus text. Returns ('rest', None) when absent.
    """
    if not session:
        return "rest", None
    stype = (session.get("session_type") or "").lower()
    focus = (session.get("focus") or "").lower()
    if stype in ("rest", "off"):
        return "rest", None
    if stype in ("run", "long_run", "easy_run", "interval", "tempo"):
        if "long" in focus or stype == "long_run":
            return "run", RUN_LONG
        if any(k in focus for k in ("quality", "tempo", "interval", "threshold", "hard")) \
                or stype in ("interval", "tempo"):
            return "run", RUN_QUALITY
        return "run", RUN_EASY
    if stype in ("lift", "strength"):
        for pat in LIFT_PATTERNS:
            if pat in focus:
                return "lift", pat
        if "upper" in focus:
            return "lift", PUSH  # coarse; upper-body day
        return "lift", None  # lift day, pattern unspecified
    return "rest", None


def assess_readiness(
    state: dict,
    planned: tuple[str, Optional[str]],
    *,
    spacing_days: int = 2,            # 48h ≈ 2 calendar days at day granularity
    run_leg_buffer_days: int = 1,     # legs kept ≥1 day from a hard/long run
) -> dict:
    """Decide whether today's planned session is appropriate given recency, and
    suggest a swap when it isn't. Pure logic — the brief renders the result.

    Returns:
      {
        'planned_kind', 'planned_sub',
        'status': 'ready' | 'too_soon' | 'interference' | 'unknown',
        'reason': str,
        'freshest_pattern': Optional[str],   # least-recently-trained ready lift
        'pattern_status': {pattern: (days_ago|None, 'ready'|'too_soon')},
        'suggested': Optional[(kind, sub)],  # swap suggestion when not ready
      }
    """
    kind, sub = planned
    patterns = state.get("patterns", {})

    # Per-pattern readiness (a pattern is ready if not trained within spacing).
    pattern_status: dict[str, tuple[Optional[int], str]] = {}
    for pat in LIFT_PATTERNS:
        days_ago = patterns.get(pat, {}).get("days_ago")
        ready = days_ago is None or days_ago >= spacing_days
        pattern_status[pat] = (days_ago, "ready" if ready else "too_soon")

    # Freshest = ready pattern trained longest ago (None days_ago = never → freshest).
    def freshness_key(pat: str) -> float:
        da = patterns.get(pat, {}).get("days_ago")
        return float("inf") if da is None else da
    ready_patterns = [p for p in LIFT_PATTERNS if pattern_status[p][1] == "ready"]
    freshest = max(ready_patterns, key=freshness_key) if ready_patterns else None

    result = {
        "planned_kind": kind,
        "planned_sub": sub,
        "status": "ready",
        "reason": "",
        "freshest_pattern": freshest,
        "pattern_status": pattern_status,
        "suggested": None,
    }

    if kind == "rest":
        result["reason"] = "Rest day."
        return result

    if kind == "lift":
        if sub is None:
            result["status"] = "unknown"
            result["reason"] = (
                "Lift day, pattern unspecified — pick the freshest ready pattern"
                f"{f' ({freshest})' if freshest else ''}."
            )
            if freshest:
                result["suggested"] = ("lift", freshest)
            return result
        days_ago, st = pattern_status.get(sub, (None, "ready"))
        if st == "too_soon":
            result["status"] = "too_soon"
            result["reason"] = (
                f"{sub} was trained {days_ago} day(s) ago — inside the "
                f"{spacing_days}-day spacing window."
            )
            if freshest and freshest != sub:
                result["suggested"] = ("lift", freshest)
                result["reason"] += f" Swap to {freshest} (freshest ready pattern)."
            return result
        result["reason"] = (
            f"{sub} last trained "
            f"{'never' if days_ago is None else f'{days_ago} day(s) ago'} — ready."
        )
        return result

    if kind == "run":
        # Interference guard: a hard/long run too close to legs (either way).
        lr = state.get("last_legs_days_ago")
        if sub in (RUN_QUALITY, RUN_LONG) and lr is not None and lr <= run_leg_buffer_days:
            result["status"] = "interference"
            result["reason"] = (
                f"Planned {sub} run, but legs were trained {lr} day(s) ago — "
                "too close for a hard/long run (concurrent-training interference). "
                "Consider an easy run today, or move the quality run."
            )
            result["suggested"] = ("run", RUN_EASY)
            return result
        result["reason"] = f"{sub} run — clear of interference windows."
        return result

    return result


def render_readiness_block(state: dict, readiness: dict) -> str:
    """Render the deterministic readiness assessment as a context block for the
    morning-brief prompt. Returns '' if there's nothing useful to say."""
    patterns = state.get("patterns", {})
    if not patterns and not state.get("runs"):
        return ""  # no logged history yet — nothing to assess

    lines = ["TRAINING READINESS (computed from logged sessions — authoritative):"]
    for pat in LIFT_PATTERNS:
        days_ago, st = readiness["pattern_status"].get(pat, (None, "ready"))
        when = "not in last 14d" if days_ago is None else f"{days_ago}d ago"
        cnt = patterns.get(pat, {}).get("count_7d", 0)
        flag = "READY" if st == "ready" else "TOO SOON (<48h)"
        lines.append(f"  {pat.title()}: last {when}, {cnt}x in 7d — {flag}")

    lhr = state.get("last_hard_run_days_ago")
    if lhr is not None:
        lines.append(f"  Last hard/long run: {lhr}d ago")

    pk, ps = readiness["planned_kind"], readiness["planned_sub"]
    planned_label = f"{pk}" + (f" / {ps}" if ps else "")
    lines.append(f"  Today's planned session: {planned_label} → {readiness['status'].upper()}")
    if readiness["reason"]:
        lines.append(f"    {readiness['reason']}")
    if readiness.get("suggested"):
        sk, ss = readiness["suggested"]
        lines.append(f"    SUGGESTED INSTEAD: {sk}" + (f" / {ss}" if ss else ""))
    return "\n".join(lines)
