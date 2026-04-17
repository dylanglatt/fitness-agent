"""
Scheduler — sends the daily morning brief, weekly training summary,
and Sunday Stoic reflection automatically.

Replace the contents of bot/scheduler.py with this file.
"""

import logging
from datetime import datetime
import pytz
from discord.ext import tasks

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, bot, config, coach):
        self.bot = bot
        self.config = config
        self.coach = coach
        self.tz = pytz.timezone(config.TIMEZONE)

        # Parse configured time
        hour, minute = map(int, config.DAILY_BRIEF_TIME.split(":"))
        self.brief_hour = hour
        self.brief_minute = minute

    def start(self):
        self.check_scheduled_tasks.start()
        logger.info(f"Scheduler started. Daily brief at {self.config.DAILY_BRIEF_TIME} {self.config.TIMEZONE}")

    @tasks.loop(minutes=1)
    async def check_scheduled_tasks(self):
        now = datetime.now(self.tz)

        # Daily morning brief
        if now.hour == self.brief_hour and now.minute == self.brief_minute:
            await self._send_daily_brief()

        # Weekly training summary — Sundays at 7:00pm
        if now.weekday() == 6 and now.hour == 19 and now.minute == 0:
            await self._send_weekly_summary()

        # Sunday Stoic reflection — Sundays at 8:30pm (after summary)
        if now.weekday() == 6 and now.hour == 20 and now.minute == 30:
            await self._send_stoic_reflection()

    async def _send_daily_brief(self):
        channel = self.bot.get_channel(self.config.DISCORD_CHANNEL_DAILY)
        if not channel:
            logger.warning("Daily channel not found.")
            return
        logger.info("Sending daily brief...")
        brief = await self.coach.daily_brief()
        await channel.send(brief)

    async def _send_weekly_summary(self):
        channel = self.bot.get_channel(self.config.DISCORD_CHANNEL_DAILY)
        if not channel:
            return
        logger.info("Sending weekly training summary...")
        summary = await self.coach.weekly_summary()
        await channel.send(f"**Weekly Training Summary**\n\n{summary}")

    async def _send_stoic_reflection(self):
        channel = self.bot.get_channel(self.config.DISCORD_CHANNEL_DAILY)
        if not channel:
            return
        logger.info("Sending Sunday Stoic reflection...")
        reflection = await self.coach.stoic_reflection()
        await channel.send(f"**Sunday Reflection**\n\n{reflection}")

    @check_scheduled_tasks.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
