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
    CHAT_SYSTEM_PROMPT,
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


# ── Regex — does this chat message want trend / historical detail? ──────────
# When TRUE, the chat path falls through to the full layered context (last
# 7d detail + 30d/12-mo aggregates + 14d activities). When FALSE, it uses
# the small "today + last 3 days" snapshot — enough for "how am I today",
# "log this lift", "what should I do this morning", which is the long tail
# of chat traffic.
_TREND_INTENT = re.compile(
    r"""
    \b(
        trend|trending|progression|progress|improving|declining
      | history|historical|baseline
      | (last|past|over\s+the)\s+(week|month|year|\d+\s*(days?|weeks?|months?))
      | (in|during|for)\s+(january|february|march|april|may|june|july|
                          august|september|october|november|december)
      | year[\s\-]?to[\s\-]?date|ytd|yoy|year[\s\-]?over[\s\-]?year
      | compared?\s+to|versus|vs\.?
      | average|avg|mean|median
      | hrv\s+(over|across|in)
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ── Regex — is the user asking an educational/conceptual question? ──────────
# Only when TRUE do we hit the knowledge retriever (which adds 1–2K tokens
# and a sentence-transformer encode call). For "log my bench", "how am I
# today", a knowledge dump is wasted tokens.
_KNOWLEDGE_INTENT = re.compile(
    r"""
    \b(
        why\b|what\s+is|what's|what\s+does|how\s+does|how\s+do
      | explain|tell\s+me\s+about
      | difference\s+between|compared?\s+to
      | should\s+i|is\s+it\s+(better|worse|safe|ok)
      | meaning|principle|concept|theory
      | zone\s*\d|polari[sz]ed|hypertrophy|periodi[sz]ation
      | vo2\s*max|lactate|threshold
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
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
        # Haiku is ~3× cheaper — great for the "is this a lift?" classifier and,
        # when COACH_CHEAP_MODE is on, the conversational chat path too.
        self.cheap_model = "claude-haiku-4-5-20251001"
        # Chat path model — Haiku by default, falls back to the heavier model
        # if the user explicitly turns cheap-mode off. Independent of self.model
        # so the morning brief / weekly summary / Sunday reflection keep using
        # Sonnet (where prose quality matters most).
        self.chat_model = (
            getattr(config, "CHAT_MODEL", self.cheap_model)
            if getattr(config, "COACH_CHEAP_MODE", True)
            else self.model
        )

        # Conversation history for multi-turn chat. Capped tight: long history
        # buys little for a personal coach and inflates input tokens on every
        # turn (no caching saves you on the messages list).
        self._conversation: list[dict] = []
        self._conversation_max = 12  # 6 user/assistant pairs

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
        model: Optional[str] = None,
        system: Optional[str] = None,
        max_tool_iters: int = 3,
    ) -> str:
        """Send a message to Claude, handling any tool calls it makes.

        Cost-aware notes:
          * `system` is wrapped in cache_control so the system prompt costs
            ~10% of normal after the first call in a 5-minute window. Big win
            on the chat path where SYSTEM_PROMPT dominates each turn.
          * `model` lets callers route the chat path to Haiku while the
            scheduled briefs (daily / weekly / Sunday) stay on Sonnet.
          * `max_tool_iters` is now 3 (was 6). Real tool-use plans rarely
            need more than 2 round-trips; 6 just paid for runaway loops.
        """
        messages: list = list(self._conversation) if use_history else []
        messages.append({"role": "user", "content": user_message})

        # Wrap the system prompt in a cache_control block so identical
        # consecutive calls hit the prompt cache instead of reprocessing
        # the prompt from scratch. The Anthropic SDK accepts this shape
        # directly on the `system` field.
        sys_text = system if system is not None else SYSTEM_PROMPT
        cached_system = [
            {
                "type": "text",
                "text": sys_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        create_kwargs = dict(
            model=model or self.model,
            max_tokens=max_tokens,
            system=cached_system,
            messages=messages,
        )
        if allow_tools:
            create_kwargs["tools"] = TOOLS

        # Tool-use loop: keep responding to tool_use blocks until Claude
        # returns plain text (stop_reason != "tool_use"). Cap iterations so a
        # buggy model can't spin forever AND so we don't pay for 6 round-trips
        # on a question that only needed one.
        for _ in range(max_tool_iters):
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
                    if len(self._conversation) > self._conversation_max:
                        self._conversation = self._conversation[-self._conversation_max:]
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

    # ── Plan adherence (prescribed vs actual) ───────────────────────────────
    #
    # The bug this exists to fix: without an explicit reconciliation between
    # the plan template and the activity log, the model treats the plan as a
    # record of what happened. It reads "Monday: legs" in the template and
    # reasons as if Monday's legs occurred, even when the Strava activity for
    # that day clearly shows a run. The fix is to compute adherence here,
    # server-side, and hand the model a ledger it can't ignore.
    #
    # Week boundary: weeks are Monday → Sunday. A rolling 7-day window
    # straddles week boundaries — on Tuesday it would mix the prior week's
    # Wed–Sun with this week's Mon–Tue, making the WEEKLY TARGETS counts
    # nonsensical ("you're under on lifts" when the under is two days into
    # a fresh week). Anchoring to Monday makes WEEKLY TARGETS week-to-date,
    # which is what a coach actually reasons about.
    def _compute_plan_adherence(
        self,
        plan: Optional[dict],
        acts: list[dict],
        lifts: list[dict],
    ) -> list[str]:
        """Reconcile prescribed plan vs actual completed sessions.

        Builds a per-day ledger for the current calendar week (Mon → today)
        and a one-line summary of the previous full week (Mon → Sun) for
        context when it's early in the week. Returns a list of lines for
        the PLAN ADHERENCE block.

        Callers should pass at least 14 days of activities + lifts so the
        last-week summary has data to work with.
        """
        if not plan:
            return []
        template = plan.get("weekly_template") or {}
        if not template:
            return []

        today = datetime.now().date()
        # Monday-anchored week boundaries. weekday(): Mon=0 ... Sun=6.
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        last_sunday = this_monday - timedelta(days=1)

        # Bucket actuals by date string ('YYYY-MM-DD').
        by_date: dict[str, dict] = {}
        for a in acts or []:
            d = a.get("date")
            if not d:
                continue
            slot = by_date.setdefault(d, {"acts": [], "lifts": []})
            slot["acts"].append(a)
        for l in lifts or []:
            d = l.get("date")
            if not d:
                continue
            slot = by_date.setdefault(d, {"acts": [], "lifts": []})
            slot["lifts"].append(l)

        def _classify_prescribed(session: Optional[dict]) -> str:
            if not session:
                return "REST"
            stype = (session.get("session_type") or "").lower()
            if stype in ("lift", "strength"):
                return "LIFT"
            if stype in ("run", "long_run", "easy_run", "interval", "tempo"):
                return "RUN"
            if stype in ("rest", "off"):
                return "REST"
            if stype == "cross":
                return "CROSS"
            # Unknown session_type — keep the raw value so it's visible.
            return stype.upper() if stype else "?"

        def _classify_actual(slot: Optional[dict]) -> tuple[str, str]:
            """Return (bucket, detail-string) for a day's activities + lifts."""
            if not slot:
                return "REST", ""
            has_lift = bool(slot.get("lifts"))
            running: list[dict] = []
            other: list[dict] = []
            for a in slot.get("acts", []):
                sp = (a.get("sport_type") or "").lower()
                if "weight" in sp or "strength" in sp:
                    has_lift = True
                elif "run" in sp:
                    running.append(a)
                else:
                    other.append(a)

            detail_bits: list[str] = []
            for a in running + other:
                mi = _m_to_mi(a.get("distance_m") or 0)
                dur = round((a.get("moving_time_s") or 0) / 60, 1)
                sp = a.get("sport_type") or "?"
                if mi:
                    detail_bits.append(f"{sp} {mi}mi {dur}min")
                else:
                    detail_bits.append(f"{sp} {dur}min")
            for l in slot.get("lifts", []):
                ex = l.get("exercise") or "lift"
                detail_bits.append(f"LIFT {ex}")
            detail = "; ".join(detail_bits)

            if has_lift and running:
                return "LIFT+RUN", detail
            if has_lift:
                return "LIFT", detail
            if running:
                return "RUN", detail
            if other:
                return "OTHER", detail
            return "REST", detail

        def _counts_for_range(start_date, end_date) -> dict:
            """Tally LIFT/RUN/REST/OTHER actuals from start..end inclusive."""
            tally = {"LIFT": 0, "RUN": 0, "REST": 0, "OTHER": 0, "CROSS": 0}
            d = start_date
            while d <= end_date:
                actual, _ = _classify_actual(by_date.get(d.isoformat()))
                if actual == "LIFT+RUN":
                    tally["LIFT"] += 1
                    tally["RUN"] += 1
                else:
                    key = actual if actual in tally else "OTHER"
                    tally[key] += 1
                d += timedelta(days=1)
            return tally

        # ── Per-day ledger for THIS WEEK (Monday → today) ──────────────────
        rows: list[str] = []
        counts_this_week = {"LIFT": 0, "RUN": 0, "REST": 0, "OTHER": 0, "CROSS": 0}
        deviation_count = 0

        d = this_monday
        while d <= today:
            day_name = d.strftime("%A").lower()
            prescribed = _classify_prescribed(template.get(day_name))
            actual, detail = _classify_actual(by_date.get(d.isoformat()))

            if actual == "LIFT+RUN":
                counts_this_week["LIFT"] += 1
                counts_this_week["RUN"] += 1
            else:
                key = actual if actual in counts_this_week else "OTHER"
                counts_this_week[key] += 1

            satisfied = (
                actual == prescribed
                or (actual == "LIFT+RUN" and prescribed in ("LIFT", "RUN"))
            )
            is_today = (d == today)
            # Don't flag today as a deviation — the day isn't over yet.
            if not satisfied and not is_today:
                deviation_count += 1

            if is_today:
                marker = "TODAY"
            elif satisfied:
                marker = "OK"
            else:
                marker = "DEVIATION"
            date_label = d.strftime("%a %b %d")
            row = (
                f"{date_label}: prescribed {prescribed:<5} | "
                f"actual {actual:<8} | {marker}"
            )
            if detail:
                row += f" ({detail})"
            rows.append(row)
            d += timedelta(days=1)

        # ── Weekly targets from the template ───────────────────────────────
        target_lifts = sum(
            1 for s in template.values() if _classify_prescribed(s) == "LIFT"
        )
        target_runs = sum(
            1 for s in template.values() if _classify_prescribed(s) == "RUN"
        )
        target_rest = sum(
            1 for s in template.values() if _classify_prescribed(s) == "REST"
        )

        # ── LAST WEEK summary (full Mon → Sun) ─────────────────────────────
        last_week_counts = _counts_for_range(last_monday, last_sunday)

        # ── Compose output ─────────────────────────────────────────────────
        lines: list[str] = []
        days_in = (today - this_monday).days + 1
        lines.append(
            f"PLAN ADHERENCE — THIS WEEK ({this_monday.strftime('%a %b %d')} "
            f"→ {today.strftime('%a %b %d')}, day {days_in} of 7):"
        )
        for r in rows:
            lines.append(f"  {r}")

        wk_summary = (
            f"WEEK-TO-DATE vs TARGETS: "
            f"lifts {counts_this_week['LIFT']}/{target_lifts}, "
            f"runs {counts_this_week['RUN']}/{target_runs}, "
            f"rest {counts_this_week['REST']}/{target_rest}"
        )
        # Pro-rate the under/over flags so we don't yell "UNDER on lifts" on
        # Monday morning when the week has barely started. A category is
        # only "under" if the days remaining in the week aren't enough to
        # close the gap.
        days_remaining = 7 - days_in  # full days after today
        deficits: list[str] = []
        lift_short = target_lifts - counts_this_week["LIFT"]
        run_short = target_runs - counts_this_week["RUN"]
        if lift_short > days_remaining:
            deficits.append(
                f"UNDER on lifts (need {lift_short} more in {days_remaining} "
                f"day{'s' if days_remaining != 1 else ''} left — tight)"
            )
        elif lift_short > 0:
            deficits.append(
                f"behind on lifts ({lift_short} to go, {days_remaining} "
                f"day{'s' if days_remaining != 1 else ''} left)"
            )
        if run_short > days_remaining:
            deficits.append(
                f"UNDER on runs (need {run_short} more in {days_remaining} left)"
            )
        elif run_short > 0:
            deficits.append(
                f"behind on runs ({run_short} to go, {days_remaining} left)"
            )
        if counts_this_week["LIFT"] > target_lifts + 1:
            deficits.append("OVER on lifts")
        if counts_this_week["RUN"] > target_runs + 1:
            deficits.append("OVER on runs")
        if deficits:
            wk_summary += " — " + ", ".join(deficits)
        lines.append(f"  {wk_summary}")

        last_week_summary = (
            f"LAST WEEK ({last_monday.strftime('%b %d')} → "
            f"{last_sunday.strftime('%b %d')}): "
            f"lifts {last_week_counts['LIFT']}/{target_lifts}, "
            f"runs {last_week_counts['RUN']}/{target_runs}, "
            f"rest {last_week_counts['REST']}/{target_rest}"
        )
        lines.append(f"  {last_week_summary}")

        # ── Loud guard rail when adherence has drifted enough that the
        # model would otherwise narrate the prescribed sequence as fact.
        if deviation_count >= 2:
            lines.append(
                f"  ⚠ {deviation_count} deviation{'s' if deviation_count != 1 else ''} "
                f"from plan so far this week — the ACTIVE PLAN block describes "
                f"what was PRESCRIBED, not what HAPPENED. Use the row-by-row "
                f"actuals above (and RECENT LIFTS / LAST 14 DAYS Strava below) "
                f"for any reasoning about prior sessions. If Dylan is under "
                f"on a category (e.g. lifts) and wants to do that category "
                f"today, that's the plan re-asserting itself — support it."
            )

        return lines

    # ── Tiered context (cheap-mode) ─────────────────────────────────────────

    async def _build_tiered_context(self, message: str) -> str:
        """Pick a context tier based on what the message actually needs.

        Tier 1 (default): today's recovery + 3-day daily summary + most
        recent 3 activities. ~250–400 tokens. Covers the long tail —
        "how am I today", "log my bench", "what should I run this morning".

        Tier 2 (trend / date-range / multi-month questions): full layered
        context (~2,000–3,000 tokens). The full context is also the right
        choice when no recovery row exists for today, since the small
        builder doesn't have the sleep/strain/HRV cross-references the
        model needs to be useful at all.

        We pay for the bigger build only when a regex on the user message
        suggests we'll actually use it. This is the single biggest input-
        token lever after prompt caching.
        """
        if _TREND_INTENT.search(message or ""):
            return await self._build_layered_context()

        today = datetime.now().date()
        d3 = today - timedelta(days=3)
        lines: list[str] = [f"TODAY: {today.strftime('%A, %B %d, %Y')}"]

        # Active lift session block — stays at the top so the model can
        # never miss it. When present, it overrides any plan-day default
        # and bans off-topic activity suggestions.
        try:
            session_block = await self.active_session_context_block()
        except Exception as e:
            logger.debug(f"Active session block failed: {e}")
            session_block = ""
        if session_block:
            lines.append("")
            lines.append(session_block)

        # Live WHOOP snapshot — same call the layered builder makes first.
        try:
            snap = await self.whoop.get_today_snapshot()
            rec_line = self.whoop.summarize_recovery(snap.get("recovery"))
            slp_line = self.whoop.summarize_sleep(snap.get("sleep"))
            if rec_line:
                lines.append(f"  {rec_line}")
            if slp_line:
                lines.append(f"  {slp_line}")
        except Exception as e:
            logger.debug(f"Tiered context: live WHOOP snapshot unavailable: {e}")

        # Last-3-days WHOOP daily rows. If we got fewer than 2 rows back,
        # the user is clearly asking from a sparse-data state — escalate
        # to the full builder so we don't answer from nothing.
        try:
            daily = await self.db.get_whoop_daily(str(d3), str(today))
        except Exception as e:
            logger.debug(f"Tiered context: WHOOP daily fetch failed: {e}")
            daily = []
        if len(daily) < 2:
            return await self._build_layered_context()

        lines.append("")
        lines.append("LAST 3 DAYS (WHOOP):")
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

        # Last 3 Strava activities for "what did I just do" follow-ups.
        try:
            acts = await self.db.get_strava_activities_range(str(d3), str(today))
        except Exception as e:
            logger.debug(f"Tiered context: Strava range fetch failed: {e}")
            acts = []
        if acts:
            lines.append("")
            lines.append("RECENT ACTIVITIES (last 3 days):")
            for a in acts[:3]:
                mi = _m_to_mi(a.get("distance_m", 0))
                dur = round((a.get("moving_time_s") or 0) / 60, 1)
                parts = [a.get("date"), a.get("sport_type") or "?", f"{dur}min"]
                if mi:
                    parts.append(f"{mi}mi")
                if a.get("average_hr"):
                    parts.append(f"avg HR {int(a['average_hr'])}")
                lines.append("  - " + " | ".join(parts))

        # Active training plan + today's prescribed session. Cheap (one
        # SQLite read) and load-bearing for mid-session questions like
        # "shouldn't I do one more set?" — without this, the model
        # correctly says it doesn't have access to the program. Only
        # injected in Tier 1; Tier 2 builds the same block itself.
        try:
            plan = await self.db.get_active_plan()
        except Exception as e:
            logger.debug(f"Tiered context: active plan lookup failed: {e}")
            plan = None
        if plan:
            day_name = today.strftime("%A").lower()
            session = (plan.get("weekly_template") or {}).get(day_name)
            lines.append("")
            lines.append(f"ACTIVE PLAN: {plan.get('name')}")
            if plan.get("goal"):
                lines.append(f"  Goal: {plan.get('goal')}")
            if session:
                stype = session.get("session_type", "?")
                focus = session.get("focus", "")
                presc = session.get("prescription", "")
                notes = session.get("notes", "")
                lines.append(
                    f"  Today ({today.strftime('%A')}): {stype}"
                    + (f" — {focus}" if focus else "")
                )
                if presc:
                    lines.append(f"    Prescription: {presc}")
                if notes:
                    lines.append(f"    Notes: {notes}")
            else:
                lines.append(
                    f"  Today ({today.strftime('%A')}): no session in template "
                    "(rest day or unscheduled)."
                )

        # Recent self-reported lifts. 14-day window so questions like "what
        # was my push workout last week" find something in Tier 1 without
        # escalating. Capped at 20 rows to keep the prompt lean.
        try:
            recent_lifts = await self.db.get_recent_lifts(days=14)
        except Exception as e:
            logger.debug(f"Tiered context: recent-lifts fetch failed: {e}")
            recent_lifts = []

        # PLAN ADHERENCE — only when a plan exists. Pulls 14 days of Strava
        # activities (one extra DB read, cheap) and reuses the 14-day lifts
        # already in hand. 14 days covers last week's Mon → today, so the
        # LAST WEEK summary line has data to render even when it's early
        # in the week. This is what stops the model from treating the
        # plan template as a record of what happened.
        if plan:
            d14 = (today - timedelta(days=14)).isoformat()
            try:
                acts_14d = await self.db.get_strava_activities_range(
                    d14, str(today)
                )
            except Exception as e:
                logger.debug(f"Tiered context: 14-day Strava fetch failed: {e}")
                acts_14d = []
            adherence_lines = self._compute_plan_adherence(
                plan, acts_14d, recent_lifts
            )
            if adherence_lines:
                lines.append("")
                for a in adherence_lines:
                    lines.append(a)

        if recent_lifts:
            lines.append("")
            lines.append("RECENT LIFTS (last 14 days, self-reported):")
            for lift in recent_lifts[:20]:
                lines.append(
                    f"  - {lift.get('date')} | "
                    f"{lift.get('exercise')} | {lift.get('details')}"
                )

        lines.append("")
        lines.append(
            "NOTE: This is a quick snapshot. For trend / multi-week / month-over-"
            "month / specific-date questions, call get_whoop_aggregates / "
            "query_correlated_runs / get_strava_aggregates directly."
        )
        return "\n".join(lines)

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

        # Active lift session block — same as in the tiered builder, kept
        # at the top so the model can never miss it.
        try:
            session_block = await self.active_session_context_block()
        except Exception as e:
            logger.debug(f"Active session block failed: {e}")
            session_block = ""
        if session_block:
            lines.append("")
            lines.append(session_block)

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

        # ── PLAN ADHERENCE block (prescribed vs actual, current Mon-Sun
        # week + last full week summary). Without this explicit
        # reconciliation, the model conflates the plan template with what
        # actually happened — e.g. assumes Monday was legs because the
        # template says so, even when the Strava activity for that day
        # clearly shows a run. We fetch 14 days so the LAST WEEK summary
        # line has data to render even when it's early in the week.
        try:
            lifts_14d = await self.db.get_recent_lifts(days=14)
        except Exception as e:
            logger.debug(f"14-day lifts fetch for adherence failed: {e}")
            lifts_14d = []
        try:
            acts_14d_for_adherence = await self.db.get_strava_activities_range(
                str(d14), str(today)
            )
        except Exception as e:
            logger.debug(f"14-day Strava fetch for adherence failed: {e}")
            acts_14d_for_adherence = []
        adherence_lines = self._compute_plan_adherence(
            plan, acts_14d_for_adherence, lifts_14d
        )
        if adherence_lines:
            lines.append("")
            for a in adherence_lines:
                lines.append(a)

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
            #
            # For each activity we look up the matching WHOOP workout by
            # (date + ±30min) and pass it to log_strava_activity, which
            # prefers WHOOP's continuous-wrist HR + zone math over Strava's
            # numbers. Without this, new Runs rows would land with HR but
            # no zones — same gap the offline backfill closed.
            for a in activities:
                try:
                    whoop_match = await self.db.find_whoop_workout_for_strava_activity(a)
                    await self.notion.log_strava_activity(a, whoop_workout=whoop_match)
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
        #
        # Guard: if the message ALSO matches a trend/temporal qualifier
        # ("last week", "in March", "over the past month"), the user is
        # asking a historical question, not a post-session debrief. The
        # debrief path only looks back 8h by default and would answer
        # "no recent workout on file". Let trend questions fall through
        # to the layered-context + tools path, which can actually query
        # arbitrary date ranges.
        if _DEBRIEF_INTENT.search(message) and not _TREND_INTENT.search(message):
            logger.info("chat(): routed to debrief_run via intent regex.")
            return await self.debrief_run()

        lift = await self._try_parse_lift(message)
        if lift:
            lift_id = await self.db.log_lift(
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
                ok = await self.notion.log_lift(
                    date=datetime.now().strftime("%Y-%m-%d"),
                    exercise=lift["exercise"],
                    workout=lift.get("workout"),
                    sets=lift.get("sets"),
                    reps=lift.get("reps"),
                    weight_lb=lift.get("weight_lb"),
                    notes=message,
                    lift_id=lift_id,
                )
                if not ok:
                    logger.warning(
                        "Notion lift write returned False for %r — the row is "
                        "in SQLite but did NOT land in Notion. The nightly "
                        "reconciliation pass will retry it.",
                        message[:80],
                    )
            except Exception as e:
                logger.warning(
                    "Notion lift write raised %s: %s — the row is in SQLite "
                    "but did NOT land in Notion. The nightly reconciliation "
                    "pass will retry it.",
                    type(e).__name__,
                    e,
                )

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

        # Tiered context: cheap "today + 3 days" by default, full layered
        # build only when the user's message hints at trend / date-range
        # questions. Single biggest input-token lever after prompt caching.
        context = await self._build_tiered_context(message)

        # Knowledge retrieval is conditional — skip it for "log my bench" /
        # "how am I today" / "good morning" since those don't need a fitness
        # textbook in the prompt. Adds 1–2K tokens when on; we only pay it
        # when the message asks an educational/conceptual question.
        knowledge = (
            self._retrieve_knowledge(message)
            if _KNOWLEDGE_INTENT.search(message or "")
            else ""
        )

        # Tools are off by default. Enable only when the message looks like
        # it'll need a database lookup (trend question or knowledge question
        # with date phrases). Most chat ("log my squat", "how am I today")
        # never enters a tool loop. With 3-iter cap and lean chat system
        # prompt, the savings stack.
        wants_tools = bool(_TREND_INTENT.search(message or ""))

        prompt = CHAT_PROMPT.format(
            message=message, context=context, knowledge=knowledge
        )
        return await self._ask_claude(
            prompt,
            use_history=True,
            allow_tools=wants_tools,
            model=self.chat_model,
            system=CHAT_SYSTEM_PROMPT,
            max_tool_iters=3,
        )

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

    # ── Active lift session (set-by-set guided workout) ────────────────────
    #
    # Driven by /liftstart and /liftend. While a session is active, the
    # Discord message handler routes every user message through
    # handle_session_message() instead of chat(). Each message is interpreted
    # as the result of the current set ("155 x 6", "6 reps", "skip", "done"),
    # logged to the lifts table, and replied to with the next prompt.
    #
    # Session state lives in the active_lift_session table (singleton).
    # 2-hour silence auto-ends the session on next interaction.

    LIFT_SESSION_TIMEOUT_HOURS = 2

    async def _parse_prescription_to_exercises(
        self, prescription: str
    ) -> list[dict]:
        """Use Haiku to turn a freeform prescription string into a structured
        exercise list.

        Input example:
          "Main: bench press 4x6-8 working up to RPE 8. Assistance:
           overhead press 3x8, incline DB press 3x10..."

        Output:
          [{"name": "Bench press", "sets": 4, "reps": "6-8",
            "notes": "Main, work up to RPE 8"}, ...]
        """
        if not prescription:
            return []
        parse_prompt = f"""
Parse this lift workout prescription into a structured list of exercises in
the order they should be performed.

Prescription:
\"\"\"{prescription}\"\"\"

Respond with JSON only. Schema:
{{
  "exercises": [
    {{
      "name": "<exercise name, capitalize properly e.g. 'Bench press'>",
      "sets": <integer>,
      "reps": "<rep target as string — '6', '6-8', '5', '12', 'AMRAP'>",
      "notes": "<short note like 'Main, RPE 8' or 'Assistance' or empty>"
    }}
  ]
}}

Rules:
  • One entry per distinct exercise.
  • If sets aren't stated, default to 3.
  • If reps aren't stated, use "?".
  • Skip warmup-only items unless they have a load.
  • Skip non-lift items (cardio, mobility) — only return weighted resistance work.
  • Order matches the order in the prescription.
""".strip()
        try:
            resp = await self.claude.messages.create(
                model=self.cheap_model,
                max_tokens=800,
                messages=[{"role": "user", "content": parse_prompt}],
            )
            raw = resp.content[0].text
            start = raw.find("{")
            end = raw.rfind("}") + 1
            data = json.loads(raw[start:end])
            out: list[dict] = []
            for ex in data.get("exercises") or []:
                name = (ex.get("name") or "").strip()
                if not name:
                    continue
                try:
                    sets = int(ex.get("sets") or 3)
                except (TypeError, ValueError):
                    sets = 3
                reps = str(ex.get("reps") or "?").strip()
                notes = (ex.get("notes") or "").strip()
                out.append({"name": name, "sets": sets, "reps": reps, "notes": notes})
            return out
        except Exception as e:
            logger.warning(f"Prescription parse failed: {e}")
            return []

    @staticmethod
    def _extract_weight_from_details(details: str) -> Optional[float]:
        """Pull the first plausible 'weight in lb' number out of a free-form
        details string like '3x10 at 145' or '5x5 @ 185 lb' or '3x10 145lb'.

        Conservative: returns None on anything ambiguous so we don't
        recommend off a misparse.
        """
        if not details:
            return None
        # Prefer "@ NUMBER" or "at NUMBER", then a NUMBERlb pattern, then any
        # 3-digit number that isn't a set/rep scheme.
        m = re.search(r"(?:@|at)\s*(\d{2,4}(?:\.\d+)?)", details, re.I)
        if not m:
            m = re.search(r"(\d{2,4}(?:\.\d+)?)\s*(?:lb|lbs|pounds)\b", details, re.I)
        if not m:
            # Last resort: last standalone number not adjacent to 'x'
            candidates = re.findall(r"(?<![x×])\b(\d{2,4}(?:\.\d+)?)\b(?![x×])", details)
            if candidates:
                m_val = candidates[-1]
                try:
                    return float(m_val)
                except ValueError:
                    return None
            return None
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            return None

    async def _recommend_for_exercise(self, exercise_name: str) -> tuple[Optional[float], str]:
        """Return (recommended_weight_lb, human_readable_note).

        Strategy: look at the most recent logged session for this exercise.
        If we hit our reps cleanly last time, suggest +5 lb. Otherwise,
        suggest the same weight again. If we have nothing on file, return
        (None, "No history yet — pick a working weight.").
        """
        rows = await self.db.get_lifts_for_exercise(exercise_name, limit=3)
        if not rows:
            return None, "No history yet — pick a working weight."
        last = rows[0]
        last_w = self._extract_weight_from_details(last.get("details") or "")
        if last_w is None:
            return None, f"Last time ({last['date']}): {last['details']}. Pick from feel."
        # Simple progression rule: small jump for upper body, slightly bigger
        # for lower body. Big compounds like squat/deadlift get +10; everything
        # else +5. This is a hint, not a prescription — Dylan can override.
        big_lower = re.search(r"\bsquat|deadlift\b", exercise_name, re.I)
        bump = 10 if big_lower else 5
        suggestion = last_w + bump
        return suggestion, (
            f"Last time ({last['date']}): {last['details']}. "
            f"Try {suggestion:g} lb — push to +{bump} if last set felt clean."
        )

    def _session_expired(self, session: dict) -> bool:
        """True if the session has been idle past the timeout."""
        try:
            last = datetime.fromisoformat(session["last_activity_at"])
        except Exception:
            return False
        return datetime.now() - last > timedelta(hours=self.LIFT_SESSION_TIMEOUT_HOURS)

    async def _format_next_set_prompt(self, session: dict) -> str:
        """Render the 'Bench, set 2 — recommended X x Y' prompt for the
        current cursor position. Pulls a recommendation from history.
        """
        ex_idx = session["current_exercise_idx"]
        set_idx = session["current_set_idx"]
        exercises = session["exercises"]
        if ex_idx >= len(exercises):
            return "All planned exercises done. `/liftend` to close out."
        ex = exercises[ex_idx]
        name = ex.get("name", "Exercise")
        total_sets = ex.get("sets", 3)
        reps = ex.get("reps", "?")
        notes = ex.get("notes", "")
        rec_w, rec_note = await self._recommend_for_exercise(name)
        head = f"**{name}** — set {set_idx + 1}/{total_sets} · target {reps} reps"
        if notes:
            head += f"  _({notes})_"
        if rec_w is not None:
            line2 = f"Recommended: **{rec_w:g} lb × {reps}**"
        else:
            line2 = "Recommended: pick a working weight"
        line3 = f"_{rec_note}_"
        line4 = "Reply with what you actually did (e.g. `155 x 6`, `6`, `skip`, `done`)."
        return "\n".join([head, line2, line3, line4])

    async def start_lift_session(self, force: bool = False) -> str:
        """Begin a guided lift session from today's planned prescription.

        Returns the first set's prompt as a Discord-ready string. If a
        session is already in progress, ends it first (the new one wins).
        If today's plan day isn't a lift day, returns an error message
        unless force=True.
        """
        plan = await self.db.get_active_plan()
        if not plan:
            return "No active training plan. Set one up before starting a session."
        today_name = datetime.now().strftime("%A").lower()
        sess_def = (plan.get("weekly_template") or {}).get(today_name)
        if not sess_def:
            return f"No session defined for {today_name}. `/plan week` to see what's scheduled."
        stype = (sess_def.get("session_type") or "").lower()
        if stype != "lift" and not force:
            focus = sess_def.get("focus", "")
            return (
                f"Today ({today_name}) is a **{stype}** day ({focus}), not a lift day.\n"
                "If you're lifting anyway, run `/liftstart force:true`."
            )
        prescription = sess_def.get("prescription", "")
        exercises = await self._parse_prescription_to_exercises(prescription)
        if not exercises:
            return (
                "Couldn't parse today's prescription into structured exercises. "
                "Check `/plan today` and try again."
            )
        focus = sess_def.get("focus", "lift")
        await self.db.start_lift_session(workout_label=focus, exercises=exercises)
        session = await self.db.get_active_lift_session()
        ex_summary = ", ".join(
            f"{e['name']} {e['sets']}x{e['reps']}" for e in exercises[:5]
        )
        if len(exercises) > 5:
            ex_summary += f", +{len(exercises) - 5} more"
        header = (
            f"💪 Lift session started — **{focus}**\n"
            f"Plan: {ex_summary}\n"
            "—\n"
        )
        return header + await self._format_next_set_prompt(session)

    async def handle_session_message(self, message: str) -> str:
        """Interpret a message as the result of the current set.

        Recognized inputs:
          - 'NUMBER x NUMBER'  → weight x reps   ("155 x 6", "155x6")
          - 'NUMBER'           → reps at the recommended weight ("6")
          - 'NUMBER lb'        → weight (assume the target reps)
          - 'skip'             → skip this set, advance one
          - 'done' / 'next'    → mark this exercise complete, advance to next
          - 'stop' / 'end'     → end the session
          - anything else      → re-prompt with help text

        Returns the next prompt as a Discord-ready string.
        """
        session = await self.db.get_active_lift_session()
        if not session:
            return "No active lift session. Start one with `/liftstart`."
        if self._session_expired(session):
            await self.db.end_lift_session()
            return (
                f"Last session timed out after {self.LIFT_SESSION_TIMEOUT_HOURS}h "
                "of silence. `/liftstart` to begin a fresh one."
            )

        msg = (message or "").strip()
        low = msg.lower()
        ex_idx = session["current_exercise_idx"]
        set_idx = session["current_set_idx"]
        exercises = session["exercises"]
        history = session["history"]

        if ex_idx >= len(exercises):
            return "All planned exercises done. `/liftend` to close out."
        ex = exercises[ex_idx]
        ex_name = ex["name"]
        total_sets = ex["sets"]

        # ── Stop ───────────────────────────────────────────────────────────
        if low in ("stop", "end", "/liftend", "liftend"):
            return await self.end_lift_session()

        # ── Skip a set ─────────────────────────────────────────────────────
        if low in ("skip", "pass"):
            history.append({
                "exercise": ex_name,
                "set": set_idx + 1,
                "skipped": True,
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
            new_set_idx = set_idx + 1
            new_ex_idx = ex_idx
            if new_set_idx >= total_sets:
                new_set_idx = 0
                new_ex_idx = ex_idx + 1
            await self.db.update_lift_session_position(
                current_exercise_idx=new_ex_idx,
                current_set_idx=new_set_idx,
                history=history,
            )
            session = await self.db.get_active_lift_session()
            return "Skipped.\n—\n" + await self._format_next_set_prompt(session)

        # ── Done with this exercise (advance to next) ──────────────────────
        if low in ("done", "next", "next exercise"):
            new_ex_idx = ex_idx + 1
            await self.db.update_lift_session_position(
                current_exercise_idx=new_ex_idx,
                current_set_idx=0,
                history=history,
            )
            session = await self.db.get_active_lift_session()
            if new_ex_idx >= len(exercises):
                return await self.end_lift_session()
            return f"Marking **{ex_name}** complete.\n—\n" + await self._format_next_set_prompt(session)

        # ── Parse "WEIGHT x REPS" or "REPS" or "WEIGHT lb" ─────────────────
        weight: Optional[float] = None
        reps: Optional[int] = None

        m = re.match(r"^\s*(\d{1,4}(?:\.\d+)?)\s*[x×]\s*(\d{1,3})\s*$", msg, re.I)
        if m:
            weight = float(m.group(1))
            reps = int(m.group(2))
        else:
            m = re.match(r"^\s*(\d{1,4}(?:\.\d+)?)\s*(?:lb|lbs|pounds)\b\s*$", msg, re.I)
            if m:
                weight = float(m.group(1))
            else:
                m = re.match(r"^\s*(\d{1,3})\s*(?:reps?)?\s*$", msg, re.I)
                if m:
                    reps = int(m.group(1))

        if weight is None and reps is None:
            return (
                "Couldn't read that as a set result. Reply with one of:\n"
                "  `155 x 6`  (weight × reps)\n"
                "  `6`        (reps at the recommended weight)\n"
                "  `155 lb`   (weight at the target reps)\n"
                "  `skip` / `done` / `stop`"
            )

        # If only reps given, fall back to the recommended weight; if only
        # weight given, fall back to the rep target.
        if weight is None:
            rec_w, _ = await self._recommend_for_exercise(ex_name)
            weight_str = f"{rec_w:g}" if rec_w is not None else "bw"
        else:
            weight_str = f"{weight:g}"
        if reps is None:
            reps_str = str(ex.get("reps") or "?")
        else:
            reps_str = str(reps)

        details = f"set {set_idx + 1}/{total_sets} · {weight_str} lb × {reps_str}"
        # Persist into the same lifts table the rest of the bot reads.
        lift_id = await self.db.log_lift(
            date=datetime.now().strftime("%Y-%m-%d"),
            exercise=ex_name,
            details=details,
            raw=f"[liftstart] {message}",
        )
        # Mirror to Notion if wired (best-effort; never blocks). Failure is
        # logged at WARNING so we hear about it — the reconciliation pass
        # picks up anything we miss here on its next run.
        try:
            ok = await self.notion.log_lift(
                date=datetime.now().strftime("%Y-%m-%d"),
                exercise=ex_name,
                workout=session.get("workout_label"),
                sets=1,
                reps=reps,
                weight_lb=weight,
                notes=f"[liftstart] set {set_idx + 1}/{total_sets} · target {ex.get('reps')}",
                lift_id=lift_id,
            )
            if not ok:
                logger.warning(
                    "Notion lift write returned False for %s set %d/%d — "
                    "row in SQLite but not Notion; reconciliation will retry.",
                    ex_name, set_idx + 1, total_sets,
                )
        except Exception as e:
            logger.warning(
                "Notion lift write raised %s for %s set %d/%d: %s — "
                "row in SQLite but not Notion; reconciliation will retry.",
                type(e).__name__, ex_name, set_idx + 1, total_sets, e,
            )

        history.append({
            "exercise": ex_name,
            "set": set_idx + 1,
            "weight_lb": weight,
            "reps": reps,
            "ts": datetime.now().isoformat(timespec="seconds"),
        })
        new_set_idx = set_idx + 1
        new_ex_idx = ex_idx
        finished_exercise = False
        if new_set_idx >= total_sets:
            finished_exercise = True
            new_set_idx = 0
            new_ex_idx = ex_idx + 1

        await self.db.update_lift_session_position(
            current_exercise_idx=new_ex_idx,
            current_set_idx=new_set_idx,
            history=history,
        )

        ack = f"Logged: **{ex_name}** set {set_idx + 1}/{total_sets} · {weight_str} lb × {reps_str}."
        if new_ex_idx >= len(exercises):
            return ack + "\n—\n" + await self.end_lift_session()
        session = await self.db.get_active_lift_session()
        sep = "\n—\n"
        if finished_exercise:
            sep = f"\n✅ {ex_name} done.\n—\n"
        return ack + sep + await self._format_next_set_prompt(session)

    async def end_lift_session(self) -> str:
        """Close the active session and return a short summary."""
        session = await self.db.end_lift_session()
        if not session:
            return "No active session to end."
        history = session.get("history") or []
        label = session.get("workout_label") or "lift"
        if not history:
            return f"Session ended — **{label}**. No sets logged."
        # Group by exercise for the recap.
        by_ex: dict[str, list[dict]] = {}
        for entry in history:
            by_ex.setdefault(entry.get("exercise", "?"), []).append(entry)
        lines = [f"✅ Session complete — **{label}**", ""]
        for ex_name, entries in by_ex.items():
            sets_done = sum(1 for e in entries if not e.get("skipped"))
            skipped = sum(1 for e in entries if e.get("skipped"))
            tail = ""
            real = [e for e in entries if not e.get("skipped")]
            if real:
                last = real[-1]
                if last.get("weight_lb") and last.get("reps"):
                    tail = f" — top: {last['weight_lb']:g} lb × {last['reps']}"
            skip_str = f" ({skipped} skipped)" if skipped else ""
            lines.append(f"  • {ex_name}: {sets_done} sets{skip_str}{tail}")
        return "\n".join(lines)

    async def active_session_context_block(self) -> str:
        """Render a context block describing the in-flight session, for
        injection into chat context. Returns '' if no session is active.

        Consumed by _build_tiered_context and _build_layered_context so the
        free-form chat path (e.g. when Dylan asks 'how's my recovery' mid-
        workout) knows there's a workout in progress and doesn't suggest a
        run or unrelated activity.
        """
        session = await self.db.get_active_lift_session()
        if not session:
            return ""
        if self._session_expired(session):
            await self.db.end_lift_session()
            return ""
        ex_idx = session["current_exercise_idx"]
        set_idx = session["current_set_idx"]
        exercises = session["exercises"]
        label = session.get("workout_label") or "lift"
        lines = [
            f"ACTIVE LIFT SESSION IN PROGRESS — {label}",
            f"  Started: {session['started_at']}",
        ]
        if ex_idx < len(exercises):
            ex = exercises[ex_idx]
            lines.append(
                f"  Currently on: {ex['name']} (set {set_idx + 1}/{ex['sets']})"
            )
        remaining = exercises[ex_idx:]
        if remaining:
            lines.append("  Remaining: " + ", ".join(
                f"{e['name']} {e['sets']}x{e['reps']}" for e in remaining
            ))
        lines.append(
            "  → Do NOT suggest cardio, runs, or unrelated activities. "
            "Stay focused on the in-progress workout."
        )
        return "\n".join(lines)
