"""
Scheduler — sends the daily morning brief and weekly summary automatically.
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

        # Daily brief
        if now.hour == self.brief_hour and now.minute == self.brief_minute:
            await self._send_daily_brief()

        # Weekly summary — Sundays at 8pm
        if now.weekday() == 6 and now.hour == 20 and now.minute == 0:
            await self._send_weekly_summary()

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
        logger.info("Sending weekly summary...")
        summary = await self.coach.weekly_summary()
        await channel.send(summary)

    @check_scheduled_tasks.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
