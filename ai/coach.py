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

import json
import logging
import re
from datetime import datetime, timedelta

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
]


class Coach:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.strava = StravaClient(config)
        self.whoop = WhoopClient(config)
        self.notion = NotionClient(config)
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
        acts = await self.db.get_strava_activities_range(str(d14), str(today))
        if acts:
            lines.append("")
            lines.append("LAST 14 DAYS (Strava):")
            for a in acts[:15]:
                mi = _m_to_mi(a.get("distance_m", 0))
                dur = round((a.get("moving_time_s") or 0) / 60, 1)
                parts = [a.get("date"), a.get("sport_type") or "?", f"{dur}min"]
                if mi:
                    parts.append(f"{mi}mi")
                if a.get("average_hr"):
                    parts.append(f"avg HR {int(a['average_hr'])}")
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

        # Best-effort Notion log (unchanged behaviour)
        try:
            snapshot = await self.whoop.get_today_snapshot()
            rec = snapshot.get("recovery", {}) or {}
            slp = snapshot.get("sleep", {}) or {}
            activities = await self.strava.get_recent_activities(days=1)

            score = rec.get("score", {}) if rec else {}
            sleep_score = slp.get("score", {}) if slp else {}
            stage_summary = sleep_score.get("stage_summary", {}) if sleep_score else {}

            await self.notion.log_daily_entry(
                date=datetime.now().strftime("%Y-%m-%d"),
                summary={
                    "recovery_score": score.get("recovery_score"),
                    "hrv": round(score.get("hrv_rmssd_milli", 0), 1),
                    "rhr": score.get("resting_heart_rate"),
                    "sleep_hours": round(stage_summary.get("total_in_bed_time_milli", 0) / 3_600_000, 1),
                    "sleep_efficiency": sleep_score.get("sleep_efficiency_percentage"),
                    "activities": [self.strava.summarize_activity(a) for a in activities],
                    "daily_brief": brief,
                },
            )
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

    async def chat(self, message: str) -> str:
        """
        Handle a conversational message.

        - If the message smells like a lift, parse + log it (Haiku, cheap).
        - Build layered context from SQLite.
        - Let Claude call history tools on demand.
        """
        lift = await self._try_parse_lift(message)
        if lift:
            await self.db.log_lift(
                date=datetime.now().strftime("%Y-%m-%d"),
                exercise=lift["exercise"],
                details=lift["details"],
                raw=message,
            )
            try:
                await self.notion.log_lift(
                    date=datetime.now().strftime("%Y-%m-%d"),
                    exercise=lift["exercise"],
                    sets_reps_weight=lift["details"],
                )
            except Exception:
                pass

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
        """
        if not _LIFT_HINT.search(message):
            return None

        parse_prompt = f"""
Does this message describe a weightlifting exercise? If yes, extract:
- exercise name
- details (sets, reps, weight as described)

Message: "{message}"

Respond with JSON only. If it's a lift: {{"is_lift": true, "exercise": "...", "details": "..."}}
If not: {{"is_lift": false}}
""".strip()

        try:
            resp = await self.claude.messages.create(
                model=self.cheap_model,  # Haiku — ~10x cheaper than Sonnet
                max_tokens=200,
                messages=[{"role": "user", "content": parse_prompt}],
            )
            raw = resp.content[0].text
            start = raw.find("{")
            end = raw.rfind("}") + 1
            data = json.loads(raw[start:end])
            if data.get("is_lift"):
                return {"exercise": data["exercise"], "details": data["details"]}
        except Exception as e:
            logger.debug(f"Lift parse failed: {e}")
        return None
