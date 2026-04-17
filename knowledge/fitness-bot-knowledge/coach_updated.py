"""
AI Coach — orchestrates data fetching, prompt construction, and Claude calls.
This is the brain of the bot.

Replace the contents of ai/coach.py with this file.
"""

import logging
import json
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


class Coach:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.strava = StravaClient(config)
        self.whoop = WhoopClient(config)
        self.notion = NotionClient(config)
        self.claude = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        self.model = config.CLAUDE_MODEL

        # Conversation history for multi-turn chat
        self._conversation: list[dict] = []

    async def _ask_claude(self, user_message: str, use_history: bool = False) -> str:
        """Send a message to Claude and return the response text."""
        messages = []

        if use_history:
            messages = self._conversation.copy()

        messages.append({"role": "user", "content": user_message})

        response = await self.claude.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages,
        )

        reply = response.content[0].text

        if use_history:
            self._conversation.append({"role": "user", "content": user_message})
            self._conversation.append({"role": "assistant", "content": reply})
            # Keep conversation history from growing too large
            if len(self._conversation) > 40:
                self._conversation = self._conversation[-40:]

        return reply

    async def _build_context(self, days: int = 7) -> str:
        """Pull fresh WHOOP + Strava data and format it as context for Claude."""
        try:
            snapshot = await self.whoop.get_today_snapshot()
            recent_activities = await self.strava.get_recent_activities(days=days)
            recent_lifts = await self.db.get_recent_lifts(days=days)
            recent_notes = await self.db.get_recent_notes(days=days)

            recovery_summary = self.whoop.summarize_recovery(snapshot.get("recovery"))
            sleep_summary = self.whoop.summarize_sleep(snapshot.get("sleep"))
            activity_summaries = [self.strava.summarize_activity(a) for a in recent_activities]

            context_parts = [
                f"TODAY ({datetime.now().strftime('%A, %B %d')})",
                f"  {recovery_summary}",
                f"  {sleep_summary}",
                "",
                f"RECENT ACTIVITIES (last {days} days):",
            ]
            for act in activity_summaries[:10]:
                context_parts.append(f"  - {act}")

            if recent_lifts:
                context_parts.append("")
                context_parts.append("RECENT LIFTS (self-reported):")
                for lift in recent_lifts:
                    context_parts.append(f"  - {lift['date']} | {lift['exercise']} | {lift['details']}")

            if recent_notes:
                context_parts.append("")
                context_parts.append("RECENT NOTES:")
                for note in recent_notes:
                    context_parts.append(f"  - {note['date']}: {note['content']}")

            return "\n".join(context_parts)

        except Exception as e:
            logger.error(f"Error building context: {e}")
            return "Unable to fetch current data — responding from memory."

    def _retrieve_knowledge(self, query: str) -> str:
        """Query the RAG knowledge base for relevant content."""
        if _retriever is None:
            return ""
        return _retriever.retrieve(query)

    async def daily_brief(self) -> str:
        """Generate and return the morning brief message."""
        context = await self._build_context(days=2)
        stoic_quote = get_daily_stoic_quote()
        prompt = DAILY_BRIEF_PROMPT.format(data=context, stoic_quote=stoic_quote)
        brief = await self._ask_claude(prompt)

        # Log to Notion in the background (best effort)
        try:
            snapshot = await self.whoop.get_today_snapshot()
            rec = snapshot.get("recovery", {}) or {}
            slp = snapshot.get("sleep", {}) or {}
            activities = await self.strava.get_recent_activities(days=1)

            score = rec.get("score", {})
            sleep_score = slp.get("score", {})
            stage_summary = sleep_score.get("stage_summary", {})

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
                }
            )
        except Exception as e:
            logger.warning(f"Notion log failed silently: {e}")

        return brief

    async def weekly_summary(self) -> str:
        """Generate and return the weekly training summary message."""
        context = await self._build_context(days=7)
        prompt = WEEKLY_SUMMARY_PROMPT.format(data=context)
        return await self._ask_claude(prompt)

    async def stoic_reflection(self) -> str:
        """Generate the Sunday evening Stoic reflection."""
        context = await self._build_context(days=7)
        prompt = SUNDAY_REFLECTION_PROMPT.format(data=context)
        return await self._ask_claude(prompt)

    async def chat(self, message: str) -> str:
        """
        Handle a conversational message from Dylan.
        Detects lift logs and stores them; otherwise responds as coach.
        Injects relevant knowledge base content when available.
        """
        # Try to detect a lift log
        lift = await self._try_parse_lift(message)
        if lift:
            await self.db.log_lift(
                date=datetime.now().strftime("%Y-%m-%d"),
                exercise=lift["exercise"],
                details=lift["details"],
                raw=message,
            )
            # Also log to Notion
            try:
                await self.notion.log_lift(
                    date=datetime.now().strftime("%Y-%m-%d"),
                    exercise=lift["exercise"],
                    sets_reps_weight=lift["details"],
                )
            except Exception:
                pass

        # Get recent context + knowledge base retrieval
        context = await self._build_context(days=3)
        knowledge = self._retrieve_knowledge(message)

        prompt = CHAT_PROMPT.format(
            message=message,
            context=context,
            knowledge=knowledge,
        )
        return await self._ask_claude(prompt, use_history=True)

    async def _try_parse_lift(self, message: str) -> dict | None:
        """
        Ask Claude to parse a potential lift log from a message.
        Returns a dict with 'exercise' and 'details' if it looks like a lift, else None.
        """
        parse_prompt = f"""
Does this message describe a weightlifting exercise? If yes, extract:
- exercise name
- details (sets, reps, weight as described)

Message: "{message}"

Respond with JSON only. If it's a lift: {{"is_lift": true, "exercise": "...", "details": "..."}}
If not: {{"is_lift": false}}
"""
        try:
            raw = await self._ask_claude(parse_prompt)
            start = raw.find("{")
            end = raw.rfind("}") + 1
            data = json.loads(raw[start:end])
            if data.get("is_lift"):
                return {"exercise": data["exercise"], "details": data["details"]}
        except Exception as e:
            logger.debug(f"Lift parse failed: {e}")
        return None
