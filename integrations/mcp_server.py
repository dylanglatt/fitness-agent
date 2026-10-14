"""
MCP server for external agents (built for Meta's Muse; any MCP client works).

Mounted inside api_server.py, so it shares the API's process, SQLite DB, Coach
and bearer token. Public URL behind Caddy: https://<host>/api/muse/mcp

Tools are coarse on purpose: an agent asks "what's today", "what does my week
look like", "move Day B to tomorrow", "swap bench for incline". Every write goes
through the same code the Discord commands use (data/plan_ops.py and
Coach.edit_todays_session), so a change made from Muse is exactly what /plan,
/session and /liftstart show afterward.

Safety:
  * Refuses to mount without FITNESS_API_TOKEN (the rest of the API runs open
    in dev when the token is unset; this one has write tools, so it never does).
  * Writes are limited to plan overrides and today's planned session. Nothing
    here touches WHOOP/Strava tokens, logged lifts, or the weekly template.
  * Tools carry read-only / destructive hints so the client can require
    approval for writes.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from data import plan_ops
from data.plan_vocab import VALID_TARGETS_HINT, WEEKDAYS

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Dylan's personal training coach (his Discord bot): WHOOP recovery, Strava runs, "
    "logged lifts, goals, and his weekly training plan. The plan comes from the bot's "
    "active weekly template, which can change, so read it with get_week instead of "
    "assuming a split. Call get_today or get_week before changing anything. Use "
    "move_session when he wants to do a session on a different day, set_day to change "
    "what one day is, and swap_exercise for one exercise in today's lift. Dates are "
    "America/New_York."
)

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# move_session trades two days, so a retry undoes it: not idempotent.
_MOVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


class BearerGuard:
    """ASGI wrapper: 401 unless Authorization: Bearer <token> matches.

    Pure ASGI rather than BaseHTTPMiddleware so the streamable-HTTP responses
    pass through untouched.
    """

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self._expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b'{"detail":"unauthorized"}'}
                )
                return
        await self.app(scope, receive, send)


def build_mcp_server(
    db,
    get_coach: Callable[[], Any],
    now: Callable[[], datetime],
    *,
    progress: Callable[[], Awaitable[dict]] | None = None,
    on_plan_change: Callable[[list[str]], None] | None = None,
) -> MCPServer:
    """Register the tools. `now` must return tz-aware local time.

    `progress` returns goals + trends (api_server wires its /goals and
    /trends handlers in); `on_plan_change` gets the ISO dates a write touched
    so the API can drop caches (the cached morning brief).
    """
    mcp = MCPServer(name="coach-aurelius", instructions=INSTRUCTIONS)

    def _today() -> date:
        return now().date()

    def _date(text: str, upcoming: bool = True) -> date:
        try:
            return plan_ops.parse_date(text, _today(), upcoming=upcoming)
        except plan_ops.PlanEditError as e:
            raise ToolError(str(e)) from e

    def _changed(*isos: str) -> None:
        if on_plan_change:
            try:
                on_plan_change(list(isos))
            except Exception as e:  # cache invalidation must never fail a write
                logger.warning("on_plan_change failed: %s", e)

    async def _done_by_date(start: date, end: date) -> dict[str, dict]:
        """What was actually logged per date: lift exercises + Strava activities."""
        out: dict[str, dict] = {}
        days_back = max(1, (_today() - start).days + 1)
        for row in await db.get_recent_lifts(days=days_back):
            d = str(row.get("date"))[:10]
            if start.isoformat() <= d <= end.isoformat():
                ex = row.get("exercise") or ""
                # Strava auto-logs a placeholder "... Weight Training" lift row.
                if ex and not ex.lower().endswith("weight training"):
                    names = out.setdefault(d, {}).setdefault("lifts", [])
                    if ex not in names:
                        names.append(ex)
                else:
                    out.setdefault(d, {}).setdefault("lifts", [])
        for a in await db.get_strava_activities_range(
            start.isoformat(), end.isoformat()
        ):
            d = str(a.get("date"))[:10]
            out.setdefault(d, {}).setdefault("activities", []).append(
                {
                    "sport": a.get("sport_type"),
                    "name": a.get("name"),
                    "miles": round((a.get("distance_m") or 0) / 1609.34, 2) or None,
                    "minutes": round((a.get("moving_time_s") or 0) / 60) or None,
                    "avg_hr": a.get("average_hr"),
                }
            )
        return out

    # ── reads ───────────────────────────────────────────────────────────────

    @mcp.tool(annotations=_READ)
    async def get_today() -> dict:
        """Today's readiness and plan. Returns WHOOP recovery (score %, HRV ms,
        resting HR, sleep hours, strain), today's planned session (type, focus,
        Day A/B/C label, whether it was changed from the weekly template), and on
        lift days the exact exercises with sets, reps and target weights in lb.
        Call this before changing anything today so you use real exercise names.
        If a lift session is already in progress, exercise swaps won't reach it."""
        today = _today()
        iso = today.isoformat()
        rows = await db.get_whoop_daily((today - timedelta(days=3)).isoformat(), iso)
        latest = rows[0] if rows else {}
        sess = await db.get_effective_session_for_date(iso)
        out: dict[str, Any] = {
            "date": iso,
            "weekday": today.strftime("%A"),
            "recovery": {
                "as_of": latest.get("date"),
                "score_pct": latest.get("recovery_score"),
                "hrv_ms": latest.get("hrv_rmssd_ms"),
                "resting_hr": latest.get("resting_hr"),
                "sleep_hours": latest.get("total_asleep_hours"),
                "strain": latest.get("strain"),
            },
            "session": plan_ops.summarize(sess),
            "exercises": [],
            "lift_in_progress": bool(await db.get_active_lift_session()),
        }
        if plan_ops.is_lift(sess):
            saved = await db.get_planned_session(iso)
            if not (saved and saved.get("plan")):
                # Same pipeline as /session: computes weights from history and
                # persists (planned_sessions only), so /liftstart walks the
                # same list. A planner failure shouldn't sink the whole read.
                try:
                    await get_coach().generate_todays_session()
                except Exception as e:
                    logger.warning("get_today: session build failed: %s", e)
                    out["exercises_note"] = (
                        "Couldn't build today's exercise list; try /session in Discord."
                    )
                saved = await db.get_planned_session(iso)
            out["exercises"] = [
                {
                    k: e.get(k)
                    for k in (
                        "name",
                        "sets",
                        "reps",
                        "target_weight_lb",
                        "per_side",
                        "notes",
                    )
                }
                for e in (saved or {}).get("plan") or []
            ]
        if latest.get("date") != iso:
            out["recovery"][
                "note"
            ] = "Today's WHOOP recovery hasn't synced yet; this is the latest reading."
        return out

    @mcp.tool(annotations=_READ)
    async def get_week(week_of: str = "today") -> dict:
        """The Monday-Sunday week containing `week_of` (today, a weekday, or
        YYYY-MM-DD): each day's planned session (marking ones changed from the
        weekly template) next to what was actually logged (lift exercises,
        Strava activities). Use it to see what's been missed and where a moved
        session fits."""
        anchor = _date(week_of, upcoming=False)
        monday = anchor - timedelta(days=anchor.weekday())
        sunday = monday + timedelta(days=6)
        done = await _done_by_date(monday, sunday)
        days = []
        for i in range(7):
            d = monday + timedelta(days=i)
            iso = d.isoformat()
            days.append(
                {
                    "date": iso,
                    "weekday": WEEKDAYS[i].title(),
                    "planned": plan_ops.summarize(
                        await db.get_effective_session_for_date(iso)
                    ),
                    "done": done.get(iso, {}),
                }
            )
        return {
            "week_of": monday.isoformat(),
            "today": _today().isoformat(),
            "days": days,
        }

    @mcp.tool(annotations=_READ)
    async def get_progress() -> dict:
        """Active goals with progress toward each, plus 8-week trends: bench est. 1RM,
        weekly mileage, HRV, body weight, run intensity split. For 'how am I
        progressing' questions."""
        if progress is None:
            return {"error": "progress data not wired in this deployment"}
        return await progress()

    # ── writes ──────────────────────────────────────────────────────────────

    @mcp.tool(annotations=_MOVE)
    async def move_session(from_date: str, to_date: str) -> dict:
        """Move a session to another day. The two days TRADE sessions, so
        nothing in the week is dropped: moving Wednesday's Day B to Thursday
        puts Thursday's run on Wednesday, and moving one lift onto another lift
        day swaps the two lifts. Moving it back restores the template. Not
        idempotent: calling it twice undoes it. Dates: today, tomorrow,
        YYYY-MM-DD, or a weekday name (the next one, today included). Past
        dates are refused; to make up a missed session use set_day on the new
        day instead (e.g. target 'day a'). Use when Dylan says e.g. 'I'm
        running today instead, do the lift tomorrow'."""
        a, b = _date(from_date), _date(to_date)
        try:
            res = await plan_ops.move_session(db, a, b, source="muse", today=_today())
        except plan_ops.PlanEditError as e:
            # ToolError text reaches the agent; any other exception is hidden.
            raise ToolError(str(e)) from e
        _changed(a.isoformat(), b.isoformat())
        return res

    @mcp.tool(
        annotations=_WRITE,
        description=(
            "Change what one day is, without touching any other day. "
            f"target: {VALID_TARGETS_HINT}. "
            "'lift' means the lift that's owed (the latest template lift day on or before "
            "that weekday). 'reset' puts the day back to the weekly template. date: today, "
            "tomorrow, a weekday name in the current week, or YYYY-MM-DD. To shift a session "
            "to another day use move_session instead; to change one exercise use swap_exercise."
        ),
    )
    async def set_day(target: str, date: str = "today") -> dict:
        day = _date(date)
        try:
            res = await plan_ops.set_day(db, day, target, source="muse", today=_today())
        except plan_ops.PlanEditError as e:
            raise ToolError(str(e)) from e
        _changed(day.isoformat())
        return res

    @mcp.tool(annotations=_WRITE)
    async def swap_exercise(find: str, replacement: str) -> dict:
        """Swap or drop ONE exercise in TODAY's lift. find: part of the planned
        exercise name, e.g. 'bench' (call get_today for the list).
        replacement: the new exercise name, or 'remove' to drop it. The new
        exercise's sets, reps and weight are computed from Dylan's history.
        Today only; for other days change the day with set_day."""
        msg = await get_coach().edit_todays_session(find, replacement)
        _changed(_today().isoformat())
        saved = await db.get_planned_session(_today().isoformat())
        return {
            "result": msg,
            "exercises": [e.get("name") for e in (saved or {}).get("plan") or []],
        }

    return mcp


def mount_mcp(
    app, mcp: MCPServer, token: str, allowed_hosts: list[str], path: str = "/muse"
):
    """Mount the MCP endpoint at `{path}/mcp` behind the bearer guard.

    Returns the session manager; the host app's lifespan must enter
    `session_manager.run()`.
    """
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[],
    )
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    app.mount(path, BearerGuard(inner, token))
    return mcp.session_manager
