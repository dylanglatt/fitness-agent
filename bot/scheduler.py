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
slightly-stale brief than no brief.

"Fired today" state is persisted via the sync_state table so a bot restart
between firing time and local midnight does not refire the brief (or the
weekly/Sunday DMs). Weekly summary, Sunday reflection, and nightly sync use
a windowed `now >= target` check combined with a per-day guard — so a
single dropped tick on the 1-minute loop never causes a missed day.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
import pytz
from discord.ext import tasks

import obs
from integrations.whoop import WhoopAuthError

logger = logging.getLogger(__name__)


def _capture(exc: BaseException) -> None:
    """Send an exception to Sentry if it's configured. Never raises."""
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


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
    # Keys in the sync_state table that record the local date on which each
    # scheduled job last fired. Using sync_state (already there for WHOOP /
    # Strava cursors) avoids a new table just to track four counters.
    _STATE_DAILY_BRIEF = "scheduler:daily_brief_last_fired"
    _STATE_WEEKLY = "scheduler:weekly_summary_last_fired"
    _STATE_STOIC = "scheduler:stoic_reflection_last_fired"
    _STATE_NIGHTLY = "scheduler:nightly_sync_last_fired"
    _STATE_HEARTBEAT = "scheduler:heartbeat_last_fired"

    # Heartbeat thresholds. WHOOP posts a recovery every morning, so >36h
    # without one is a real outage (dead token, bot was down, webhook+poll
    # both missed). Strava is intermittent — only flag a long gap so rest
    # days / travel don't page you.
    _HEARTBEAT_WHOOP_STALE_HOURS = 36
    _HEARTBEAT_STRAVA_STALE_DAYS = 14

    def __init__(self, bot, config, coach):
        self.bot = bot
        self.config = config
        self.coach = coach
        self.tz = pytz.timezone(config.TIMEZONE)

        # Poll-window bounds for the morning brief.
        self.poll_start_h, self.poll_start_m = _parse_hhmm(config.DAILY_BRIEF_POLL_START)
        self.backstop_h, self.backstop_m = _parse_hhmm(config.DAILY_BRIEF_BACKSTOP)

        # Throttle the WHOOP check to once every N minutes (we're called every
        # minute by the loop; no need to hit WHOOP that often).
        self._last_whoop_check: datetime | None = None
        # Cached owner discord.User, fetched lazily on first DM send. Scheduled
        # briefs DM the owner directly; a shared channel is noise for a
        # personal coach.
        self._owner_user = None
        # Serializes the "fire today's brief" decision across its two triggers
        # — the 1-minute poll loop and the WHOOP recovery webhook. Without it,
        # both can pass the initial already-fired check before either marks the
        # day done, and the owner gets two identical briefs.
        self._brief_lock = asyncio.Lock()

    def start(self):
        self.check_scheduled_tasks.start()
        logger.info(
            "Scheduler started. Morning brief window: "
            f"{self.config.DAILY_BRIEF_POLL_START}–{self.config.DAILY_BRIEF_BACKSTOP} "
            f"{self.config.TIMEZONE}"
        )

    async def _already_fired_today(self, state_key: str, today_iso: str) -> bool:
        """True if this scheduled job has already recorded `today_iso` as its
        last-fired date. Persisted via the sync_state table, so a restart
        between firing and midnight does NOT cause a refire."""
        try:
            state = await self.coach.db.get_sync_state(state_key)
        except Exception as e:
            # If state lookup fails, fall through to "not fired" — better to
            # risk an occasional duplicate DM after a DB error than to silently
            # skip a real day's brief.
            logger.warning(f"sync_state read failed for {state_key}: {e}")
            return False
        if not state:
            return False
        return (state.get("last_record_date") or "") == today_iso

    async def _mark_fired_today(self, state_key: str, today_iso: str) -> None:
        """Record that this scheduled job fired on `today_iso`."""
        try:
            await self.coach.db.set_sync_state(
                source=state_key,
                last_synced_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                last_record_date=today_iso,
            )
        except Exception as e:
            logger.warning(f"sync_state write failed for {state_key}: {e}")

    async def _run_job(
        self,
        job_name: str,
        fn,
        today_iso: str,
        *,
        rid_prefix: str,
        mark_on_success: str | None,
        mark_on_failure: str | None,
    ) -> bool:
        """Run one scheduled job behind a hard failure boundary.

        Before this existed, every scheduled job ran naked inside the 1-minute
        task loop. An unhandled exception anywhere — an anthropic.APIError, a
        KeyError, a dead WHOOP token — propagated into discord.ext.tasks,
        which is only willing to retry its own connection errors. Anything
        else STOPPED THE LOOP PERMANENTLY, taking the morning brief, the
        weekly summary, the Sunday reflection, the nightly sync AND the health
        heartbeat with it, and leaving one library ERROR line as the only
        evidence. That is a single point of total, silent failure.

        Now: each job is isolated (one failing job cannot skip the others),
        logged CRITICAL with a stack trace, reported to Sentry, and DM'd to
        the owner. The day is then marked fired so the 1-minute loop does not
        retry — and re-alert — sixty times an hour on a persistent fault.

        Returns True if the job ran cleanly.
        """
        with obs.request_scope(rid_prefix):
            try:
                async with obs.timed(logger, f"job.{job_name}", job=job_name):
                    await fn()
            except Exception as e:
                obs.log_event(
                    logger,
                    logging.CRITICAL,
                    "job.failed",
                    job=job_name,
                    err=type(e).__name__,
                    detail=str(e)[:300],
                    retry="tomorrow",
                    exc_info=True,
                )
                _capture(e)
                if mark_on_failure:
                    await self._mark_fired_today(mark_on_failure, today_iso)
                await self._dm_owner(
                    f"🔴 **`{job_name}` failed** and will not retry until "
                    f"tomorrow.\n```{type(e).__name__}: {str(e)[:400]}```"
                )
                return False
            if mark_on_success:
                await self._mark_fired_today(mark_on_success, today_iso)
            return True

    @tasks.loop(minutes=1)
    async def check_scheduled_tasks(self):
        now = datetime.now(self.tz)
        today_iso = now.date().isoformat()

        # ── Daily morning brief — data-driven window ───────────────────────
        # The brief marks its own fired-state inside _fire_daily_brief_once
        # (under the lock that keeps the poll loop and the recovery webhook
        # from double-sending), so only the failure path marks here.
        if not await self._already_fired_today(self._STATE_DAILY_BRIEF, today_iso):
            await self._run_job(
                "daily_brief",
                lambda: self._maybe_fire_daily_brief(now, today_iso),
                today_iso,
                rid_prefix="brief",
                mark_on_success=None,
                mark_on_failure=self._STATE_DAILY_BRIEF,
            )

        # ── Weekly training summary — Sundays from 7:00 PM local ───────────
        # Window-based ("any tick at or after target time, once per day") so a
        # single skipped 1-min tick never silently drops a Sunday.
        if now.weekday() == 6 and (now.hour, now.minute) >= (19, 0):
            if not await self._already_fired_today(self._STATE_WEEKLY, today_iso):
                await self._run_job(
                    "weekly_summary",
                    self._send_weekly_summary,
                    today_iso,
                    rid_prefix="week",
                    mark_on_success=self._STATE_WEEKLY,
                    mark_on_failure=self._STATE_WEEKLY,
                )

        # ── Sunday Stoic reflection — Sundays from 8:30 PM (after summary) ─
        if now.weekday() == 6 and (now.hour, now.minute) >= (20, 30):
            if not await self._already_fired_today(self._STATE_STOIC, today_iso):
                await self._run_job(
                    "stoic_reflection",
                    self._send_stoic_reflection,
                    today_iso,
                    rid_prefix="stoic",
                    mark_on_success=self._STATE_STOIC,
                    mark_on_failure=self._STATE_STOIC,
                )

        # ── Nightly incremental sync — from 3:05 AM local ──────────────────
        if (now.hour, now.minute) >= (3, 5) and now.hour < 5:
            if not await self._already_fired_today(self._STATE_NIGHTLY, today_iso):
                await self._run_job(
                    "nightly_sync",
                    self._nightly_sync,
                    today_iso,
                    rid_prefix="sync",
                    mark_on_success=self._STATE_NIGHTLY,
                    mark_on_failure=self._STATE_NIGHTLY,
                )

        # ── Data-feed heartbeat — once daily from 12:00 local ──────────────
        # Runs after the morning-brief window (backstop 11:30) closes, so a
        # healthy same-morning sync is never mistaken for stale. DMs the owner
        # ONLY when a feed is broken — silence means everything is flowing.
        # This is the guard that turns a silent multi-month outage into a
        # same-day ping.
        if (now.hour, now.minute) >= (12, 0) and now.hour < 14:
            if not await self._already_fired_today(self._STATE_HEARTBEAT, today_iso):
                await self._run_job(
                    "heartbeat",
                    lambda: self._data_health_check(now),
                    today_iso,
                    rid_prefix="beat",
                    mark_on_success=self._STATE_HEARTBEAT,
                    mark_on_failure=self._STATE_HEARTBEAT,
                )

    async def _fire_daily_brief_once(
        self, today_iso: str, reason: str
    ) -> bool:
        """Fire today's brief exactly once, no matter which trigger calls.

        The poll loop and the recovery webhook both race to send the morning
        brief. This serializes them: take the lock, re-check the persisted
        already-fired flag (the check that matters is the one INSIDE the lock),
        send, then mark. Returns True if this call actually sent the brief,
        False if it was already sent today.
        """
        async with self._brief_lock:
            if await self._already_fired_today(self._STATE_DAILY_BRIEF, today_iso):
                return False
            await self._send_daily_brief(reason=reason)
            await self._mark_fired_today(self._STATE_DAILY_BRIEF, today_iso)
            return True

    async def _maybe_fire_daily_brief(self, now: datetime, today_iso: str):
        """Decide whether to fire today's brief.

        Three gates:
          1. Before poll-start → no-op.
          2. Inside window: every ~10 min, check WHOOP for a record dated
             today. If present → fire. If absent → wait.
          3. At/past backstop → fire regardless of WHOOP state.

        Note: with the recovery webhook wired up (on_recovery_webhook), this
        poll loop is the FALLBACK path — it covers the cases where the webhook
        never arrives (bot was down at score time, WHOOP didn't push, the
        public URL was unreachable). On a normal morning the webhook fires the
        brief first and this loop then no-ops via the shared already-fired flag.
        """
        start_min = self.poll_start_h * 60 + self.poll_start_m
        back_min = self.backstop_h * 60 + self.backstop_m
        now_min = now.hour * 60 + now.minute

        if now_min < start_min:
            return  # too early

        if now_min >= back_min:
            fired = await self._fire_daily_brief_once(today_iso, reason="backstop")
            if fired:
                logger.info(
                    "Backstop time reached without a recovery webhook/record — "
                    "fired brief anyway."
                )
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
            fired = await self._fire_daily_brief_once(today_iso, reason="fresh-whoop-poll")
            if fired:
                logger.info("Fresh WHOOP recovery detected by poll — fired brief.")

    async def on_recovery_webhook(self):
        """Fire the brief the moment WHOOP finishes scoring today's recovery.

        Called by the webhook server's recovery handler. Recovery is computed
        from your main sleep, so a recovery.* event is the real "sleep detected
        and processed" signal — this is what lets the brief track your actual
        wake/sync time instead of a fixed clock.

        Guards (any failing → no-op, leaving the poll loop as backstop):
          • Outside the morning window (before poll-start) — don't let a
            midnight re-score or a late nap's recovery fire a 3 AM brief.
          • Already fired today.
          • The event isn't actually for today's local date (WHOOP re-scoring
            an older recovery still sends recovery.updated).
        """
        now = datetime.now(self.tz)
        today_iso = now.date().isoformat()

        start_min = self.poll_start_h * 60 + self.poll_start_m
        if now.hour * 60 + now.minute < start_min:
            logger.info(
                "Recovery webhook arrived before poll-start window — "
                "leaving it to the poll loop."
            )
            return

        if await self._already_fired_today(self._STATE_DAILY_BRIEF, today_iso):
            return

        try:
            fresh = await self._whoop_has_today_recovery(now)
        except Exception as e:
            logger.warning(f"Recovery-webhook freshness check failed: {e}")
            return
        if not fresh:
            logger.info(
                "Recovery webhook fired but no recovery dated today — "
                "likely a re-score of an older record. Ignoring."
            )
            return

        fired = await self._fire_daily_brief_once(today_iso, reason="recovery-webhook")
        if fired:
            logger.info("Daily brief fired from WHOOP recovery webhook.")

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
        obs.log_event(logger, logging.INFO, "brief.start", reason=reason)
        degraded: list[str] = []
        # Upsert today (and yesterday, for safety) into SQLite so the 7-day
        # block in the context actually shows today as a row, not a gap.
        try:
            await self._refresh_recent_whoop_into_db(days=2)
        except Exception as e:
            degraded.append("whoop_refresh")
            obs.source_failed(logger, "whoop_refresh", "brief", e)
        # Reconcile any lifts / activities the real-time path missed since
        # the last brief. Walks SQLite vs Notion for the last 7 days and
        # writes only the gaps (dedup by [liftrow:<id>] / [strava:<id>]
        # markers). Tighter loop than waiting for the 3:05 AM nightly job,
        # which matters when a lift logged at 9 AM never made it to Notion.
        try:
            missing = await self.coach.notion.reconcile_recent(self.coach.db, days=7)
            if missing.get("lifts") or missing.get("activities"):
                obs.log_event(
                    logger,
                    logging.INFO,
                    "notion.reconcile",
                    stage="pre_brief",
                    lifts=missing.get("lifts", 0),
                    activities=missing.get("activities", 0),
                )
        except Exception as e:
            degraded.append("notion_reconcile")
            obs.source_failed(logger, "notion_reconcile", "brief", e)
        async with obs.timed(logger, "brief.done", reason=reason) as ctx:
            brief = await self.coach.daily_brief()
            delivered = await self._dm_owner(brief)
            ctx["chars"] = len(brief or "")
            ctx["delivered"] = bool(delivered)
            ctx["degraded"] = ",".join(degraded) or "-"

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
                obs.source_failed(
                    logger, "whoop_workout_upsert", "brief_refresh", e
                )

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
                    obs.source_failed(
                        logger, "whoop_workout_upsert", "nightly_sync", e
                    )
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

            # Strava: walk back 2 days to catch edits/delayed uploads and
            # upsert into SQLite. We deliberately do NOT push to Notion from
            # this loop. log_strava_activity always CREATES a new page (no
            # by-marker idempotency), so an unconditional write here produced a
            # duplicate row every night — and reconcile_recent only SKIPS rows
            # already present, it never removes the dupes this loop created
            # (the compounding "Lunch Weight Training" duplicates in the June
            # 2026 audit). Notion writes now flow through reconcile_recent
            # below, the single dedup-aware writer. Webhooks still mirror fresh
            # activities to Notion in real time.
            after_ts = int((now - timedelta(days=2)).timestamp())
            count_a = 0
            async for act in self.coach.strava.iter_all_activities(after=after_ts):
                try:
                    act = await self.coach.strava.enrich_activity(act)
                except Exception as e:
                    # This is the call that fetches Detailed fields + HR zones.
                    # Failing it silently is exactly how Notion ends up with
                    # rows that have null Avg HR and null Zone %, with nothing
                    # in the log to say why.
                    obs.source_failed(
                        logger,
                        "strava_enrichment",
                        "nightly_sync",
                        e,
                        activity_id=act.get("id"),
                    )
                await db.upsert_strava_activity(act)
                count_a += 1
            await db.set_sync_state(
                "strava",
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                last_record_date=datetime.utcnow().strftime("%Y-%m-%d"),
                note="nightly",
            )
            logger.info(
                f"Strava nightly sync: {count_a} activities upserted to SQLite "
                f"(Notion writes handled by reconciliation below)."
            )

            # ── Notion reconciliation — fills any gaps the real-time
            # webhook + nightly push both missed. Walks the last N days of
            # SQLite lifts + activities and writes whatever isn't already
            # in Notion (matched by [liftrow:<id>] / [strava:<id>]
            # markers). Cheap query + write only the missing rows.
            try:
                missing = await self.coach.notion.reconcile_recent(
                    db, days=7
                )
                logger.info(
                    f"Notion reconciliation: wrote {missing.get('lifts', 0)} "
                    f"missing lifts and {missing.get('activities', 0)} "
                    f"missing activities."
                )
            except Exception as e:
                logger.warning(f"Notion reconciliation failed: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Nightly sync failed: {e}", exc_info=True)

    async def _data_health_check(self, now: datetime):
        """Daily liveness check on the data feeds. DMs the owner ONLY when
        something is wrong — silence means healthy. Two layers:

          1. Live WHOOP probe — hit /v2/recovery. A WhoopAuthError means the
             refresh token is dead (the exact failure that silently killed
             sync for ~2 months); tell the owner to re-auth. Any other error
             means the API/network is unreachable right now.
          2. DB freshness — compare the newest WHOOP recovery date and Strava
             activity timestamp against the staleness thresholds.

        Problems are collected and sent as a single concise DM.
        """
        problems: list[str] = []

        # 1. Live WHOOP auth / reachability probe.
        #
        # A dead refresh token is THE failure mode that took this system down
        # for a month without anyone noticing: the DM went out, but the catch
        # block logged nothing, so `journalctl` showed a perfectly healthy bot
        # and there was no second place to notice. A dead token is now
        # CRITICAL — the highest severity in the codebase, reserved for
        # exactly two conditions (this and a stopped scheduler loop) — and it
        # goes to Sentry as well as the log and the DM.
        try:
            await self.coach.whoop.get_recovery(days=1)
        except WhoopAuthError as e:
            obs.log_event(
                logger,
                logging.CRITICAL,
                "whoop.token_dead",
                remedy="run whoop_auth.py on the host, then restart the service",
                detail=str(e)[:200],
            )
            _capture(e)
            problems.append(
                "🔴 WHOOP token rejected — recovery/sleep/strain are NOT syncing. "
                "Run `python whoop_auth.py` on the host and restart the service."
            )
        except Exception as e:
            obs.log_event(
                logger,
                logging.WARNING,
                "whoop.unreachable",
                err=type(e).__name__,
                detail=str(e)[:200],
            )
            problems.append(
                f"🟠 WHOOP API unreachable right now ({type(e).__name__})."
            )

        # 2a. WHOOP freshness in the DB.
        try:
            latest_whoop = await self.coach.db.get_latest_whoop_date()
            if latest_whoop:
                last = datetime.strptime(latest_whoop, "%Y-%m-%d").date()
                age_days = (now.date() - last).days
                if age_days * 24 > self._HEARTBEAT_WHOOP_STALE_HOURS:
                    problems.append(
                        f"🟠 No new WHOOP recovery since {latest_whoop} "
                        f"({age_days}d ago)."
                    )
            else:
                problems.append("🟠 No WHOOP recovery records in the database at all.")
        except Exception as e:
            logger.warning(f"Heartbeat WHOOP freshness check failed: {e}")

        # 2b. Strava freshness in the DB (long threshold — runs aren't daily).
        try:
            ts = await self.coach.db.get_latest_strava_timestamp()
            if ts:
                age_days = (now - datetime.fromtimestamp(ts, self.tz)).days
                if age_days > self._HEARTBEAT_STRAVA_STALE_DAYS:
                    problems.append(
                        f"🟠 No Strava activity in {age_days}d "
                        f"(threshold {self._HEARTBEAT_STRAVA_STALE_DAYS}d)."
                    )
        except Exception as e:
            logger.warning(f"Heartbeat Strava freshness check failed: {e}")

        if problems:
            body = "\n".join(f"• {p}" for p in problems)
            await self._dm_owner(f"**⚠️ Data sync health check**\n{body}")
            # Log every problem individually, not just the count. The DM is
            # easy to miss or mute; the log is the durable record.
            for prob in problems:
                obs.log_event(
                    logger, logging.WARNING, "heartbeat.problem", problem=prob
                )
            obs.log_event(
                logger,
                logging.WARNING,
                "heartbeat.done",
                healthy=False,
                problems=len(problems),
                notified=True,
            )
        else:
            obs.log_event(
                logger, logging.INFO, "heartbeat.done", healthy=True, problems=0
            )

    @check_scheduled_tasks.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    @check_scheduled_tasks.error
    async def on_loop_error(self, exc: BaseException):
        """Last line of defence for the scheduler.

        _run_job already isolates every individual job, so reaching here means
        something outside them broke — a DB read in the fired-today guard, a
        timezone error, an event-loop fault. discord.ext.tasks calls this and
        then STOPS the loop, so without a restart the bot goes quiet forever
        while still appearing healthy in `systemctl status`. Restart on a
        short delay (not immediately: if the fault is persistent we don't want
        a tight crash-loop) and make it loud.
        """
        obs.log_event(
            logger,
            logging.CRITICAL,
            "scheduler.loop_died",
            err=type(exc).__name__,
            detail=str(exc)[:300],
            action="restarting_in_60s",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _capture(exc)
        try:
            await self._dm_owner(
                f"🔴 **Scheduler loop crashed** — restarting in 60s. All "
                f"scheduled jobs were stopped.\n"
                f"```{type(exc).__name__}: {str(exc)[:400]}```"
            )
        except Exception:
            pass
        asyncio.create_task(self._restart_loop_after(60))

    async def _restart_loop_after(self, delay_s: int) -> None:
        await asyncio.sleep(delay_s)
        try:
            self.check_scheduled_tasks.restart()
            obs.log_event(logger, logging.WARNING, "scheduler.loop_restarted")
        except Exception as e:
            obs.log_event(
                logger,
                logging.CRITICAL,
                "scheduler.restart_failed",
                err=type(e).__name__,
                detail=str(e)[:300],
                exc_info=True,
            )
            _capture(e)
