"""
Deterministic lift-session planning.

The guided /liftstart flow had three failures, all reported by the owner and
all traceable to this area of the code:

  "It constantly guesses what I should lift."
      start_lift_session read `prescription` — free text from the weekly
      template — and asked Haiku to parse it into exercises. The template is
      the auto-seeded "Balanced concurrent" default from 2026-04-17 that was
      never customized, so the session plan was an LLM's reading of generic
      prose rather than anything derived from what Dylan actually trains.

  "I have to tell it what to do next."
      _recommend_for_exercise regex-scraped `lifts.details` free text for a
      weight while structured weight/reps sat unused in lift_sets two tables
      over. Its docstring promised to hold the weight when reps were missed;
      the code never looked at reps and added +5/+10 unconditionally. A
      counter that only goes up is not progression — it walks you into a
      failed set and then tells you to add weight again. When the regex found
      nothing it returned "Pick from feel", i.e. asked Dylan.

  "It tells me to end my workouts early."
      Once the cursor passed the parsed exercise list the only reply was
      "All planned exercises done. /liftend to close out." Three exercises
      parsed out of vague prose meant being told you were finished after
      three.

This module is the decision layer: pure functions over history, so the plan is
computed and testable and the LLM only delivers it.
"""

from __future__ import annotations

import re
from typing import Optional

# Load increments. Big hinge/squat patterns move in bigger jumps than presses,
# and isolation work moves in the smallest jump the rack allows.
INCREMENT_LARGE = 10.0   # deadlift, squat
INCREMENT_MEDIUM = 5.0   # barbell presses, rows
INCREMENT_SMALL = 2.5    # cable/dumbbell isolation

_LARGE = re.compile(r"\b(deadlift|squat)\b", re.I)
_SMALL = re.compile(
    r"\b(lateral raise|curl|fly|flye|pushdown|extension|raise|crunch|"
    r"pulldown|face pull)\b",
    re.I,
)


def load_increment(exercise: str) -> float:
    if _LARGE.search(exercise or ""):
        return INCREMENT_LARGE
    if _SMALL.search(exercise or ""):
        return INCREMENT_SMALL
    return INCREMENT_MEDIUM


def _round_to(value: float, step: float) -> float:
    return round(value / step) * step


def next_load(
    sessions: list[dict],
    target_reps: int,
    exercise: str,
    *,
    intensity_band: Optional[str] = None,
) -> tuple[Optional[float], str, str]:
    """Decide the working load for the next session of one exercise.

    `sessions` is newest-LAST: [{"date": str, "sets": [{"weight_lb", "reps"}]}]

    Returns (weight_lb, reason, action) where action is one of
    "progress" | "repeat" | "deload" | "pick".

    The rule the old code claimed to implement and did not:
      • Hit every working set at the target reps -> add one increment.
      • Missed -> repeat the same weight. Progression stalls, on purpose.
      • Missed the same weight twice running -> deload ~10%, so a stall
        becomes a planned back-off rather than a third failed session.
      • Red recovery day -> hold rather than progress, whatever the history.
    """
    if not sessions:
        return None, "No history for this lift — pick a working weight and log it.", "pick"

    inc = load_increment(exercise)
    step = INCREMENT_SMALL if inc == INCREMENT_SMALL else INCREMENT_MEDIUM

    def working(sets: list[dict]) -> list[dict]:
        """Working sets = those at the heaviest weight of the session. Warmups
        and back-offs must not decide whether the top load was earned."""
        weighted = [s for s in sets if s.get("weight_lb")]
        if not weighted:
            return []
        top = max(s["weight_lb"] for s in weighted)
        return [s for s in weighted if s["weight_lb"] == top]

    last = sessions[-1]
    last_working = working(last.get("sets") or [])
    if not last_working:
        return None, f"Last session ({last.get('date')}) had no weighted sets.", "pick"

    last_w = last_working[0]["weight_lb"]
    hit_all = all((s.get("reps") or 0) >= target_reps for s in last_working)

    if intensity_band == "red":
        return (
            _round_to(last_w * 0.9, step),
            f"Recovery is red — holding well under {last_w:g} lb today. "
            "Movement quality over load.",
            "deload",
        )

    if hit_all:
        if intensity_band == "yellow":
            return (
                last_w,
                f"Hit all {target_reps} reps at {last_w:g} lb on "
                f"{last.get('date')}, but recovery is yellow — repeat the "
                "weight and win the reps again rather than adding load.",
                "repeat",
            )
        nxt = _round_to(last_w + inc, step)
        return (
            nxt,
            f"Hit all {target_reps} reps at {last_w:g} lb on {last.get('date')} "
            f"— add {inc:g} lb.",
            "progress",
        )

    # Missed. Did we also miss this same weight the session before?
    prev_working = working((sessions[-2].get("sets") or [])) if len(sessions) > 1 else []
    missed_twice = bool(
        prev_working
        and prev_working[0]["weight_lb"] == last_w
        and not all((s.get("reps") or 0) >= target_reps for s in prev_working)
    )
    best = max((s.get("reps") or 0) for s in last_working)
    if missed_twice:
        deload = _round_to(last_w * 0.9, step)
        return (
            deload,
            f"{last_w:g} lb has stalled twice (best {best} of {target_reps} "
            f"reps). Drop to {deload:g} lb and build back — a third failed "
            "session is not a stimulus.",
            "deload",
        )
    return (
        last_w,
        f"Got {best} of {target_reps} reps at {last_w:g} lb on "
        f"{last.get('date')} — repeat the weight and own it.",
        "repeat",
    )


# ── Session shape ────────────────────────────────────────────────────────────

# Working sets per exercise by role and phase emphasis. A lift the phase has
# parked at maintenance gets fewer sets, which is what "maintenance" means in
# practice rather than as an adjective in a prompt.
SETS_PRIMARY = {"push": 4, "pull": 4, "legs": 4, "core": 3, "other": 3}
SETS_ACCESSORY = 3
SETS_MAINTENANCE = 2

# Rep targets by role.
REPS_COMPOUND = 6
REPS_ACCESSORY = 10
REPS_ISOLATION = 12


def infer_target_reps(sessions: list[dict], default: int, lookback: int = 3) -> int:
    """Rep target from what Dylan actually does, falling back to the class
    default.

    Without this the planner imposed a textbook rep scheme: overhead press got
    6 because it matches "press", while he has always trained it at 8. A plan
    that quietly changes your rep ranges is not "dynamic based on my history",
    and it also corrupts the progression check — 8 reps against a target of 6
    reads as a clean session when it was actually a missed one.

    Uses the modal rep count across the working sets of the last few sessions;
    ties go to the higher target, which is the conservative choice (harder to
    clear, so it will not wave through a session that was missed).
    """
    if not sessions:
        return default
    counts: dict[int, int] = {}
    for sess in sessions[-lookback:]:
        weighted = [s for s in (sess.get("sets") or []) if s.get("weight_lb")]
        if not weighted:
            continue
        top = max(s["weight_lb"] for s in weighted)
        for s in weighted:
            if s["weight_lb"] == top and s.get("reps"):
                counts[int(s["reps"])] = counts.get(int(s["reps"]), 0) + 1
    if not counts:
        return default
    best = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return best[0]


def _is_compound(name: str) -> bool:
    return bool(
        re.search(
            r"\b(bench|press|deadlift|squat|row|pulldown|pull-up|chin|dip)\b",
            name or "",
            re.I,
        )
    ) and not _SMALL.search(name or "")


def plan_session(
    pattern: str,
    library: list[dict],
    history: dict,
    *,
    goal_lifts: Optional[list[str]] = None,
    is_primary_phase: bool = True,
    intensity_band: Optional[str] = None,
    max_exercises: int = 6,
) -> list[dict]:
    """Build an ordered session for `pattern` from what Dylan actually trains.

    `library` is [{"exercise", "n_sets", "n_sessions", "last_date"}] for this
    pattern, most-trained first (Database.get_exercise_vocabulary).
    `history` maps lowercase exercise -> sessions list for next_load().
    `goal_lifts` are lifts named by an active goal; they lead the session and
    get primary set counts even if they are not the most-trained movement.

    Compounds first, then accessories, then isolation — the order load should
    be spent in. Returns [] when the library has nothing for this pattern,
    which the caller must report honestly rather than inventing a session.
    """
    if not library:
        return []
    goal_lifts = [g.lower() for g in (goal_lifts or [])]

    def rank(item: dict) -> tuple:
        name = item["exercise"].lower()
        is_goal = any(g in name or name in g for g in goal_lifts)
        return (not is_goal, not _is_compound(name), -item["n_sets"])

    ordered = sorted(library, key=rank)[:max_exercises]

    out: list[dict] = []
    for idx, item in enumerate(ordered):
        name = item["exercise"]
        low = name.lower()
        is_goal = any(g in low or low in g for g in goal_lifts)
        compound = _is_compound(low)

        if compound:
            default_reps = REPS_COMPOUND
        elif _SMALL.search(low):
            default_reps = REPS_ISOLATION
        else:
            default_reps = REPS_ACCESSORY
        reps = infer_target_reps(history.get(low) or [], default_reps)

        if not is_primary_phase:
            sets = SETS_MAINTENANCE
        elif idx == 0 or is_goal:
            sets = SETS_PRIMARY.get(pattern, 3)
        else:
            sets = SETS_ACCESSORY
        if intensity_band == "red":
            sets = max(2, sets - 1)

        weight, reason, action = next_load(
            history.get(low) or [], reps, name, intensity_band=intensity_band
        )
        role = "GOAL LIFT" if is_goal else ("Main" if idx == 0 else "Accessory")
        out.append({
            "name": name,
            "sets": sets,
            "reps": str(reps),
            "target_weight_lb": weight,
            "reason": reason,
            "action": action,
            "notes": role,
        })
    return out
