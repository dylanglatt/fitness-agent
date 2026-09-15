"""
Race-date-driven periodization — pure functions, no DB, no LLM.

The gap this closes: training_phases already has a "focus" field with values
like "marathon" / "base" / "taper" that _build_session_plan() reads to decide
whether lifting is primary or maintenance-only — but nothing ever computed
those values. A phase was a hand-authored blob with manually typed
start_date/end_date/focus, so the coach treated week 1 and week 15 of
marathon prep identically unless Dylan remembered to go edit a database row.

This module answers one question deterministically: given a goal race date
and today, what training block are we in (base/build/peak/taper/race_week/
post_race), and what does that mean for leg-lifting volume and weekly
mileage? ai/coach.py wires the result into the existing is_primary check and
into the brief prompt — this module itself touches neither the DB nor the
LLM, so it's fully unit-testable the same way training_state.py is.

Deliberately NOT included: "base" does not deprioritize lifting the way the
old manual training_phases check did. Early in a marathon build, running
volume is lower and that is exactly when strength work should stay primary
— only taper and race week, when fresh legs matter more than a strength
stimulus, pull lifting back to maintenance.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

BASE, BUILD, PEAK, TAPER, RACE_WEEK, POST_RACE = (
    "base", "build", "peak", "taper", "race_week", "post_race",
)

# Days-out thresholds by race distance. Defaults (used for an unknown
# distance string) match marathon — the longest, most conservative taper.
_TAPER_DAYS = {"5k": 5, "10k": 7, "half_marathon": 10, "marathon": 21}
_PEAK_DAYS = {"5k": 7, "10k": 10, "half_marathon": 14, "marathon": 21}
_BUILD_DAYS = {"5k": 21, "10k": 28, "half_marathon": 42, "marathon": 70}
_RACE_WEEK_DAYS = {"5k": 2, "10k": 3, "half_marathon": 4, "marathon": 6}
_POST_RACE_DAYS = {"5k": 3, "10k": 5, "half_marathon": 7, "marathon": 14}


def _lookup(table: dict, distance: str) -> int:
    return table.get((distance or "").strip().lower(), table["marathon"])


def days_to_race(today: date, race_date: date) -> int:
    return (race_date - today).days


def training_block(days_out: int, distance: str = "marathon") -> str:
    """Which block `days_out` days before the race falls into.

    `days_out` is negative once the race has passed. Pure integer math —
    same distance always gives the same block for the same days_out.
    """
    race_week = _lookup(_RACE_WEEK_DAYS, distance)
    taper = _lookup(_TAPER_DAYS, distance)
    peak = _lookup(_PEAK_DAYS, distance)
    build = _lookup(_BUILD_DAYS, distance)
    post = _lookup(_POST_RACE_DAYS, distance)

    if days_out < 0:
        return POST_RACE if -days_out <= post else BASE
    if days_out <= race_week:
        return RACE_WEEK
    if days_out <= taper:
        return TAPER
    if days_out <= taper + peak:
        return PEAK
    if days_out <= taper + peak + build:
        return BUILD
    return BASE


def is_lifting_primary(block: str) -> bool:
    """False only when fresh legs matter more than a strength stimulus.

    Base and build keep lifting primary on purpose — see module docstring.
    """
    return block not in (TAPER, RACE_WEEK)


_LEG_GUIDANCE = {
    BASE: "normal leg volume — build strength while running volume is lower",
    BUILD: "normal to slightly reduced leg volume as mileage climbs",
    PEAK: "moderate leg volume — protect quality/long run days",
    TAPER: "light leg volume only — priority is fresh legs for race day",
    RACE_WEEK: "no new leg stimulus — bodyweight/mobility only",
    POST_RACE: "no leg lifting until soreness clears, then resume gradually",
}


def leg_lift_guidance(block: str) -> str:
    return _LEG_GUIDANCE.get(block, _LEG_GUIDANCE[BASE])


def weekly_mileage_guidance(block: str, peak_weekly_mi: Optional[float] = None) -> str:
    """Plain-language weekly mileage target for the current block.

    Gives concrete numbers when a peak weekly mileage is on file (set via
    scripts/set_goal_race.py); otherwise a relative description the coach can
    still act on.
    """
    if peak_weekly_mi:
        p = peak_weekly_mi
        by_block = {
            BASE: f"~{p * 0.5:.0f}-{p * 0.6:.0f} mi/wk, building ~10%/wk toward peak",
            BUILD: f"climbing toward peak ({p:.0f} mi/wk)",
            PEAK: f"~{p:.0f} mi/wk — this is the top of the build",
            TAPER: f"~{p * 0.6:.0f}-{p * 0.7:.0f} mi/wk, dropping toward race day",
            RACE_WEEK: f"~{p * 0.2:.0f}-{p * 0.3:.0f} mi/wk plus the race itself",
            POST_RACE: "easy/rest — resume base building in 1-2 weeks",
        }
    else:
        by_block = {
            BASE: "building aerobic volume ~10%/wk, no cap set yet",
            BUILD: "climbing weekly mileage toward this cycle's peak",
            PEAK: "highest volume of the cycle",
            TAPER: "dropping volume while keeping some intensity",
            RACE_WEEK: "sharply reduced volume plus the race itself",
            POST_RACE: "easy/rest — resume base building in 1-2 weeks",
        }
    return by_block.get(block, by_block[BASE])


def render_race_periodization_block(
    race_name: str,
    race_date_iso: str,
    distance: str,
    today: date,
    peak_weekly_mi: Optional[float] = None,
) -> str:
    """Render the periodization state for the brief/chat prompt context."""
    try:
        rd = datetime.fromisoformat(race_date_iso[:10]).date()
    except ValueError:
        return ""
    out = days_to_race(today, rd)
    block = training_block(out, distance)
    weeks = abs(out) / 7.0
    when = f"{out} days out" if out >= 0 else f"{-out} days past race"
    lines = [
        f"RACE PERIODIZATION (computed from goal race date, {distance.replace('_', ' ')}):",
        f"  {race_name}: {when} ({weeks:.1f} weeks) — {block.upper()} phase",
        f"  Legs: {leg_lift_guidance(block)}",
        f"  Running volume: {weekly_mileage_guidance(block, peak_weekly_mi)}",
    ]
    if not is_lifting_primary(block):
        lines.append(
            "  Lifting is on MAINTENANCE this block (fewer sets) — fresh legs "
            "matter more than a strength stimulus right now."
        )
    return "\n".join(lines)
