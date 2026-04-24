"""
Scheduler — sends the daily morning brief, weekly training summary,
and Sunday Stoic reflection automatically. Also runs a nightly incremental
sync of WHOOP + Strava so the SQLite history stays current without a manual
re-run of sync_history.py.

Daily-brief trigger (why this is more complex than a cron):
The brief used to fire at a fixed 7:30 AM local time. The problem: if Dylan
hadn't yet synced his WHOOP (phone still charging, watch mid-sync, etc.),
the "today" snapshot silently returned yesterday's recovery. The brief came
out with stale numbers and stale advice.

New behavior: inside a poll window (default 05:30–10:00 local), check every
10 minutes whether WHOOP has posted a recovery record dated today. The first
time we see one, fire the brief. If we hit the backstop time (10:00) without
seeing a fresh record, fire anyway with whatever's available — better a
slightly-stale brief than no brief. "Fired today" state is in-memory and
resets at local midnight.
"""

import logging
from datetime import datetime, timedelta, timezone
import pytz
from discord.ext import tasks

logger = logging.getLogger(__name__)


def _parse_hhmm(s: str) -> tuple[int, int]:
    hh, mm = s.split(":")
    return int(hh), int(mm)


def _chunk_for_discord(text: str, limit: int = 1990) -> list[str]:
    """Split a long message into Discord-safe chunks (2000-char cap).

    Duplicated from bot/discord_bot.py to avoid a circular import at module
    load — bot/discord_bot.py already imports Scheduler from this module.
    Prefer paragraph > line > space > hard-cut so replies don't split mid-word.
    """
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class Scheduler:
    def __init__(self, bot, config, coach):
        self.bot = bot
        self.config = config
        self.coach = coach
        self.tz = pytz.timezone(config.TIMEZONE)

        # Poll-window bounds for the morning brief.
        self.poll_start_h, self.poll_start_m = _parse_hhmm(config.DAILY_BRIEF_POLL_START)
        self.backstop_h, self.backstop_m = _parse_hhmm(config.DAILY_BRIEF_BACKSTOP)

        # In-memory "did the brief fire today?" state. Keyed by local date.
        self._brief_fired_on: str | None = None  # ISO date string, or None
        # Throttle the WHOOP check to once every N minutes (we're called every
        # minute by the loop; no need to hit WHOOP that often).
        self._last_whoop_check: datetime | None = None
        # Cached owner discord.User, fetched lazily on first DM send. Scheduled
        # briefs used to post to DISCORD_CHANNEL_DAILY; they now DM the owner
        # directly (a channel is noise for a personal coach).
        self._owner_user = None

    def start(self):
        self.check_scheduled_tasks.start()
        logger.info(
            "Scheduler started. Morning brief window: "
            f"{self.config.DAILY_BRIEF_POLL_START}–{self.config.DAILY_BRIEF_BACKSTOP} "
            f"{self.config.TIMEZONE}"
        )

    @tasks.loop(minutes=1)
    async def check_scheduled_tasks(self):
        now = datetime.now(self.tz)
        today_iso = now.date().isoformat()

        # Reset the "fired today" flag on day rollover.
        if self._brief_fired_on and self._brief_fired_on != today_iso:
            self._brief_fired_on = None

        # ── Daily morning brief — data-driven window ───────────────────────
        if self._brief_fired_on != today_iso:
            await self._maybe_fire_daily_brief(now, today_iso)

        # Weekly training summary — Sundays at 7:00pm
        if now.weekday() == 6 and now.hour == 19 and now.minute == 0:
            await self._send_weekly_summary()

        # Sunday Stoic reflection — Sundays at 8:30pm (after summary)
        if now.weekday() == 6 and now.hour == 20 and now.minute == 30:
            await self._send_stoic_reflection()

        # Nightly incremental sync — 3:05 AM local
        if now.hour == 3 and now.minute == 5:
            await self._nightly_sync()

    async def _maybe_fire_daily_brief(self, now: datetime, today_iso: str):
        """Decide whether to fire today's brief.

        Three gates:
          1. Before poll-start → no-op.
          2. Inside window: every ~10 min, check WHOOP for a record dated
             today. If present → fire. If absent → wait.
          3. At/past backstop → fire regardless of WHOOP state.
        """
        start_min = self.poll_start_h * 60 + self.poll_start_m
        back_min = self.backstop_h * 60 + self.backstop_m
        now_min = now.hour * 60 + now.minute

        if now_min < start_min:
            return  # too early

        if now_min >= back_min:
            logger.info("Backstop time reached without fresh WHOOP record — firing brief anyway.")
            await self._send_daily_brief(reason="backstop")
            self._brief_fired_on = today_iso
            return

        # Inside the poll window. Throttle the WHOOP check.
        if self._last_whoop_check is not None:
            since = (now - self._last_whoop_check).total_seconds()
            if since < 600:  # 10 minutes
                return
        self._last_whoop_check = now

        try:
            fresh = await self._whoop_has_today_recovery(now)
        except Exception as e:
            logger.warning(f"WHOOP freshness check failed: {e}")
            fresh = False

        if fresh:
            logger.info("Fresh WHOOP recovery detected for today — firing brief.")
            await self._send_daily_brief(reason="fresh-whoop")
            self._brief_fired_on = today_iso

    async def _whoop_has_today_recovery(self, now_local: datetime) -> bool:
        """Ask WHOOP whether a recovery record for today's local date exists yet.

        WHOOP returns recoveries timestamped in UTC. A recovery calculated
        from a sleep that ended this morning will have `created_at` within
        the last few hours. We consider it "today's" if its timestamp,
        converted to local tz, falls on today's local date.
        """
        records = await self.coach.whoop.get_recovery(days=1)
        if not records:
            return False
        today = now_local.date()
        for rec in records:
            ts = rec.get("created_at") or rec.get("updated_at")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local_date = dt.astimezone(self.tz).date()
            except Exception:
                continue
            if local_date == today:
                return True
        return False

    async def _get_owner_dm(self):
        """Fetch the owner's discord.User for DMing, cached after first call.

        Why this replaced bot.get_channel(DISCORD_CHANNEL_DAILY):
        Dylan moved the personal training stream off a shared channel and into
        a DM — the briefs are for one reader, not a feed. discord.py opens the
        DM channel implicitly on the first User.send(), so we only need the
        User object. fetch_user hits the REST API if it isn't cached; the
        result is stable, so we memoize.
        """
        if self.config.OWNER_USER_ID == 0:
            logger.warning("OWNER_USER_ID is not set — cannot DM the owner.")
            return None
        if self._owner_user is not None:
            return self._owner_user
        try:
            self._owner_user = await self.bot.fetch_user(self.config.OWNER_USER_ID)
            return self._owner_user
        except Exception as e:
            logger.error(
                f"Failed to fetch owner user {self.config.OWNER_USER_ID}: {e}"
            )
            return None

    async def _dm_owner(self, text: str) -> bool:
        """Send text to the owner as a DM, chunked to Discord's 2000-char cap.

        Returns True on success. On failure (DMs closed, user fetch failure,
        Discord API error), logs and returns False so the caller can stay
        quiet rather than crashing the scheduler loop.
        """
        user = await self._get_owner_dm()
        if user is None:
            return False
        try:
            for chunk in _chunk_for_discord(text):
                await user.send(chunk)
            return True
        except Exception as e:
            # Most common cause: the owner has "Allow direct messages from
            # server members" disabled for the server the bot shares, or the
            # bot and owner don't share a guild. We can't fix that from here
            # — just log so the failure is visible.
            logger.error(f"DM to owner failed: {e}")
            return False

    async def _send_daily_brief(self, reason: str = ""):
        logger.info(f"Sending daily brief (reason={reason})...")
        # Upsert today (and yesterday, for safety) into SQLite so the 7-day
        # block in the context actually shows today as a row, not a gap.
        try:
            await self._refresh_recent_whoop_into_db(days=2)
        except Exception as e:
            logger.warning(f"Pre-brief WHOOP refresh failed (non-fatal): {e}")
        brief = await self.coach.daily_brief()
        await self._dm_owner(brief)

    async def _refresh_recent_whoop_into_db(self, days: int = 2):
        """Quick upsert of the last N days of WHOOP data into SQLite.

        Same code path as the nightly sync, just a tighter window. Runs in a
        couple of seconds. Makes today's row available in the 7-day block
        rather than relying on the live snapshot alone. Also refreshes
        per-session workouts so /debrief has a warm cache even when a push
        was missed.
        """
        whoop = self.coach.whoop
        db = self.coach.db
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")
        end = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        async for rec in whoop.iter_all_recovery(start=start, end=end):
            date, row = whoop.normalize_recovery(rec)
            if date:
                await db.upsert_whoop_recovery(date, row, rec)
        async for rec in whoop.iter_all_sleep(start=start, end=end):
            date, row = whoop.normalize_sleep(rec)
            if date:
                await db.upsert_whoop_sleep(date, row, rec)
        async for rec in whoop.iter_all_cycles(start=start, end=end):
            date, row = whoop.normalize_cycle(rec)
            if date:
                await db.upsert_whoop_cycle(date, row, rec)
        async for rec in whoop.iter_all_workouts(start=start, end=end):
            try:
                row = whoop.normalize_workout(rec)
                await db.upsert_whoop_workout(row, rec)
            except Exception as e:
                logger.debug(f"Workout upsert failed during refresh: {e}")

    async def _send_weekly_summary(self):
        logger.info("Sending weekly training summary...")
        summary = await self.coach.weekly_summary()
        await self._dm_owner(f"**Weekly Training Summary**\n\n{summary}")

    async def _send_stoic_reflection(self):
        logger.info("Sending Sunday Stoic reflection...")
        reflection = await self.coach.stoic_reflection()
        await self._dm_owner(f"**Sunday Reflection**\n\n{reflection}")

    async def _nightly_sync(self):
        """Pull the last couple days of WHOOP + Strava into SQLite.

        With webhooks wired up (integrations/webhook_server.py), the nightly
        job is a safety-net catching events we missed — dropped pushes,
        WHOOP re-scorings, Strava edits — rather than the primary ingest.
        A 2-day window is enough overlap for that; bigger windows just burn
        API quota without surfacing new information. Upserts make this safe.

        Also pulls per-session WHOOP workouts (/v2/activity/workout), which
        are the authoritative source for per-run HR used by /debrief — the
        day-level /v2/cycle we were using before returns 24h averages that
        are useless for grading a single run.
        """
        logger.info("Running nightly incremental sync…")
        try:
            whoop = self.coach.whoop
            db = self.coach.db
            now = datetime.utcnow()
            start = (now - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00.000Z")
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
            # Per-session workouts — the source /debrief reads for HR + zones.
            count_w = 0
            async for rec in whoop.iter_all_workouts(start=start, end=end):
                try:
                    row = whoop.normalize_workout(rec)
                    await db.upsert_whoop_workout(row, rec)
                    count_w += 1
                except Exception as e:
                    logger.debug(f"Workout upsert failed: {e}")
            await db.set_sync_state(
                "whoop",
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                last_record_date=datetime.utcnow().strftime("%Y-%m-%d"),
                note="nightly",
            )
            logger.info(
                f"WHOOP nightly sync: {count_r} recovery, {count_s} sleep, "
                f"{count_c} cycles, {count_w} workouts."
            )

            # Strava: walk back 2 days to catch edits/delayed uploads.
            # Webhooks handle everything fresh; this is the safety net only.
            after_ts = int((now - timedelta(days=2)).timestamp())
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
