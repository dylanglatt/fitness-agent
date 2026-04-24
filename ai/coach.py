"""
AI Coach — orchestrates data fetching, prompt construction, and Claude calls.

Architecture notes (why this file is shaped the way it is):

1. History lives in SQLite, not in the prompt.
   The backfill (sync_history.py) and the nightly scheduler job keep the
   whoop_recovery / whoop_sleep / whoop_cycle / strava_activities tables
   current. We read from those tables — we do NOT re-fetch from WHOOP / Strava
   on every chat turn. That used to be the slow part and the expensive part.

2. The prompt shows a layered view, not raw history.
   _build_layered_context() returns 7 days of detail, 30 days of aggregates,
   and a 1-year baseline. That's ~1K tokens, not 90K. If Claude needs more,
   it asks for it via tools.

3. Claude has tools for on-demand lookups.
   query_daily_metrics / query_activities / query_lifts / get_whoop_aggregates /
   get_strava_aggregates are wired into chat(). A trend question like "how was
   my HRV in February?" turns into Claude calling get_whoop_aggregates for that
   window, getting back ~100 tokens of numbers, and answering from them.

4. Lift-parse is pre-filtered with regex.
   Sending a Haiku/Sonnet round-trip on every "hello" to ask "is this a lift?"
   is pure waste. We skip the model call entirely unless the message looks
   numeric enough to plausibly be a set/rep/weight line.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic

from ai.prompts import (
    SYSTEM_PROMPT,
    DAILY_BRIEF_PROMPT,
    WEEKLY_SUMMARY_PROMPT,
    SUNDAY_REFLECTION_PROMPT,
    CHAT_PROMPT,
    get_daily_stoic_quote,
)
from integrations.strava import StravaClient
from integrations.whoop import WhoopClient
from integrations.notion import NotionClient
from integrations.weather import WeatherClient

logger = logging.getLogger(__name__)


# Try to load knowledge retriever — gracefully skips if not set up yet
try:
    from ai.knowledge_retriever import KnowledgeRetriever
    _retriever = KnowledgeRetriever()
except ImportError:
    _retriever = None
    logger.info("Knowledge retriever not available — run ingest_knowledge.py to enable RAG.")


# ── Unit helpers (user prefers miles/pounds) ────────────────────────────────
def _m_to_mi(meters):
    return round((meters or 0) / 1609.344, 2) if meters else 0.0


def _mps_to_min_per_mi(mps):
    """Convert m/s → min:sec per mile string."""
    if not mps or mps <= 0:
        return None
    sec_per_mi = 1609.344 / mps
    m, s = divmod(int(sec_per_mi), 60)
    return f"{m}:{s:02d}/mi"


def _m_to_ft(meters):
    return round((meters or 0) * 3.28084, 0) if meters else 0


# ── Regex — does this message even plausibly contain a lift? ────────────────
_LIFT_HINT = re.compile(
    r"""
    (\d+\s*[xX×]\s*\d+)                 # 3x10, 4x8
    | (\d+\s*(reps?|sets?)\b)           # 10 reps, 3 sets
    | (\b(bench|squat|deadlift|press|curl|row|pull[-\s]?up|chin[-\s]?up|
           clean|snatch|hinge|lunge|dip|ohp)\b)  # common lift names
    | (@\s*\d+)                         # @ 185
    | (\d{2,4}\s*(lb|lbs|kg))           # 185 lbs, 225lb
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ── Regex — is Dylan asking for a workout debrief? ──────────────────────────
# Matches phrasings like "debrief my run", "break down the run", "how'd my
# workout go", "tell me about that ride", "analyze my lift". Kept broad so
# the router can catch intent without Claude needing a tool-call round-trip.
_DEBRIEF_INTENT = re.compile(
    r"""
    \b(
        debrief
      | break[\s\-]?down
      | how('d| did)\s+(my|that|the)
      | tell\s+me\s+about\s+(my|that|the)
      | (how\s+was|what\s+was)\s+(my|that|the)
      | (analy[sz]e|recap|review|read)\s+(my|that|the|this)
      | post[\s\-]?(mortem|workout|run|game)
    )\b
    .{0,40}?
    \b(run|ride|cycle|workout|session|lift|training|activity|hike|ruck|game)\b
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


# ── Regex — does this message plausibly describe a recovery session? ────────
# sauna, steam, cold plunge, ice bath, contrast bath, cryo, sensory-dep.
_RECOVERY_HINT = re.compile(
    r"\b("
    r"sauna|"
    r"steam\s*room|steamroom|"
    r"cold\s*plunge|cold[-\s]?tub|ice\s*bath|plunge|"
    r"contrast\s*(bath|therapy)|"
    r"cryo(therapy)?|cryo\s*chamber|"
    r"hot\s*tub|jacuzzi"
    r")\b",
    re.IGNORECASE,
)


# ── Claude tools — what the coach can query on demand ───────────────────────

TOOLS = [
    {
        "name": "query_daily_metrics",
        "description": (
            "Fetch Dylan's daily WHOOP metrics (recovery_score, hrv_rmssd_ms, "
            "resting_hr, sleep hours, sleep efficiency, strain) for a date range. "
            "Use this when he asks about specific days or wants to see day-by-day "
            "numbers. Max ~90 days per call to keep responses focused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_whoop_aggregates",
        "description": (
            "Get averages/min/max for WHOOP metrics over a date range. Use this "
            "for trend questions like 'how was my HRV in February' or 'what's my "
            "baseline recovery this year'. Returns one summary row, not daily "
            "detail — cheap and fast."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "query_activities",
        "description": (
            "List Strava activities in a date range. Optionally filter by "
            "sport_type (e.g. 'Run', 'Ride', 'WeightTraining'). Returns distance "
            "in miles, time, avg/max HR, elevation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "sport_type": {
                    "type": "string",
                    "description": "Optional filter. Strava sport_type value.",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_strava_aggregates",
        "description": (
            "Totals over a range: activity count, total miles, total hours, total "
            "elevation gain (feet), and counts broken down by sport. For trend/"
            "volume questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "query_lifts",
        "description": (
            "Fetch Dylan's self-reported lift history for a specific exercise "
            "(e.g. 'bench', 'squat'). Returns the last N entries, most recent "
            "first. Use for progression questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {
                    "type": "string",
                    "description": "Exercise name — matched with LIKE, partial is fine.",
                },
                "limit": {"type": "integer", "description": "Max rows (default 20)"},
            },
            "required": ["exercise"],
        },
    },
    {
        "name": "query_correlated_runs",
        "description": (
            "PREFER THIS for any running-performance trend question (pace over "
            "time, HR drift, zone distribution, how fitness is progressing, "
            "are easy runs actually easy, etc.). Returns each Strava run in "
            "the date range joined with its matching WHOOP workout — so you "
            "get Strava pace/distance/elevation AND WHOOP HR + Z1–Z5 time + "
            "workout strain side-by-side per run. Dylan's Strava runs come "
            "from WHOOP, so the match rate is near 100% when both sources "
            "have the session. whoop_avg_hr / whoop_max_hr / whoop_z*_min "
            "will be null for runs that weren't captured on WHOOP. Defaults "
            "to sport_type='Run' but accepts any Strava sport_type."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "sport_type": {
                    "type": "string",
                    "description": "Strava sport_type (default 'Run').",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
]


class Coach:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.strava = StravaClient(config)
        self.whoop = WhoopClient(config)
        self.notion = NotionClient(config)
        self.weather = WeatherClient(config)
        self.claude = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        self.model = config.CLAUDE_MODEL
        # Haiku is ~10x cheaper — great for the "is this a lift?" classifier.
        self.cheap_model = "claude-haiku-4-5-20251001"

        # Conversation history for multi-turn chat
        self._conversation: list[dict] = []

    # ── Tool execution — called when Claude asks for data ───────────────────

    async def _execute_tool(self, name: str, args: dict) -> str:
        """Run a tool call and return a JSON string Claude can read."""
        try:
            if name == "query_daily_metrics":
                rows = await self.db.get_whoop_daily(args["start_date"], args["end_date"])
                return json.dumps(rows, default=str)
            if name == "get_whoop_aggregates":
                agg = await self.db.get_whoop_aggregates(args["start_date"], args["end_date"])
                return json.dumps(agg, default=str)
            if name == "query_activities":
                rows = await self.db.get_strava_activities_range(
                    args["start_date"], args["end_date"], args.get("sport_type")
                )
                # Convert units for Dylan's preference (miles, ft)
                for r in rows:
                    r["distance_mi"] = _m_to_mi(r.pop("distance_m", 0))
                    r["elevation_ft"] = _m_to_ft(r.pop("total_elevation_gain_m", 0))
                    r["pace"] = _mps_to_min_per_mi(r.pop("average_speed_mps", 0))
                    if r.get("moving_time_s") is not None:
                        r["duration_min"] = round(r.pop("moving_time_s") / 60, 1)
                return json.dumps(rows, default=str)
            if name == "get_strava_aggregates":
                agg = await self.db.get_strava_aggregates(args["start_date"], args["end_date"])
                agg["total_distance_mi"] = _m_to_mi((agg.pop("total_distance_km", 0) or 0) * 1000)
                agg["total_elevation_ft"] = _m_to_ft(agg.pop("total_elevation_m", 0))
                return json.dumps(agg, default=str)
            if name == "query_lifts":
                rows = await self.db.get_lifts_for_exercise(
                    args["exercise"], args.get("limit", 20)
                )
                return json.dumps(rows, default=str)
            if name == "query_correlated_runs":
                rows = await self.db.get_correlated_runs_in_range(
                    args["start_date"],
                    args["end_date"],
                    args.get("sport_type", "Run"),
                )
                return json.dumps(rows, default=str)
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"unknown tool: {name}"})

    # ── Claude call with tool-use loop ──────────────────────────────────────

    async def _ask_claude(
        self,
        user_message: str,
        *,
        use_history: bool = False,
        allow_tools: bool = False,
        max_tokens: int = 1024,
    ) -> str:
        """Send a message to Claude, handling any tool calls it makes."""
        messages: list = list(self._conversation) if use_history else []
        messages.append({"role": "user", "content": user_message})

        create_kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        if allow_tools:
            create_kwargs["tools"] = TOOLS

        # Tool-use loop: keep responding to tool_use blocks until Claude
        # returns plain text (stop_reason != "tool_use"). Cap iterations so a
        # buggy model can't spin forever.
        for _ in range(6):
            response = await self.claude.messages.create(**create_kwargs)

            if response.stop_reason != "tool_use":
                # Plain text response — extract and return.
                text_parts = [
                    b.text for b in response.content if getattr(b, "type", None) == "text"
                ]
                reply = "".join(text_parts).strip()
                if use_history:
                    self._conversation.append({"role": "user", "content": user_message})
                    self._conversation.append({"role": "assistant", "content": reply})
                    if len(self._conversation) > 40:
                        self._conversation = self._conversation[-40:]
                return reply

            # Claude wants to call one or more tools. Append the assistant
            # turn (the SDK serializes ContentBlock objects correctly when
            # passed back) and then the tool_result turn, then loop.
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    logger.info(f"Claude tool call: {block.name}({block.input})")
                    result = await self._execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            create_kwargs["messages"] = messages

        logger.warning("Claude tool-use loop hit max iterations; returning empty.")
        return "Sorry, I got stuck looking things up. Try asking again."

    # ── Trend helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _mean(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    @staticmethod
    def _describe_delta(recent, baseline, good_direction: str = "up") -> str:
        """Return 'rising/stable/declining' label based on recent vs baseline."""
        if recent is None or baseline is None or baseline == 0:
            return "n/a"
        pct = (recent - baseline) / baseline * 100
        # ±5% = "stable"; bigger moves get a direction.
        if abs(pct) < 5:
            return "stable"
        if pct > 0:
            return "rising" if good_direction == "up" else "elevated"
        return "declining" if good_direction == "up" else "lowered"

    def _compute_trend_signals(
        self,
        daily_7d: list[dict],
        agg30: dict,
        agg365: dict,
        acts_7d: list[dict],
        acts_28d: list[dict],
    ) -> list[str]:
        """Derive the summary trend lines the model should reason over.

        The daily-brief prompt no longer asks the model to infer trend from
        the day-by-day table alone — we compute it here and hand it over
        explicitly, so the model can focus on the coaching decision.
        """
        lines: list[str] = []
        if not daily_7d:
            return lines

        # daily_7d comes ordered DESC by date. Slice carefully.
        last3 = daily_7d[:3]
        prior4 = daily_7d[3:7]

        hrv_recent = self._mean([d.get("hrv_rmssd_ms") for d in last3])
        hrv_prior = self._mean([d.get("hrv_rmssd_ms") for d in prior4])
        rec_recent = self._mean([d.get("recovery_score") for d in last3])
        rec_prior = self._mean([d.get("recovery_score") for d in prior4])

        baseline_hrv = round(agg365.get("avg_hrv") or 0, 1) if agg365 else None
        baseline_rec = int(agg365.get("avg_recovery") or 0) if agg365 else None
        avg30_rec = int(agg30.get("avg_recovery") or 0) if agg30 else None

        hrv_slope = self._describe_delta(hrv_recent, hrv_prior)
        rec_slope = self._describe_delta(rec_recent, rec_prior)

        lines.append(
            f"HRV trend: last-3d avg {hrv_recent}ms vs prior-4d avg {hrv_prior}ms "
            f"→ {hrv_slope} (12-mo baseline {baseline_hrv}ms)"
        )
        lines.append(
            f"Recovery trend: last-3d avg {rec_recent}% vs prior-4d avg {rec_prior}% "
            f"→ {rec_slope} (12-mo baseline {baseline_rec}%)"
        )

        # Flag when 30d recovery is meaningfully below baseline.
        if avg30_rec and baseline_rec:
            gap = baseline_rec - avg30_rec
            if gap >= 5:
                lines.append(
                    f"⚠ 30-day recovery avg ({avg30_rec}%) is {gap} pts below "
                    f"12-month baseline ({baseline_rec}%) — accumulated fatigue signal."
                )
            elif gap <= -5:
                lines.append(
                    f"↑ 30-day recovery avg ({avg30_rec}%) is {-gap} pts above "
                    f"12-month baseline — well-adapted."
                )

        # Red/green day count in the last 7 days.
        rec_scores = [d.get("recovery_score") for d in daily_7d if d.get("recovery_score") is not None]
        if rec_scores:
            red = sum(1 for r in rec_scores if r < 34)
            green = sum(1 for r in rec_scores if r > 66)
            lines.append(
                f"Last 7 days: {green} green / {len(rec_scores) - green - red} yellow / {red} red recovery days."
            )

        # 7-day activity composition (Strava sport_type breakdown).
        if acts_7d:
            by_sport: dict[str, int] = {}
            for a in acts_7d:
                sp = a.get("sport_type") or "Other"
                by_sport[sp] = by_sport.get(sp, 0) + 1
            breakdown = ", ".join(f"{v} {k}" for k, v in sorted(by_sport.items(), key=lambda x: -x[1]))
            rest_days = 7 - len({a.get("date") for a in acts_7d if a.get("date")})
            lines.append(f"Last 7 days activities: {breakdown} ({rest_days} rest days).")

        # Acute:chronic workload ratio on strain (7d avg / 28d avg).
        # >1.5 is the conventional injury-risk red zone.
        strain_7d = self._mean([d.get("strain") for d in daily_7d])
        strain_28d_vals = [a.get("strain") for a in acts_28d if a.get("strain") is not None]
        # acts_28d here is actually the 28d whoop rows we pass in; see caller.
        strain_28d = self._mean(strain_28d_vals) if strain_28d_vals else None
        if strain_7d and strain_28d and strain_28d > 0:
            acwr = round(strain_7d / strain_28d, 2)
            flag = ""
            if acwr >= 1.5:
                flag = " ⚠ above injury-risk threshold (>1.5)"
            elif acwr <= 0.8:
                flag = " (detraining zone, <0.8)"
            lines.append(
                f"Acute:chronic strain ratio (7d/28d): {acwr} "
                f"[7d avg {strain_7d}, 28d avg {strain_28d}]{flag}"
            )

        return lines

    # ── Layered context builder ─────────────────────────────────────────────

    async def _build_layered_context(self) -> str:
        """Assemble a compact, layered view of Dylan's recent state.

        Shape:
          - TODAY (live WHOOP snapshot)
          - TRENDS (HRV/recovery slope, baselines, ACWR, activity composition)
          - LAST 7 DAYS (day-by-day detail)
          - LAST 30 DAYS / 12-MONTH BASELINE (aggregates)
          - Recent Strava activities (last 14 days)
          - Recent lifts + notes (last 14 days)

        The TRENDS block is the new load-bearing piece: it replaces the
        old "infer trend from the daily table" work the model used to do
        implicitly — often poorly. Now the model gets pre-computed slopes
        and can spend its tokens on the coaching decision.
        """
        today = datetime.now().date()
        d7 = today - timedelta(days=7)
        d28 = today - timedelta(days=28)
        d30 = today - timedelta(days=30)
        d365 = today - timedelta(days=365)
        d14 = today - timedelta(days=14)

        lines: list[str] = []
        lines.append(f"TODAY: {today.strftime('%A, %B %d, %Y')}")

        # ── Today snapshot (live, since nightly sync may not have today yet)
        try:
            snap = await self.whoop.get_today_snapshot()
            rec_line = self.whoop.summarize_recovery(snap.get("recovery"))
            slp_line = self.whoop.summarize_sleep(snap.get("sleep"))
            lines.append(f"  {rec_line}")
            lines.append(f"  {slp_line}")
        except Exception as e:
            logger.warning(f"Live WHOOP snapshot unavailable, falling back to DB: {e}")
            # Fall back to whatever is in SQLite for today
            rows = await self.db.get_whoop_daily(str(today), str(today))
            if rows:
                r = rows[0]
                lines.append(
                    f"  Recovery: {r.get('recovery_score')}% | "
                    f"HRV: {r.get('hrv_rmssd_ms')}ms | "
                    f"RHR: {r.get('resting_hr')} bpm"
                )

        # ── Active training plan — today's session
        # Placed high in the context because it's the primary driver of the
        # brief's prescription: the coach delivers today's planned session
        # and modulates intensity based on recovery, rather than inventing
        # a session from scratch.
        try:
            plan = await self.db.get_active_plan()
        except Exception as e:
            logger.warning(f"Active plan lookup failed: {e}")
            plan = None
        if plan:
            day_name = today.strftime("%A").lower()
            session = (plan.get("weekly_template") or {}).get(day_name)
            lines.append("")
            lines.append(f"ACTIVE PLAN: {plan.get('name')}")
            lines.append(f"  Goal: {plan.get('goal')}")
            if session:
                stype = session.get("session_type", "?")
                focus = session.get("focus", "")
                presc = session.get("prescription", "")
                notes = session.get("notes", "")
                lines.append(
                    f"  Today ({day_name}): {stype.upper()} — {focus}"
                )
                if presc:
                    lines.append(f"  Prescription: {presc}")
                if notes:
                    lines.append(f"  Scheduling notes: {notes}")
            else:
                lines.append(f"  Today ({day_name}): no session defined in template")

        # ── Weather block (forecast + air quality for home location)
        try:
            weather_block = await self.weather.summarize_today()
        except Exception as e:
            logger.warning(f"Weather summary failed: {e}")
            weather_block = ""
        if weather_block:
            lines.append("")
            lines.append(weather_block)

        # ── Load everything we need for trends + detail
        daily = await self.db.get_whoop_daily(str(d7), str(today))
        daily_28 = await self.db.get_whoop_daily(str(d28), str(today))
        agg30 = await self.db.get_whoop_aggregates(str(d30), str(today))
        agg365 = await self.db.get_whoop_aggregates(str(d365), str(today))
        acts_7d = await self.db.get_strava_activities_range(str(d7), str(today))

        # ── TRENDS block (pre-computed so the model doesn't have to infer)
        trend_lines = self._compute_trend_signals(daily, agg30, agg365, acts_7d, daily_28)
        if trend_lines:
            lines.append("")
            lines.append("TRENDS:")
            for t in trend_lines:
                lines.append(f"  {t}")

        # ── Last 7 days detail
        if daily:
            lines.append("")
            lines.append("LAST 7 DAYS (WHOOP):")
            for r in daily:
                parts = [r.get("date")]
                if r.get("recovery_score") is not None:
                    parts.append(f"rec {int(r['recovery_score'])}%")
                if r.get("hrv_rmssd_ms") is not None:
                    parts.append(f"hrv {r['hrv_rmssd_ms']}ms")
                if r.get("resting_hr") is not None:
                    parts.append(f"rhr {int(r['resting_hr'])}")
                if r.get("strain") is not None:
                    parts.append(f"strain {round(r['strain'], 1)}")
                if r.get("total_asleep_hours") is not None:
                    parts.append(f"sleep {r['total_asleep_hours']}h")
                lines.append("  " + " | ".join(parts))

        # ── Last 30 days aggregate
        if agg30 and agg30.get("days"):
            lines.append("")
            lines.append(
                f"LAST 30 DAYS AVG: recovery {int(agg30['avg_recovery'] or 0)}% "
                f"(range {int(agg30['min_recovery'] or 0)}–{int(agg30['max_recovery'] or 0)}) | "
                f"HRV {round(agg30['avg_hrv'] or 0, 1)}ms | "
                f"RHR {round(agg30['avg_rhr'] or 0, 1)} | "
                f"sleep {round(agg30['avg_sleep_hours'] or 0, 1)}h | "
                f"avg strain {round(agg30['avg_strain'] or 0, 1)}"
            )

        # ── 12-month baseline
        if agg365 and agg365.get("days"):
            lines.append(
                f"12-MONTH BASELINE ({agg365['days']} days on record): "
                f"recovery {int(agg365['avg_recovery'] or 0)}% | "
                f"HRV {round(agg365['avg_hrv'] or 0, 1)}ms | "
                f"RHR {round(agg365['avg_rhr'] or 0, 1)}"
            )

        # ── Strava: last 14 days detail + 30/365 aggregates
        # We merge in WHOOP per-session workouts so HR fields read from the
        # authoritative source when available (Strava's `average_hr` can be
        # off or missing; WHOOP strap measures HR directly). Per-activity
        # strain also comes from WHOOP workouts — day-level /v2/cycle strain
        # is 24h-aggregate and wrong for grading a single session.
        acts = await self.db.get_strava_activities_range(str(d14), str(today))
        # Pull the 14-day window of per-session WHOOP workouts so we can
        # substitute WHOOP's HR where Strava's is missing or wrong. This is
        # the fix for the old "0.6 strain, max HR 110 for a real run" bug:
        # those numbers came from the DAY-level /v2/cycle table, not a real
        # per-session workout record.
        try:
            whoop_wos_14d = await self.db.get_whoop_workouts_in_window(
                f"{d14}T00:00:00.000Z",
                f"{today}T23:59:59.999Z",
            )
        except Exception as e:
            logger.debug(f"WHOOP workouts-in-window fetch failed: {e}")
            whoop_wos_14d = []

        def _whoop_overlap(a: dict) -> Optional[dict]:
            """Find a WHOOP workout whose window overlaps this Strava activity."""
            a_raw = {}
            try:
                a_raw = json.loads(a.get("raw_json") or "{}") if isinstance(
                    a.get("raw_json"), str
                ) else (a.get("raw_json") or {})
            except Exception:
                a_raw = {}
            a_start_iso = a_raw.get("start_date") or a.get("start_date")
            if not a_start_iso:
                return None
            try:
                a_start = datetime.fromisoformat(
                    a_start_iso.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except Exception:
                return None
            a_end = a_start + timedelta(
                seconds=(a.get("elapsed_time_s") or a.get("moving_time_s") or 0)
            )
            for w in whoop_wos_14d:
                try:
                    w_start = datetime.fromisoformat(
                        (w.get("start_utc") or "").replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    w_end = datetime.fromisoformat(
                        (w.get("end_utc") or "").replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                except Exception:
                    continue
                if not (w_end + timedelta(minutes=10) < a_start
                        or a_end + timedelta(minutes=10) < w_start):
                    return w
            return None

        if acts:
            lines.append("")
            lines.append("LAST 14 DAYS (Strava + WHOOP workout HR):")
            for a in acts[:15]:
                mi = _m_to_mi(a.get("distance_m", 0))
                dur = round((a.get("moving_time_s") or 0) / 60, 1)
                parts = [a.get("date"), a.get("sport_type") or "?", f"{dur}min"]
                if mi:
                    parts.append(f"{mi}mi")
                matched = _whoop_overlap(a)
                # Prefer WHOOP HR when available; fall back to Strava's HR.
                avg_hr = (matched or {}).get("average_hr") or a.get("average_hr")
                max_hr = (matched or {}).get("max_hr") or a.get("max_hr")
                if avg_hr:
                    parts.append(f"avg HR {int(avg_hr)}")
                if max_hr:
                    parts.append(f"max HR {int(max_hr)}")
                if matched and matched.get("strain") is not None:
                    parts.append(f"WHOOP strain {round(matched['strain'], 1)}")
                lines.append("  - " + " | ".join(parts))

        s30 = await self.db.get_strava_aggregates(str(d30), str(today))
        if s30 and s30.get("activity_count"):
            total_mi = round((s30.get("total_distance_km") or 0) * 0.621371, 1)
            total_hrs = round(s30.get("total_hours") or 0, 1)
            lines.append(
                f"LAST 30 DAYS (Strava): {s30['activity_count']} activities | "
                f"{total_mi} mi | {total_hrs}h moving"
            )
            if s30.get("by_sport"):
                by_sport = [
                    f"{r['sport_type']} ×{r['n']}" for r in s30["by_sport"][:5]
                ]
                lines.append("  breakdown: " + ", ".join(by_sport))

        # ── Lifts + notes (last 14 days)
        lifts = await self.db.get_recent_lifts(days=14)
        notes = await self.db.get_recent_notes(days=14)
        if lifts:
            lines.append("")
            lines.append("RECENT LIFTS (self-reported, last 14 days):")
            for l in lifts[:15]:
                lines.append(f"  - {l['date']} | {l['exercise']} | {l['details']}")

        # ── Recovery sessions (sauna / plunge / etc.) — last 14 days
        try:
            recovery_sessions = await self.db.get_recent_recovery_sessions(days=14)
        except Exception as e:
            logger.debug(f"Could not load recovery sessions: {e}")
            recovery_sessions = []
        if recovery_sessions:
            lines.append("")
            lines.append("RECENT RECOVERY SESSIONS (self-reported, last 14 days):")
            for r in recovery_sessions[:20]:
                parts = [r["date"], r["session_type"]]
                if r.get("duration_min") is not None:
                    parts.append(f"{r['duration_min']:g} min")
                if r.get("temp_f") is not None:
                    parts.append(f"{r['temp_f']:g}°F")
                if r.get("notes"):
                    parts.append(r["notes"])
                lines.append("  - " + " | ".join(str(p) for p in parts))

        if notes:
            lines.append("")
            lines.append("RECENT NOTES (last 14 days):")
            for n in notes[:10]:
                lines.append(f"  - {n['date']}: {n['content']}")

        lines.append("")
        lines.append(
            "NOTE: Full history is in SQLite. Use tools "
            "(query_daily_metrics, get_whoop_aggregates, query_activities, "
            "get_strava_aggregates, query_lifts) to answer questions about "
            "specific past periods — don't guess from memory."
        )
        return "\n".join(lines)

    def _retrieve_knowledge(self, query: str) -> str:
        if _retriever is None:
            return ""
        return _retriever.retrieve(query)

    # ── Public coach methods ────────────────────────────────────────────────

    async def daily_brief(self) -> str:
        context = await self._build_layered_context()
        stoic_quote = get_daily_stoic_quote()
        prompt = DAILY_BRIEF_PROMPT.format(data=context, stoic_quote=stoic_quote)
        brief = await self._ask_claude(prompt, allow_tools=False)

        # Best-effort Notion log. Two writes now — one daily-summary row, plus
        # one row per Strava activity that came in since yesterday. Each write
        # is independently try/except'd so a failure on one doesn't skip the
        # others; we care more about "something got logged" than "everything
        # was perfect" on the morning-brief path.
        try:
            snapshot = await self.whoop.get_today_snapshot()
            rec = snapshot.get("recovery", {}) or {}
            slp = snapshot.get("sleep", {}) or {}
            activities = await self.strava.get_recent_activities(days=1)

            score = rec.get("score", {}) if rec else {}
            sleep_score = slp.get("score", {}) if slp else {}
            stage_summary = sleep_score.get("stage_summary", {}) if sleep_score else {}

            # 1) Daily row (WHOOP + brief text). No activities list here —
            # Strava activities become their own rows in the Workouts DB below.
            await self.notion.log_daily_entry(
                date=datetime.now().strftime("%Y-%m-%d"),
                summary={
                    "recovery_score": score.get("recovery_score"),
                    "hrv": round(score.get("hrv_rmssd_milli", 0), 1),
                    "rhr": score.get("resting_heart_rate"),
                    "sleep_hours": round(stage_summary.get("total_in_bed_time_milli", 0) / 3_600_000, 1),
                    "sleep_efficiency": sleep_score.get("sleep_efficiency_percentage"),
                    "daily_brief": brief,
                },
            )

            # 2) One Runs row per Strava activity. Each activity also flows
            # into the Schedule DB (via find_or_create_schedule inside
            # log_run) so that day's Schedule entry exists and relates back.
            # Non-run cardio (rides/hikes/swims/walks) lands here too, tagged
            # by Type. Dedupe-by-date isn't enforced — if the same activity
            # lands twice, user can delete the dupe row manually.
            for a in activities:
                try:
                    await self.notion.log_strava_activity(a)
                except Exception as e:
                    logger.debug(f"Notion run log skipped for activity {a.get('id')}: {e}")
        except Exception as e:
            logger.warning(f"Notion log failed silently: {e}")

        return brief

    async def weekly_summary(self) -> str:
        context = await self._build_layered_context()
        prompt = WEEKLY_SUMMARY_PROMPT.format(data=context)
        return await self._ask_claude(prompt, allow_tools=True, max_tokens=1500)

    async def stoic_reflection(self) -> str:
        context = await self._build_layered_context()
        prompt = SUNDAY_REFLECTION_PROMPT.format(data=context)
        return await self._ask_claude(prompt, allow_tools=True, max_tokens=1200)

    async def recommend_workout(self) -> str:
        """Prescribe today's session, modulated by recovery + training load.

        This isn't a freeform "give me a workout" — it reads the active plan's
        session for today and dials intensity up/down based on recovery, ACWR,
        and recent sleep. If recovery is red, it substitutes a rest-day shape
        (walk + mobility + heat) rather than insisting on the planned session.
        Output stays terse and concrete — no philosophy quotes mid-message.
        """
        context = await self._build_layered_context()
        prompt = (
            "You're CoachRex. Prescribe TODAY's workout, using the active "
            "plan's session for today as the default and modulating intensity "
            "based on the data below. Rules:\n"
            "  • Recovery green (>=67) AND ACWR 0.8–1.3 → run the session as written.\n"
            "  • Recovery yellow (34–66) OR ACWR >1.3 → dial intensity down "
            "one notch: lighter top sets, easier run intervals, or shorter work.\n"
            "  • Recovery red (<34) → swap the session for an active-recovery "
            "shape (walk, mobility, sauna/plunge). Be honest about why.\n"
            "  • No planned session defined for today → give a simple default "
            "consistent with the plan's weekly shape.\n\n"
            "Output shape (plain text, no markdown headers):\n"
            "  Line 1: one-sentence honest read on today's state.\n"
            "  Line 2 onward: session type / focus, then the prescription with "
            "specific sets/reps/time/pace. Units: miles and pounds.\n"
            "  Close with one short line on what you dialed up or down and why.\n\n"
            "Keep total output under ~200 words. Stoic-concrete voice — no "
            "philosophy-poster phrases, no 'keep pushing' filler.\n\n"
            f"=== CONTEXT ===\n{context}"
        )
        return await self._ask_claude(prompt, allow_tools=False, max_tokens=700)

    # ── Debrief (live WHOOP workout + Strava parallel fetch) ────────────────

    @staticmethod
    def _parse_iso_utc(ts: str) -> datetime | None:
        """Parse an ISO-8601 timestamp into an aware UTC datetime, or None."""
        if not ts:
            return None
        try:
            # fromisoformat accepts "+00:00" but not "Z" until 3.11 — normalize.
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except Exception:
            return None

    @staticmethod
    def _windows_overlap(
        a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime,
        slack_minutes: int = 10,
    ) -> bool:
        """Return True if two time windows overlap within `slack_minutes`.

        Strava and WHOOP sometimes disagree on start/end by a few minutes — the
        watch starts the activity a beat before you hit go on the phone, etc.
        A small slack here matches pairs that clearly describe the same session.
        """
        slack = timedelta(minutes=slack_minutes)
        return not (a_end + slack < b_start or a_start > b_end + slack)

    async def debrief_run(
        self,
        hours_back: int = 8,
        activity_id: int | None = None,
    ) -> str:
        """Generate a post-activity debrief.

        Fetches the most recent WHOOP workout AND recent Strava activities in
        parallel. WHOOP is the authoritative source for HR (avg HR, max HR,
        zone time, workout strain); Strava fills in pace / distance / GPS /
        splits / elevation when it has synced. The debrief NEVER blocks on
        Strava — if Strava is empty or errors, we still produce a WHOOP-only
        debrief.

        Args:
            hours_back: window for the "most recent" workout lookup.
            activity_id: optional specific Strava activity id to debrief. If
                given, we fetch that activity directly and pair it with the
                overlapping WHOOP workout.

        Returns:
            Coaching text, ready to send to Discord. Always returns something,
            even if no activity was found — a single sentence is better than a
            silent failure.
        """

        async def _fetch_whoop():
            try:
                return await self.whoop.get_workouts(hours=hours_back, limit=10)
            except Exception as e:
                logger.warning(f"WHOOP workout fetch failed in debrief: {e}")
                return e

        async def _fetch_strava():
            try:
                if activity_id is not None:
                    detail = await self.strava.get_activity_detail(int(activity_id))
                    return [detail] if detail else []
                # Strava's `after` window; days is overkill for a post-run
                # debrief but lets a request for "the run this morning" still
                # hit when the run ended ~6–8 h ago.
                days = max(1, (hours_back + 23) // 24)
                return await self.strava.get_recent_activities(days=days)
            except Exception as e:
                logger.warning(f"Strava fetch failed in debrief: {e}")
                return e

        # Parallel — a Strava failure must not delay the WHOOP-only path.
        whoop_res, strava_res = await asyncio.gather(
            _fetch_whoop(), _fetch_strava(), return_exceptions=False
        )

        whoop_workouts = whoop_res if isinstance(whoop_res, list) else []
        strava_acts = strava_res if isinstance(strava_res, list) else []

        # Opportunistically persist anything we fetched so SQLite stays warm.
        for rec in whoop_workouts:
            try:
                row = self.whoop.normalize_workout(rec)
                await self.db.upsert_whoop_workout(row, rec)
            except Exception as e:
                logger.debug(f"Debrief: whoop workout upsert failed: {e}")
        for act in strava_acts:
            try:
                await self.db.upsert_strava_activity(act)
            except Exception as e:
                logger.debug(f"Debrief: strava activity upsert failed: {e}")

        # Choose the anchor workout: prefer the most recent WHOOP workout
        # (that's our HR source), fall back to the most recent Strava activity
        # if WHOOP has nothing.
        whoop_workouts = sorted(
            whoop_workouts,
            key=lambda r: r.get("start") or "",
            reverse=True,
        )
        strava_acts = sorted(
            strava_acts,
            key=lambda a: a.get("start_date") or a.get("start_date_local") or "",
            reverse=True,
        )

        anchor_whoop = whoop_workouts[0] if whoop_workouts else None

        # Match Strava activity to the anchor by time-window overlap.
        matched_strava = None
        if anchor_whoop:
            w_start = self._parse_iso_utc(anchor_whoop.get("start"))
            w_end = self._parse_iso_utc(anchor_whoop.get("end"))
            if w_start and w_end:
                for act in strava_acts:
                    a_start = self._parse_iso_utc(act.get("start_date"))
                    if not a_start:
                        continue
                    a_end = a_start + timedelta(
                        seconds=(act.get("elapsed_time") or act.get("moving_time") or 0)
                    )
                    if self._windows_overlap(w_start, w_end, a_start, a_end):
                        matched_strava = act
                        break
        else:
            # WHOOP-empty path — anchor on most recent Strava if present.
            matched_strava = strava_acts[0] if strava_acts else None

        # No data at all — short, honest reply beats hallucinating a session.
        if not anchor_whoop and not matched_strava:
            return (
                "No recent workout on file. WHOOP shows no session in the last "
                f"{hours_back}h and Strava has nothing new. If you just finished, "
                "give the phones a minute to sync and ask again."
            )

        # Build a compact data block for Claude. WHOOP numbers are preferred
        # for HR/zone; Strava is preferred for pace/distance/splits.
        data_lines: list[str] = []
        if anchor_whoop:
            w = self.whoop.normalize_workout(anchor_whoop)
            data_lines.append("WHOOP workout (authoritative for HR + zones + workout strain):")
            data_lines.append(f"  Sport: {w.get('sport_name')} (id {w.get('sport_id')})")
            data_lines.append(f"  Start: {w.get('start_utc')}  End: {w.get('end_utc')}")
            if w.get("average_hr") is not None:
                data_lines.append(f"  Avg HR: {int(w['average_hr'])} bpm")
            if w.get("max_hr") is not None:
                data_lines.append(f"  Max HR: {int(w['max_hr'])} bpm")
            if w.get("strain") is not None:
                data_lines.append(f"  Workout strain: {round(w['strain'], 1)}")
            if w.get("kilojoule") is not None:
                data_lines.append(f"  Energy: {int(w['kilojoule'])} kJ")
            # Zones — only include if at least one bucket has real time in it.
            zone_keys = [
                ("Z0 (<50% HRmax)", "zone0_ms"),
                ("Z1 (50–60%)", "zone1_ms"),
                ("Z2 (60–70%)", "zone2_ms"),
                ("Z3 (70–80%)", "zone3_ms"),
                ("Z4 (80–90%)", "zone4_ms"),
                ("Z5 (90–100%)", "zone5_ms"),
            ]
            zone_mins = [(lbl, round((w.get(k) or 0) / 60000, 1)) for lbl, k in zone_keys]
            if any(m for _, m in zone_mins):
                data_lines.append(
                    "  Zone time: "
                    + ", ".join(f"{lbl} {m}m" for lbl, m in zone_mins if m)
                )
            if w.get("percent_recorded") is not None:
                data_lines.append(f"  % recorded: {round(w['percent_recorded'], 1)}")

        if matched_strava:
            a = matched_strava
            dist_mi = _m_to_mi(a.get("distance") or a.get("distance_m"))
            dur_min = round((a.get("moving_time") or a.get("moving_time_s") or 0) / 60, 1)
            elev_ft = _m_to_ft(a.get("total_elevation_gain") or a.get("total_elevation_gain_m"))
            avg_mps = a.get("average_speed") or a.get("average_speed_mps")
            max_mps = a.get("max_speed") or a.get("max_speed_mps")
            pace = _mps_to_min_per_mi(avg_mps) if avg_mps else None
            max_pace = _mps_to_min_per_mi(max_mps) if max_mps else None

            data_lines.append("")
            data_lines.append("Strava activity (authoritative for pace / distance / GPS / splits):")
            data_lines.append(f"  Name: {a.get('name', '—')}")
            data_lines.append(f"  Type: {a.get('sport_type') or a.get('type') or '?'}")
            data_lines.append(f"  Start: {a.get('start_date') or a.get('start_date_local', '')}")
            if dist_mi:
                data_lines.append(f"  Distance: {dist_mi} mi")
            if dur_min:
                data_lines.append(f"  Duration: {dur_min} min")
            if pace:
                data_lines.append(f"  Avg pace: {pace}")
            if max_pace:
                data_lines.append(f"  Peak pace: {max_pace}")
            if elev_ft:
                data_lines.append(f"  Elevation gain: {elev_ft} ft")
            # If we have detail-level splits (activity_id path), surface them.
            laps = a.get("laps") or []
            if laps:
                data_lines.append(f"  Splits ({len(laps)}):")
                for i, lap in enumerate(laps, 1):
                    lap_mi = _m_to_mi(lap.get("distance"))
                    lap_pace = _mps_to_min_per_mi(lap.get("average_speed"))
                    lap_hr = lap.get("average_heartrate")
                    parts = [f"    {i}: {lap_mi}mi"]
                    if lap_pace:
                        parts.append(lap_pace)
                    if lap_hr:
                        parts.append(f"avg HR {int(lap_hr)}")
                    data_lines.append(" | ".join(parts))
        elif anchor_whoop:
            data_lines.append("")
            data_lines.append(
                "Strava activity: NOT YET SYNCED (Strava webhook may lag 30s–"
                "several min, or the activity wasn't recorded on Strava). "
                "Debrief HR + zones + workout strain from WHOOP; call out "
                "that pace/splits are unavailable rather than guessing."
            )

        # Pull today's recovery for context — a hard intervals-day interpretation
        # changes completely if recovery was 32 vs 78.
        try:
            today_iso = datetime.now().date().isoformat()
            rows = await self.db.get_whoop_daily(today_iso, today_iso)
            if not rows:
                # Fall back to yesterday if today hasn't been synced yet.
                y = (datetime.now().date() - timedelta(days=1)).isoformat()
                rows = await self.db.get_whoop_daily(y, y)
            if rows:
                r = rows[0]
                data_lines.append("")
                data_lines.append("Today's recovery context:")
                data_lines.append(
                    f"  Recovery {r.get('recovery_score')}% | "
                    f"HRV {r.get('hrv_rmssd_ms')}ms | "
                    f"RHR {r.get('resting_hr')} bpm | "
                    f"Sleep {r.get('total_asleep_hours')}h"
                )
        except Exception as e:
            logger.debug(f"Debrief: recovery context fetch failed: {e}")

        # Active plan session for today, if any — lets the coach read the run
        # against what was prescribed instead of judging it in a vacuum.
        try:
            plan = await self.db.get_active_plan()
            if plan:
                day_name = datetime.now().strftime("%A").lower()
                session = (plan.get("weekly_template") or {}).get(day_name)
                if session:
                    data_lines.append("")
                    data_lines.append(
                        f"Today's planned session: {session.get('session_type', '?').upper()} "
                        f"— {session.get('focus', '')}"
                    )
                    if session.get("prescription"):
                        data_lines.append(f"  Prescription: {session['prescription']}")
        except Exception as e:
            logger.debug(f"Debrief: active plan fetch failed: {e}")

        data_block = "\n".join(data_lines)

        prompt = (
            "You're CoachRex debriefing Dylan's most recent workout. Rules:\n"
            "  • WHOOP is the source of truth for HR, zones, and workout strain.\n"
            "  • Strava is the source of truth for pace, distance, elevation, "
            "splits. If Strava hasn't synced, say so — do not invent pace numbers.\n"
            "  • Read the session against today's recovery and the planned "
            "prescription (if given). Was intensity appropriate? Too hard? Too light?\n"
            "  • Call out one specific thing he did well and one thing to tune next time.\n"
            "  • If zone distribution says easy day, do not talk about PRs; if it was "
            "threshold/interval, grade the work honestly.\n"
            "  • Units: miles and pounds. Pace as mm:ss/mi.\n\n"
            "Output shape (plain text, no markdown headers):\n"
            "  Line 1: one-sentence verdict (\"That was a well-controlled Z2\" / "
            "\"You blew past the prescription — here's what it cost\").\n"
            "  Lines 2–4: the numbers that matter, in plain sentences.\n"
            "  Lines 5–6: what to do differently / repeat next time.\n\n"
            "Keep under ~180 words. No philosophy quotes. No 'keep pushing' filler.\n\n"
            f"=== DATA ===\n{data_block}"
        )
        return await self._ask_claude(prompt, allow_tools=False, max_tokens=600)

    async def chat(self, message: str) -> str:
        """
        Handle a conversational message.

        - If the message smells like a lift, parse + log it (Haiku, cheap).
        - Build layered context from SQLite.
        - Let Claude call history tools on demand.
        """
        # Fast intent route: "debrief my run" / "how'd the workout go" / etc.
        # We skip the full layered-context + tools build because the debrief
        # path has its own, tighter data-assembly tailored to a post-session
        # question.
        if _DEBRIEF_INTENT.search(message):
            logger.info("chat(): routed to debrief_run via intent regex.")
            return await self.debrief_run()

        lift = await self._try_parse_lift(message)
        if lift:
            await self.db.log_lift(
                date=datetime.now().strftime("%Y-%m-%d"),
                exercise=lift["exercise"],
                details=lift["details"],
                raw=message,
            )
            try:
                # Writes to the Lifts DB. The parser now returns optional
                # structured `sets`, `reps`, `weight_lb`, `workout` fields
                # when Haiku can extract them — when it can't, those cells
                # stay blank and the raw message in Notes preserves the
                # original phrasing so nothing is lost.
                await self.notion.log_lift(
                    date=datetime.now().strftime("%Y-%m-%d"),
                    exercise=lift["exercise"],
                    workout=lift.get("workout"),
                    sets=lift.get("sets"),
                    reps=lift.get("reps"),
                    weight_lb=lift.get("weight_lb"),
                    notes=message,
                )
            except Exception:
                pass

        # Recovery session logging runs independently — a single message can
        # describe both a lift AND a post-lift sauna.
        recovery = await self._try_parse_recovery_session(message)
        if recovery:
            await self.db.log_recovery_session(
                date=datetime.now().strftime("%Y-%m-%d"),
                session_type=recovery["session_type"],
                duration_min=recovery.get("duration_min"),
                temp_f=recovery.get("temp_f"),
                notes=recovery.get("notes", ""),
                raw=message,
            )

        context = await self._build_layered_context()
        knowledge = self._retrieve_knowledge(message)
        prompt = CHAT_PROMPT.format(
            message=message, context=context, knowledge=knowledge
        )
        return await self._ask_claude(prompt, use_history=True, allow_tools=True)

    # ── Lift parsing (regex pre-filter + cheap model) ───────────────────────

    async def _try_parse_lift(self, message: str) -> dict | None:
        """Fast pre-filter: if the message has no numbers or lift keywords, skip
        the model call entirely. Saves roughly one Claude round-trip per chat.

        Returns a dict with:
          exercise (str, required), details (str, free-form backup),
          sets, reps, weight_lb (numbers, optional — missing if ambiguous),
          workout (Push/Pull/Legs/Other, optional — model's best guess).

        The structured fields feed the Notion Lifts DB columns directly. The
        details string is kept as a fallback so partial parses (e.g. Haiku
        extracts exercise but can't pin sets/reps) still record the raw lift.
        """
        if not _LIFT_HINT.search(message):
            return None

        # Prompt tightened so Haiku returns numbers we can put straight into
        # number columns. Unit note is critical — "135" in a bench context
        # is pounds in the US; the model defaults to whatever the message
        # says, and we coerce below.
        parse_prompt = f"""
Does this message describe a weightlifting exercise? If yes, extract the structured fields.

Message: "{message}"

Respond with JSON ONLY. Schema:
{{
  "is_lift": true | false,
  "exercise": "<lift name, e.g. 'Bench press', 'Back squat'>",
  "details": "<original sets/reps/weight phrasing, e.g. '3x10 @ 145'>",
  "sets": <int or null>,
  "reps": <int or null — if sets are a scheme like 5x5, this is reps per set>,
  "weight_lb": <number or null — assume pounds unless message says 'kg'>,
  "workout": "Push" | "Pull" | "Legs" | "Other" | null
}}

Rules:
  • If the message describes a lift but you can't confidently pin a field, set it to null.
  • For push/pull/legs: Bench/OHP/Dips/Push-ups = Push. Rows/Pull-ups/Chin-ups/Curls = Pull.
    Squat/Deadlift/Lunge/Leg press = Legs. Core/accessory/mixed = Other.
  • "185" in a bench context means 185 lb. If the message says "60 kg", convert: kg*2.2046, rounded.
  • If it's not a lift, return {{"is_lift": false}}.
""".strip()

        try:
            resp = await self.claude.messages.create(
                model=self.cheap_model,  # Haiku — ~10x cheaper than Sonnet
                max_tokens=300,
                messages=[{"role": "user", "content": parse_prompt}],
            )
            raw = resp.content[0].text
            start = raw.find("{")
            end = raw.rfind("}") + 1
            data = json.loads(raw[start:end])
            if not data.get("is_lift"):
                return None

            def _coerce_int(v):
                if v is None:
                    return None
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    return None

            def _coerce_float(v):
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            return {
                "exercise": data.get("exercise") or "",
                "details": data.get("details") or "",
                "sets": _coerce_int(data.get("sets")),
                "reps": _coerce_int(data.get("reps")),
                "weight_lb": _coerce_float(data.get("weight_lb")),
                "workout": data.get("workout") if data.get("workout") in ("Push", "Pull", "Legs", "Other") else None,
            }
        except Exception as e:
            logger.debug(f"Lift parse failed: {e}")
        return None

    # ── Recovery-session parsing (sauna / cold plunge / etc.) ───────────────

    async def _try_parse_recovery_session(self, message: str) -> dict | None:
        """Fast pre-filter: skip the model unless the message mentions a
        known recovery modality. Parses duration (min) and temp (°F).
        """
        if not _RECOVERY_HINT.search(message):
            return None

        parse_prompt = f"""
Does this message describe a completed recovery session (sauna, steam room,
cold plunge, ice bath, contrast bath, cryo)? If yes, extract:
- session_type: one of "sauna", "steam_room", "cold_plunge", "ice_bath",
  "contrast", "cryo", "hot_tub" (pick the closest match)
- duration_min: minutes as a number, or null if not stated
- temp_f: temperature in degrees Fahrenheit as a number. Convert from Celsius
  if the user gave °C. null if not stated.
- notes: short free-text detail worth keeping (e.g. "post-lift", "after run",
  "2 rounds"), else empty string.

Message: "{message}"

Respond with JSON only.
If it's a session: {{"is_session": true, "session_type": "...", "duration_min": <number or null>, "temp_f": <number or null>, "notes": "..."}}
If not (e.g. "headed to the sauna later" — no completion, no data): {{"is_session": false}}
""".strip()

        try:
            resp = await self.claude.messages.create(
                model=self.cheap_model,  # Haiku — cheap classifier
                max_tokens=250,
                messages=[{"role": "user", "content": parse_prompt}],
            )
            raw = resp.content[0].text
            start = raw.find("{")
            end = raw.rfind("}") + 1
            data = json.loads(raw[start:end])
            if data.get("is_session"):
                return {
                    "session_type": data.get("session_type") or "sauna",
                    "duration_min": data.get("duration_min"),
                    "temp_f": data.get("temp_f"),
                    "notes": data.get("notes", "") or "",
                }
        except Exception as e:
            logger.debug(f"Recovery-session parse failed: {e}")
        return None
