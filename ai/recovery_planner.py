"""
Deterministic recovery prescription.

The brief never told Dylan to do recovery work. It read the RECENT RECOVERY
SESSIONS block and described what he had already done, because the prompt
explicitly said "Only suggest adding recovery work if the block is genuinely
empty" — so a consistent sauna user got acknowledgement and never direction.

Meanwhile knowledge/04_hrv_recovery.md §6 and §7 carry exactly the protocols
that should drive this, and the RAG retriever returns "" on every call (its
dependencies are not in requirements.txt). Encoding the decision here is how
that knowledge actually reaches the brief.

The rule that matters most for Dylan right now: cold water immersion within
4-6h AFTER strength training blunts the inflammatory signalling that drives
adaptation. He is in a strength phase with a bench goal, so a post-lift plunge
works directly against the thing he is training for. Sauna has no such
conflict and is the modality he actually has easy access to (91 sauna sessions
against 6 ice baths over nine months).

Protocol constants below are from knowledge/04_hrv_recovery.md; change them
there and here together.
"""

from __future__ import annotations

from typing import Optional

SAUNA = "sauna"
COLD = "cold_plunge"
STEAM = "steam"
BREATHING = "breathing"

# knowledge/04 §6
SAUNA_TEMP = "170-185°F"
SAUNA_DURATION = "15-20 min"
SAUNA_WEEKLY_TARGET = 4  # sessions/week for the cardiovascular effect
# knowledge/04 §7
COLD_TEMP = "50-60°F"
COLD_DURATION = "2-5 min"
COLD_POST_LIFT_EMBARGO_H = 6
COLD_WEEKLY_MINUTES = 11  # Huberman total weekly cold exposure


def plan_recovery(
    *,
    session_kind: str = "rest",
    session_sub: Optional[str] = None,
    band: Optional[str] = None,
    phase_focus: str = "",
    recent: Optional[dict] = None,
    access: Optional[dict] = None,
) -> dict:
    """Recommend recovery work for today.

    `recent` is {modality: count_in_last_7_days}.
    `access` is {modality: "easy" | "trip"} — a modality behind a trip to
    another gym has to be worth the trip, so it is only recommended when it is
    clearly the right call rather than as a default.

    Returns {"recommendations": [...], "warnings": [...], "notes": [...]}.
    """
    recent = recent or {}
    # An explicitly empty dict means "nothing available" and must not fall
    # through to the default — `or` would have swallowed that distinction.
    access = {SAUNA: "easy"} if access is None else access
    kind = (session_kind or "rest").lower()
    sub = (session_sub or "").lower()
    is_lift = kind in ("lift", "strength")
    is_hard_run = kind == "run" and sub in ("quality", "long")
    strength_phase = phase_focus in ("strength", "")

    recs: list[dict] = []
    warnings: list[str] = []
    notes: list[str] = []

    sauna_7d = recent.get(SAUNA, 0)

    # ── Red recovery: heat stress is a contraindication, not a remedy.
    if band == "red":
        warnings.append(
            "Recovery is RED — skip the sauna today. Heat stress on top of a "
            "red day adds load rather than removing it (knowledge/04 §6 lists "
            "red recovery as a contraindication)."
        )
        recs.append(
            {
                "modality": BREATHING,
                "when": "evening",
                "duration": "5-10 min",
                "detail": "slow nasal breathing, ~6 breaths/min",
                "reason": "Parasympathetic work with no thermal load. Acute HRV lift.",
                "priority": 1,
            }
        )
        if access.get(COLD) and band == "red" and not is_lift:
            recs.append(
                {
                    "modality": COLD,
                    "when": "morning or any time away from lifting",
                    "duration": COLD_DURATION,
                    "detail": COLD_TEMP,
                    "reason": (
                        "Cold is appropriate on a yellow/red day — it reduces "
                        "inflammation and drives a parasympathetic rebound."
                    ),
                    "priority": 2,
                }
            )
        return {"recommendations": recs, "warnings": warnings, "notes": notes}

    # ── Sauna: the default, and the one he actually has access to.
    if access.get(SAUNA):
        if is_hard_run:
            when = "AFTER today's run, not before"
            reason = (
                "Post-run heat is fine and aids cardiovascular adaptation. "
                "Sauna BEFORE a quality or long run starts you dehydrated and "
                "raises early fatigue."
            )
        elif is_lift:
            when = "within 1-2h after the lift"
            reason = (
                "Post-lift heat enhances cardiovascular adaptation and, unlike "
                "cold, does not interfere with the strength adaptation you are "
                "training for."
            )
        else:
            when = "any time — a rest day is the highest-value slot"
            reason = (
                "No session competing for recovery, so heat exposure is pure "
                "gain: plasma volume, HRV, deep sleep."
            )
        gap = SAUNA_WEEKLY_TARGET - sauna_7d
        if gap > 0:
            reason += (
                f" You are at {sauna_7d} sauna session"
                f"{'s' if sauna_7d != 1 else ''} in the last 7 days; "
                f"{SAUNA_WEEKLY_TARGET}+ is where the cardiovascular and HRV "
                "benefits show up."
            )
        else:
            reason += (
                f" You are at {sauna_7d} this week, already at the "
                f"{SAUNA_WEEKLY_TARGET}x/week target — maintain, don't chase more."
            )
        recs.append(
            {
                "modality": SAUNA,
                "when": when,
                "duration": SAUNA_DURATION,
                "detail": f"{SAUNA_TEMP}, 2-3 rounds, 16-24 oz water + electrolytes",
                "reason": reason,
                "priority": 1 if gap > 0 else 2,
            }
        )

    # ── Cold: the timing rule that actually matters.
    if is_lift and strength_phase:
        warnings.append(
            f"NO cold plunge within {COLD_POST_LIFT_EMBARGO_H}h after today's "
            "lift. Cold blunts the inflammatory signalling that drives muscle "
            "adaptation — during a strength phase with a bench goal that works "
            "directly against the session you just did. Before lifting, or a "
            "different day, is fine."
        )
    elif access.get(COLD) and (is_hard_run or band == "yellow"):
        recs.append(
            {
                "modality": COLD,
                "when": "after the run" if is_hard_run else "morning",
                "duration": COLD_DURATION,
                "detail": f"{COLD_TEMP}, rewarm naturally",
                "reason": (
                    "Cold after endurance work is appropriate — the adaptation-"
                    "blunting concern applies to strength training, not running."
                    if is_hard_run
                    else "Yellow recovery: cold reduces inflammation and drives a "
                    "parasympathetic rebound."
                )
                + (
                    " Worth the trip to a location that has it; otherwise skip it, "
                    "sauna is the higher-frequency win."
                    if access.get(COLD) == "trip"
                    else ""
                ),
                "priority": 3,
            }
        )
        cold_7d = recent.get(COLD, 0)
        if cold_7d == 0:
            notes.append(
                f"No cold exposure logged in 7 days. Target is ~"
                f"{COLD_WEEKLY_MINUTES} min/week total, split across sessions."
            )

    if access.get(STEAM) == "trip" and not access.get(SAUNA):
        recs.append(
            {
                "modality": STEAM,
                "when": "post-session",
                "duration": "15 min",
                "detail": "steam room",
                "reason": "Substitute for dry sauna when that is what is available.",
                "priority": 4,
            }
        )

    recs.sort(key=lambda r: r["priority"])
    return {"recommendations": recs, "warnings": warnings, "notes": notes}


def render_recovery_prescription(plan: dict) -> str:
    """Render the recovery prescription for the brief prompt."""
    recs = (plan or {}).get("recommendations") or []
    warnings = (plan or {}).get("warnings") or []
    notes = (plan or {}).get("notes") or []
    if not recs and not warnings:
        return ""
    lines = ["RECOVERY PRESCRIPTION (prescribe this, do not merely acknowledge it):"]
    for r in recs:
        lines.append(
            f"  {r['modality'].replace('_', ' ').upper()} — {r['when']}, "
            f"{r['duration']} ({r['detail']})"
        )
        lines.append(f"       why: {r['reason']}")
    for w in warnings:
        lines.append(f"  ⚠ {w}")
    for n in notes:
        lines.append(f"  note: {n}")
    return "\n".join(lines)
