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
INCREMENT_LARGE = 10.0  # deadlift, squat
INCREMENT_MEDIUM = 5.0  # barbell presses, rows
INCREMENT_SMALL = 2.5  # cable/dumbbell isolation

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


def rep_range_for(sessions: list[dict], default_low: int) -> tuple[int, int]:
    """The rep range to run double progression in.

    The bottom of the range is what Dylan actually trains at (bench 6, OHP 10,
    lateral raise 12), so his session shape is preserved. The top is +2, which
    is the room to earn the next load increase.
    """
    low = infer_target_reps(sessions, default_low)
    return low, low + 2


def next_prescription(
    sessions: list[dict],
    exercise: str,
    *,
    rep_range: Optional[tuple[int, int]] = None,
    default_reps: int = 6,
    intensity_band: Optional[str] = None,
) -> dict:
    """Decide load AND reps for the next session — double progression.

    Adding weight every time you clear a single rep target is linear
    progression. It works for a novice and stalls fast for anyone else: you
    add 5 lb, miss, repeat, miss again, and the weight never moves. The
    previous rule did exactly that, so the load either climbed unsustainably
    or sat still.

    Double progression is what a coach actually programs:
      • Hit the TOP of the range on every working set  -> add weight, reset
        reps to the bottom of the range. The load moves, earned.
      • Hit at least the BOTTOM on every set           -> keep the weight, add
        one rep. Progress without a load jump you have not earned.
      • Miss the bottom on any set                     -> repeat.
      • Same weight stuck for 3+ sessions with no rep gain, or two failed
        sessions -> deload ~10% and rebuild. A stall is a signal, not
        something to grind through.

    Returns {weight_lb, target_reps, action, reason} where action is
    "add_weight" | "add_rep" | "repeat" | "deload" | "pick".
    """
    low, high = rep_range or rep_range_for(sessions, default_reps)
    inc = load_increment(exercise)
    step = INCREMENT_SMALL if inc == INCREMENT_SMALL else INCREMENT_MEDIUM

    def working(sets: list[dict]) -> list[dict]:
        weighted = [s for s in sets if s.get("weight_lb")]
        if not weighted:
            return []
        top = max(s["weight_lb"] for s in weighted)
        return [s for s in weighted if s["weight_lb"] == top]

    if not sessions:
        return {
            "weight_lb": None,
            "target_reps": low,
            "action": "pick",
            "reason": (
                f"No history for this lift — pick a weight you can get {low} "
                f"clean reps with and log it."
            ),
        }

    last = sessions[-1]
    last_working = working(last.get("sets") or [])
    if not last_working:
        return {
            "weight_lb": None,
            "target_reps": low,
            "action": "pick",
            "reason": f"Last session ({last.get('date')}) had no weighted sets.",
        }

    w = last_working[0]["weight_lb"]
    reps = [(s.get("reps") or 0) for s in last_working]
    worst, best = min(reps), max(reps)

    if intensity_band == "red":
        return {
            "weight_lb": _round_to(w * 0.9, step),
            "target_reps": low,
            "action": "deload",
            "reason": (
                f"Recovery is red — {_round_to(w * 0.9, step):g} lb for {low}, "
                "well under working weight. Movement quality, not load."
            ),
        }

    # How long has this weight been stuck without the reps improving?
    same_weight_sessions = 0
    best_at_weight: list[int] = []
    for sess in reversed(sessions):
        ws = working(sess.get("sets") or [])
        if not ws or ws[0]["weight_lb"] != w:
            break
        same_weight_sessions += 1
        best_at_weight.append(max((s.get("reps") or 0) for s in ws))
    stalled = (
        same_weight_sessions >= 3 and len(set(best_at_weight)) == 1 and worst < high
    )

    if stalled:
        deload = _round_to(w * 0.9, step)
        return {
            "weight_lb": deload,
            "target_reps": low + 1,
            "action": "deload",
            "reason": (
                f"{w:g} lb has sat at {best} reps for {same_weight_sessions} "
                f"sessions without moving. Drop to {deload:g} lb for {low + 1} "
                "and build back through the range — grinding a stall is not a "
                "stimulus."
            ),
        }

    if worst >= high:
        nxt = _round_to(w + inc, step)
        return {
            "weight_lb": nxt,
            "target_reps": low,
            "action": "add_weight",
            "reason": (
                f"Cleared the top of the range — {high} reps on every set at "
                f"{w:g} lb on {last.get('date')}. Add {inc:g} lb and reset to "
                f"{low}."
            ),
        }

    if worst >= low:
        nxt_reps = min(worst + 1, high)
        band_note = (
            " Recovery is yellow, so this is a rep, not a load jump."
            if intensity_band == "yellow"
            else ""
        )
        return {
            "weight_lb": w,
            "target_reps": nxt_reps,
            "action": "add_rep",
            "reason": (
                f"Got {worst}-{best} at {w:g} lb on {last.get('date')}. Same "
                f"weight, go for {nxt_reps} on every set — weight moves at "
                f"{high}.{band_note}"
            ),
        }

    # Missed the bottom of the range.
    prev = working(sessions[-2].get("sets") or []) if len(sessions) > 1 else []
    missed_twice = bool(
        prev
        and prev[0]["weight_lb"] == w
        and min((s.get("reps") or 0) for s in prev) < low
    )
    if missed_twice:
        deload = _round_to(w * 0.9, step)
        return {
            "weight_lb": deload,
            "target_reps": low,
            "action": "deload",
            "reason": (
                f"Missed {low} reps at {w:g} lb twice running (best {best}). "
                f"Back off to {deload:g} lb and rebuild — a third failed "
                "session is not a stimulus."
            ),
        }
    return {
        "weight_lb": w,
        "target_reps": low,
        "action": "repeat",
        "reason": (
            f"Got {worst} of {low} at {w:g} lb on {last.get('date')} — repeat "
            "and own the bottom of the range before adding anything."
        ),
    }


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
        return (
            None,
            "No history for this lift — pick a working weight and log it.",
            "pick",
        )

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
    prev_working = (
        working((sessions[-2].get("sets") or [])) if len(sessions) > 1 else []
    )
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


def progression_pace(
    sessions: list[dict],
    target_e1rm: float,
    deadline_iso: str,
    today_iso: str,
) -> Optional[dict]:
    """Is the current rate of progress actually going to hit the goal?

    The part a coach does that a set-by-set rule cannot: look at the slope and
    say whether it lands. Without this, "add 5 lb when you clear the range"
    can run for a year and quietly miss by 40 lb, and nothing in the system
    would ever have said so.

    Returns None when there is not enough history to draw a line.
    """
    from datetime import date as _d

    pts = []
    for sess in sessions:
        weighted = [
            s for s in (sess.get("sets") or []) if s.get("weight_lb") and s.get("reps")
        ]
        if not weighted:
            continue
        best = max(s["weight_lb"] * (1 + s["reps"] / 30.0) for s in weighted)
        try:
            pts.append((_d.fromisoformat(str(sess["date"])[:10]), best))
        except Exception:
            continue
    if len(pts) < 4:
        return None
    pts.sort()
    span_weeks = (pts[-1][0] - pts[0][0]).days / 7.0
    if span_weeks < 2:
        return None
    current = pts[-1][1]
    per_week = (current - pts[0][1]) / span_weeks
    try:
        deadline = _d.fromisoformat(deadline_iso[:10])
        today = _d.fromisoformat(today_iso[:10])
    except Exception:
        return None
    weeks_left = max((deadline - today).days / 7.0, 0.1)
    projected = current + per_week * weeks_left
    needed = (target_e1rm - current) / weeks_left
    on_pace = projected >= target_e1rm
    if per_week <= 0:
        note = (
            f"e1RM is flat or falling ({current:.0f} lb). At this rate the "
            f"{target_e1rm:.0f} lb goal does not happen — something has to "
            "change: the stall protocol, volume, or the deadline."
        )
    elif on_pace:
        note = (
            f"On pace: {current:.0f} lb now, +{per_week:.1f} lb/week over the "
            f"last {span_weeks:.0f} weeks, projecting {projected:.0f} lb by "
            f"{deadline_iso}."
        )
    else:
        note = (
            f"BEHIND pace: {current:.0f} lb now, +{per_week:.1f} lb/week, "
            f"projecting {projected:.0f} lb by {deadline_iso} against a "
            f"{target_e1rm:.0f} lb target. Needs +{needed:.1f} lb/week. "
            "Either the rate changes or the target does — say which."
        )
    return {
        "current_e1rm": round(current),
        "per_week": round(per_week, 2),
        "projected": round(projected),
        "needed_per_week": round(needed, 2),
        "on_pace": on_pace,
        "weeks_left": round(weeks_left),
        "note": note,
    }


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


def infer_target_sets(sessions: list[dict], default: int, lookback: int = 3) -> int:
    """Working-set count from history, like infer_target_reps does for reps.

    Dylan's push day is three working sets per movement. A planner that
    prescribes four is not reading his training, it is asserting a template —
    the same mistake as imposing a rep scheme.
    """
    if not sessions:
        return default
    counts: dict[int, int] = {}
    for sess in sessions[-lookback:]:
        weighted = [s for s in (sess.get("sets") or []) if s.get("weight_lb")]
        if not weighted:
            continue
        top = max(s["weight_lb"] for s in weighted)
        n = sum(1 for s in weighted if s["weight_lb"] == top)
        counts[n] = counts.get(n, 0) + 1
    if not counts:
        return default
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


_UNILATERAL = re.compile(
    r"\b(single[- ]arm|single[- ]leg|one[- ]arm|each side)\b", re.I
)


def is_unilateral(name: str) -> bool:
    """True for movements performed one limb at a time, so the plan can say
    'x12 each side' rather than silently halving the real volume."""
    return bool(_UNILATERAL.search(name or ""))


def warmup_for(working_weight: Optional[float], exercise: str) -> Optional[dict]:
    """One warmup set for a compound, at ~65% of the working load.

    Dylan's bench day is literally "1 set of 135 for 10, then 3 of 205 for 6".
    The planner previously emitted only working sets, so the session it showed
    him was not the session he does. 65% of 205 rounds to 135, which is the
    weight he actually uses.
    """
    if not working_weight or not _is_compound(exercise):
        return None
    w = _round_to(working_weight * 0.65, INCREMENT_MEDIUM)
    w = max(w, 45.0)  # an empty barbell is the floor
    if w >= working_weight:
        return None
    return {"weight_lb": w, "reps": 10}


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
        sessions_for_lift = history.get(low) or []
        rng = rep_range_for(sessions_for_lift, default_reps)

        if not is_primary_phase:
            sets = SETS_MAINTENANCE
        elif idx == 0 or is_goal:
            sets = SETS_PRIMARY.get(pattern, 3)
        else:
            sets = SETS_ACCESSORY
        if intensity_band == "red":
            sets = max(2, sets - 1)

        presc = next_prescription(
            sessions_for_lift, name, rep_range=rng, intensity_band=intensity_band
        )
        weight = presc["weight_lb"]
        reps = presc["target_reps"]
        reason = presc["reason"]
        action = presc["action"]
        # Set count follows history too, unless the phase has parked this
        # modality at maintenance (in which case the phase wins).
        if is_primary_phase:
            sets = infer_target_sets(sessions_for_lift, sets)
            if intensity_band == "red":
                sets = max(2, sets - 1)
        role = "GOAL LIFT" if is_goal else ("Main" if idx == 0 else "Accessory")
        out.append(
            {
                "name": name,
                "sets": sets,
                "reps": str(reps),
                "rep_range": f"{rng[0]}-{rng[1]}",
                "target_weight_lb": weight,
                # Warmup only on the opening lift. Dylan's push day is one warmup
                # set on bench and straight into working sets on everything after,
                # and a "45 lb bar" floor is meaningless for a dumbbell movement.
                "warmup": warmup_for(weight, low) if idx == 0 else None,
                "per_side": is_unilateral(name),
                "reason": reason,
                "action": action,
                "notes": role,
            }
        )
    return out
