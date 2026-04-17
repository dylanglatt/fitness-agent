"""
Scheduler — sends the daily morning brief, weekly training summary,
and Sunday Stoic reflection automatically. Also runs a nightly incremental
sync of WHOOP + Strava so the SQLite history stays current without a manual
re-run of sync_history.py.
"""

import logging
from datetime import datetime, timedelta, timezone
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

        # Nightly incremental sync — 3:05 AM local (after WHOOP usually finishes
        # scoring the previous night's sleep, before the morning brief at 7:30).
        if now.hour == 3 and now.minute == 5:
            await self._nightly_sync()

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

    async def _nightly_sync(self):
        """Pull the last few days of WHOOP + Strava into SQLite.

        Uses a generous overlap window (3 days for WHOOP, 7 days for Strava)
        so late-arriving records (WHOOP re-scoring, Strava edits, delayed sync
        from the watch) get picked up. Upserts make this safe — re-writing an
        existing row just refreshes it.
        """
        logger.info("Running nightly incremental sync…")
        try:
            whoop = self.coach.whoop
            db = self.coach.db
            now = datetime.utcnow()
            start = (now - timedelta(days=3)).strftime("%Y-%m-%dT00:00:00.000Z")
            end = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            count_r = 0
            async for rec in whoop.iter_all_recovery(start=start, end=end):
                date, row = whoop.normalize_recovery(rec)
                if date:
                    await db.upsert_whoop_recovery(date, row, rec)
                    count_r += 1
            count_s = 0
            async for rec in whoop.iter_all_sleep(start=start, end=end):
                date, row = whoop.normalize_sleep(rec)
                if date:
                    await db.upsert_whoop_sleep(date, row, rec)
                    count_s += 1
            count_c = 0
            async for rec in whoop.iter_all_cycles(start=start, end=end):
                date, row = whoop.normalize_cycle(rec)
                if date:
                    await db.upsert_whoop_cycle(date, row, rec)
                    count_c += 1
            await db.set_sync_state(
                "whoop",
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                last_record_date=datetime.utcnow().strftime("%Y-%m-%d"),
                note="nightly",
            )
            logger.info(
                f"WHOOP nightly sync: {count_r} recovery, {count_s} sleep, {count_c} cycles."
            )

            # Strava: walk back 7 days to catch edits/delayed uploads.
            after_ts = int((now - timedelta(days=7)).timestamp())
            count_a = 0
            async for act in self.coach.strava.iter_all_activities(after=after_ts):
                await db.upsert_strava_activity(act)
                count_a += 1
            await db.set_sync_state(
                "strava",
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                last_record_date=datetime.utcnow().strftime("%Y-%m-%d"),
                note="nightly",
            )
            logger.info(f"Strava nightly sync: {count_a} activities touched.")
        except Exception as e:
            logger.error(f"Nightly sync failed: {e}", exc_info=True)

    @check_scheduled_tasks.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
