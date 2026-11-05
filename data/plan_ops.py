"""
Plan edits: swap what a date is, or move a session between two dates.

One implementation shared by the Discord commands (/swap, /move), the app API
(POST /swap-session, /move-session) and the Muse MCP server, so a change made
from any of them lands the same way:

  * Edits are daily_plan_overrides rows. The weekly template is never touched.
  * A lift target resolves to a REAL template day (session_type, focus AND
    prescription copied over), so the full-body planner has an exercise list
    to build from. Overrides used to carry prescription="" for lifts, which on
    the full-body plan meant /session had nothing to parse.
  * Every edited date's persisted planned_sessions row is dropped. Those rows
    are only checked for staleness by pattern, and every full-body day shares
    the pattern "full_body", so a stale Day A plan would otherwise survive a
    swap to Day B and /liftstart would walk you through the wrong session.
  * An override that would equal the template for that weekday is cleared
    instead of written, so moving a session back restores a clean template
    day (is_override=False) rather than leaving a redundant row.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from data import session_library
from data.plan_vocab import VALID_TARGETS_HINT, WEEKDAYS, parse_target

LIFT_TYPES = ("lift", "strength", "full_body")
_FIELDS = ("session_type", "focus", "prescription", "notes")
_LABEL_RE = re.compile(r"\bday\s+([a-z])\b", re.IGNORECASE)
_EDIT_MARK = "[edit]"
_EDIT_SPLIT = re.compile(r"\s*\[edit\]")
_RUN_KEYWORDS = {
    "easy": ("easy", "aerobic", "z2", "zone 2"),
    "quality": ("quality", "tempo", "interval", "threshold"),
    "long": ("long",),
}


class PlanEditError(ValueError):
    """User-facing error: the message is safe to show as-is."""


# ── template helpers ────────────────────────────────────────────────────────


def template_of(plan: dict | None) -> dict:
    tpl = (plan or {}).get("weekly_template") or {}
    if isinstance(tpl, str):
        try:
            tpl = json.loads(tpl)
        except Exception:
            tpl = {}
    return tpl


def day_label(session: dict | None) -> str | None:
    """'A' for a lift session whose prescription/focus says 'Day A', else None.

    Lift days only: run prescriptions mention lift days too ("placed after
    Day B on purpose") and must not pick up a label.
    """
    if not is_lift(session):
        return None
    for field in ("prescription", "focus"):
        m = _LABEL_RE.search(session.get(field) or "")
        if m:
            return m.group(1).upper()
    return None


def is_lift(session: dict | None) -> bool:
    return (session or {}).get("session_type", "").lower() in LIFT_TYPES


def lift_days(template: dict) -> list[tuple[str, dict]]:
    """Template lift days in week order: [("monday", {...}), ...]."""
    return [(d, template[d]) for d in WEEKDAYS if is_lift(template.get(d))]


def _fields(session: dict | None) -> dict:
    s = session or {}
    out = {k: (s.get(k) or "") for k in _FIELDS}
    # Drop the audit suffix a previous edit appended, so notes don't grow a
    # chain of "moved from ..." every time a session changes hands.
    out["notes"] = _EDIT_SPLIT.split(out["notes"], maxsplit=1)[0].strip()
    return out


def summarize(session: dict | None) -> dict:
    """Compact, JSON-safe view of a session for replies and tools."""
    s = session or {"session_type": "rest", "focus": "unscheduled"}
    return {
        "type": (s.get("session_type") or "rest").lower(),
        "focus": s.get("focus") or "",
        "day_label": day_label(s),
        "is_override": bool(s.get("is_override")),
    }


def describe(session: dict | None) -> str:
    """One-line human label: 'LIFT Day B — full body (quad + ...)'."""
    s = summarize(session)
    head = s["type"].upper() + (f" Day {s['day_label']}" if s["day_label"] else "")
    return f"{head} — {s['focus']}" if s["focus"] else head


# ── target + date resolution ────────────────────────────────────────────────


def resolve_target(template: dict, target: str, on_date: date) -> dict:
    """Turn a user target into concrete override fields for `on_date`.

    Raises PlanEditError with a message listing valid options when the
    target is unknown or the plan has no matching day.
    """
    parsed = parse_target(target)
    if not parsed:
        raise PlanEditError(f"Unknown target {target!r}. Try: {VALID_TARGETS_HINT}.")
    kind, detail = parsed
    if kind == "reset":
        raise PlanEditError("'reset' is not a session; clear the override instead.")

    if kind == "library":
        # Standalone session, not a template day: the template and the
        # Day A/B/C labels stay as they are.
        return _fields(session_library.get(detail))

    lifts = lift_days(template)

    if kind == "day":
        for _, sess in lifts:
            if day_label(sess) == detail.upper():
                return _fields(sess)
        labels = ", ".join(f"Day {day_label(s)}" for _, s in lifts if day_label(s))
        raise PlanEditError(
            f"The active plan has no Day {detail.upper()}."
            + (f" Lift days: {labels}." if labels else "")
        )

    if kind == "weekday":
        sess = template.get(detail)
        if not sess:
            raise PlanEditError(f"The active plan has nothing on {detail.title()}.")
        return _fields(sess)

    if kind == "lift":
        if not lifts:
            return {
                "session_type": "lift",
                "focus": "lift",
                "prescription": "",
                "notes": "",
            }
        # The lift that's owed: the latest template lift day on or before this
        # weekday. On a Tuesday that's Monday's Day A; before the first lift
        # day of the week it wraps to last week's final lift day.
        idx = on_date.weekday()
        owed = [s for d, s in lifts if WEEKDAYS.index(d) <= idx]
        return _fields(owed[-1] if owed else lifts[-1][1])

    if kind == "run":
        runs = [
            (d, template[d])
            for d in WEEKDAYS
            if (template.get(d) or {}).get("session_type", "").lower() == "run"
        ]
        if detail:
            keys = _RUN_KEYWORDS.get(detail, (detail,))
            for _, sess in runs:
                if any(k in (sess.get("focus") or "").lower() for k in keys):
                    return _fields(sess)
            return {
                "session_type": "run",
                "focus": detail,
                "prescription": "",
                "notes": "",
            }
        own = template.get(WEEKDAYS[on_date.weekday()]) or {}
        if own.get("session_type", "").lower() == "run":
            return _fields(own)
        for _, sess in runs:
            if any(
                k in (sess.get("focus") or "").lower() for k in _RUN_KEYWORDS["easy"]
            ):
                return _fields(sess)
        return (
            _fields(runs[0][1])
            if runs
            else {
                "session_type": "run",
                "focus": "easy run",
                "prescription": "",
                "notes": "",
            }
        )

    own = template.get(WEEKDAYS[on_date.weekday()]) or {}
    own_type = own.get("session_type", "").lower()
    if kind == "rest":
        if own_type == "rest":  # e.g. Saturday's "active recovery" stays as authored
            return _fields(own)
        return {
            "session_type": "rest",
            "focus": "rest",
            "prescription": "",
            "notes": "",
        }
    if kind == "cross":
        if own_type == "cross_train":
            return _fields(own)
        return {
            "session_type": "cross_train",
            "focus": "cross-train",
            "prescription": "",
            "notes": "",
        }
    raise PlanEditError(f"Unknown target {target!r}. Try: {VALID_TARGETS_HINT}.")


def parse_date(text: str, today: date, *, upcoming: bool = False) -> date:
    """'today' / 'tomorrow' / 'yesterday' / ISO date / weekday name.

    A weekday name means that day in the current Monday-Sunday week, or with
    upcoming=True the next occurrence on or after today. Writes use upcoming:
    "move it to Monday" said on a Wednesday means next Monday, not two days ago.
    """
    t = (text or "").strip().lower()
    if t in ("", "today"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "yesterday":
        return today - timedelta(days=1)
    if t in WEEKDAYS and upcoming:
        return today + timedelta(days=(WEEKDAYS.index(t) - today.weekday()) % 7)
    if t in WEEKDAYS:
        monday = today - timedelta(days=today.weekday())
        return monday + timedelta(days=WEEKDAYS.index(t))
    try:
        return date.fromisoformat(t)
    except ValueError:
        raise PlanEditError(
            f"Can't read date {text!r}. Use today, tomorrow, a weekday name, or YYYY-MM-DD."
        )


def split_target_and_date(text: str) -> tuple[str, str]:
    """'day b friday' -> ('day b', 'friday'); 'long run' -> ('long run', 'today').

    For the prefix-command form of /swap, where the whole line arrives as one
    string. The last token is the date only if it reads as one.
    """
    parts = (text or "").strip().split()
    if len(parts) >= 2:
        last = parts[-1].lower()
        if (
            last in ("today", "tomorrow")
            or last in WEEKDAYS
            or re.match(r"^\d{4}-\d{2}-\d{2}$", last)
        ):
            return " ".join(parts[:-1]), last
    return " ".join(parts), "today"


def _no_past(day: date, today: Optional[date]) -> None:
    if today is not None and day < today:
        raise PlanEditError(
            f"{day.isoformat()} is in the past; plan edits only change today or later. "
            "To make up a missed session, set the new day to it (e.g. 'day a' on Thursday)."
        )


# ── writes ──────────────────────────────────────────────────────────────────


async def _write(
    db, template: dict, day: date, fields: dict, note: str, source: str
) -> None:
    """Upsert an override, or clear it if it would just restate the template."""
    iso = day.isoformat()
    base = template.get(WEEKDAYS[day.weekday()])
    same = base is not None and all(
        (fields.get(k) or "") == (base.get(k) or "") for k in _FIELDS if k != "notes"
    )
    if same:
        await db.clear_daily_override(iso)
    else:
        # Lift notes are about the session (exercise priorities) and travel
        # with it. Run/rest notes are about where it sits in the week ("placed
        # after Day B on purpose") and would be wrong on another day.
        keep = fields["notes"] if fields["session_type"].lower() in LIFT_TYPES else ""
        await db.set_daily_override(
            date=iso,
            session_type=fields["session_type"],
            focus=fields["focus"],
            prescription=fields["prescription"],
            notes=" ".join(x for x in (keep, _EDIT_MARK, note) if x),
            source=source,
        )
    await db.clear_planned_session(iso)


async def _plan_and_template(db) -> tuple[dict, dict]:
    plan = await db.get_active_plan()
    if not plan:
        raise PlanEditError("No active training plan.")
    return plan, template_of(plan)


async def set_day(
    db, day: date, target: str, source: str, today: Optional[date] = None
) -> dict:
    """Set what `day` is (a template day, run, rest, ...) or reset it.

    Pass `today` to refuse past dates (overrides feed plan-adherence, so
    editing a past day rewrites what the brief says you skipped).
    """
    _no_past(day, today)
    _, template = await _plan_and_template(db)
    iso = day.isoformat()
    before = await db.get_effective_session_for_date(iso)

    parsed = parse_target(target)
    if parsed and parsed[0] == "reset":
        await db.clear_daily_override(iso)
        await db.clear_planned_session(iso)
    else:
        fields = resolve_target(template, target, day)
        await _write(
            db,
            template,
            day,
            fields,
            note=f"set to {target!r} via {source}",
            source=source,
        )

    after = await db.get_effective_session_for_date(iso)
    return {"date": iso, "before": summarize(before), "after": summarize(after)}


async def move_session(
    db, from_day: date, to_day: date, source: str, today: Optional[date] = None
) -> dict:
    """Swap the sessions on two dates.

    Moving Wednesday's Day B to Thursday puts Thursday's run on Wednesday,
    so nothing in the week is silently dropped. Exchanging is its own
    inverse: moving it back restores the template.
    """
    if from_day == to_day:
        raise PlanEditError("Both dates are the same; nothing to move.")
    _no_past(from_day, today)
    _no_past(to_day, today)
    _, template = await _plan_and_template(db)
    a_iso, b_iso = from_day.isoformat(), to_day.isoformat()
    a = await db.get_effective_session_for_date(a_iso)
    b = await db.get_effective_session_for_date(b_iso)
    a_f = (
        _fields(a)
        if a
        else {"session_type": "rest", "focus": "rest", "prescription": "", "notes": ""}
    )
    b_f = (
        _fields(b)
        if b
        else {"session_type": "rest", "focus": "rest", "prescription": "", "notes": ""}
    )

    await _write(
        db,
        template,
        to_day,
        a_f,
        note=f"moved from {a_iso} via {source}",
        source=source,
    )
    await _write(
        db,
        template,
        from_day,
        b_f,
        note=f"moved from {b_iso} via {source}",
        source=source,
    )

    return {
        "moved": summarize(a),
        "from": {
            "date": a_iso,
            "now": summarize(await db.get_effective_session_for_date(a_iso)),
        },
        "to": {
            "date": b_iso,
            "now": summarize(await db.get_effective_session_for_date(b_iso)),
        },
    }
