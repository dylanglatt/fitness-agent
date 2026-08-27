"""
Tests for the scheduler's failure boundary.

The bug these lock down: every scheduled job used to run naked inside the
1-minute `discord.ext.tasks` loop. discord.py only retries its own connection
errors, so any other exception — an Anthropic API error, a KeyError, a dead
WHOOP token — stopped the loop PERMANENTLY, silently taking the morning brief,
weekly summary, Sunday reflection, nightly sync and the health heartbeat with
it. A single library ERROR line was the only evidence.

What we assert:

  1. A job that raises does not propagate out of `check_scheduled_tasks`
     (i.e. cannot kill the loop).
  2. A failing job does not prevent the *other* jobs on the same tick from
     running — the jobs are isolated from each other, not just from the loop.
  3. A failed job is marked fired for the day, so a persistent fault does not
     re-alert every 60 seconds.
  4. A failed job logs at CRITICAL and DMs the owner.
  5. A successful job marks its own state and does not DM.
  6. A dead WHOOP token in the heartbeat is CRITICAL (the exact failure that
     went unnoticed for a month), and every problem is logged individually.

Fully offline: the bot, coach, db and Discord user are all stubs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytz

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.scheduler import Scheduler  # noqa: E402
from integrations.whoop import WhoopAuthError  # noqa: E402

TZ = "America/New_York"


def _make_scheduler() -> Scheduler:
    config = MagicMock()
    config.TIMEZONE = TZ
    config.DAILY_BRIEF_POLL_START = "05:30"
    config.DAILY_BRIEF_BACKSTOP = "11:30"

    coach = MagicMock()
    coach.db = MagicMock()
    # Nothing has fired today; record every set_sync_state call so we can
    # assert on the mark-fired behaviour.
    coach.db.get_sync_state = AsyncMock(return_value=None)
    coach.db.set_sync_state = AsyncMock(return_value=None)
    coach.db.get_latest_whoop_date = AsyncMock(return_value=None)
    coach.db.get_latest_strava_timestamp = AsyncMock(return_value=None)

    sched = Scheduler(bot=MagicMock(), config=config, coach=coach)
    sched._dm_owner = AsyncMock(return_value=True)
    return sched


def _marked_keys(sched: Scheduler) -> set[str]:
    return {
        c.kwargs.get("source")
        for c in sched.coach.db.set_sync_state.await_args_list
    }


class RunJobTests(unittest.TestCase):
    def test_failure_is_contained_and_marked(self):
        sched = _make_scheduler()

        async def boom():
            raise RuntimeError("anthropic exploded")

        with self.assertLogs("bot.scheduler", level="CRITICAL") as cap:
            ok = asyncio.run(
                sched._run_job(
                    "weekly_summary",
                    boom,
                    "2026-08-27",
                    rid_prefix="week",
                    mark_on_success="k:weekly",
                    mark_on_failure="k:weekly",
                )
            )

        self.assertFalse(ok)
        self.assertTrue(any("job.failed" in line for line in cap.output))
        # Marked fired despite failing → no 60-second retry storm.
        self.assertIn("k:weekly", _marked_keys(sched))
        sched._dm_owner.assert_awaited()

    def test_success_marks_and_stays_quiet(self):
        sched = _make_scheduler()
        ran = []

        async def fine():
            ran.append(1)

        ok = asyncio.run(
            sched._run_job(
                "nightly_sync",
                fine,
                "2026-08-27",
                rid_prefix="sync",
                mark_on_success="k:nightly",
                mark_on_failure="k:nightly",
            )
        )

        self.assertTrue(ok)
        self.assertEqual(ran, [1])
        self.assertIn("k:nightly", _marked_keys(sched))
        sched._dm_owner.assert_not_awaited()

    def test_brief_failure_does_not_mark_on_success_key_twice(self):
        """The brief marks its own state under the double-send lock, so
        _run_job must only mark it on the failure path."""
        sched = _make_scheduler()

        async def fine():
            pass

        asyncio.run(
            sched._run_job(
                "daily_brief",
                fine,
                "2026-08-27",
                rid_prefix="brief",
                mark_on_success=None,
                mark_on_failure=sched._STATE_DAILY_BRIEF,
            )
        )
        self.assertEqual(_marked_keys(sched), set())


class LoopIsolationTests(unittest.TestCase):
    """One failing job must not kill the loop or block its siblings."""

    def _tick_at(self, sched: Scheduler, local: datetime):
        # Sunday 20:45 local: weekly summary, stoic reflection and the daily
        # brief window are all eligible on the same tick.
        real_dt = datetime

        class _FrozenDatetime(real_dt):
            @classmethod
            def now(cls, tz=None):
                return pytz.timezone(TZ).localize(local)

        import bot.scheduler as mod

        mod.datetime = _FrozenDatetime
        try:
            asyncio.run(sched.check_scheduled_tasks.coro(sched))
        finally:
            mod.datetime = real_dt

    def test_one_failure_does_not_stop_the_others(self):
        sched = _make_scheduler()
        calls = []

        async def brief_ok(*a, **k):
            calls.append("brief")

        async def weekly_boom(*a, **k):
            calls.append("weekly")
            raise RuntimeError("claude 529")

        async def stoic_ok(*a, **k):
            calls.append("stoic")

        sched._maybe_fire_daily_brief = brief_ok
        sched._send_weekly_summary = weekly_boom
        sched._send_stoic_reflection = stoic_ok

        # Sunday 2026-08-30, 20:45 local.
        with self.assertLogs("bot.scheduler", level="CRITICAL"):
            self._tick_at(sched, datetime(2026, 8, 30, 20, 45))

        # The tick did not raise, and every job ran despite the middle one
        # blowing up.
        self.assertEqual(calls, ["brief", "weekly", "stoic"])


class HeartbeatTests(unittest.TestCase):
    def test_dead_whoop_token_is_critical(self):
        sched = _make_scheduler()
        sched.coach.whoop = MagicMock()
        sched.coach.whoop.get_recovery = AsyncMock(
            side_effect=WhoopAuthError("refresh token rejected (400)", 400)
        )

        # Capture at WARNING so we see both the CRITICAL token line and the
        # per-problem WARNING lines in one pass.
        with self.assertLogs("bot.scheduler", level="WARNING") as cap:
            asyncio.run(
                sched._data_health_check(
                    pytz.timezone(TZ).localize(datetime(2026, 8, 27, 12, 0))
                )
            )

        joined = "\n".join(cap.output)
        self.assertIn("whoop.token_dead", joined)
        # ...and it is CRITICAL, not merely a warning among warnings.
        token_rec = next(
            r for r in cap.records if getattr(r, "event", "") == "whoop.token_dead"
        )
        self.assertEqual(token_rec.levelno, logging.CRITICAL)
        # Each problem gets its own durable log line, not just a count.
        self.assertIn("heartbeat.problem", joined)
        sched._dm_owner.assert_awaited()

    def test_healthy_feeds_stay_silent(self):
        sched = _make_scheduler()
        sched.coach.whoop = MagicMock()
        sched.coach.whoop.get_recovery = AsyncMock(return_value=[{"score": {}}])
        sched.coach.db.get_latest_whoop_date = AsyncMock(
            return_value="2026-08-27"
        )
        sched.coach.db.get_latest_strava_timestamp = AsyncMock(
            return_value=datetime(2026, 8, 26, 12, 0, tzinfo=pytz.utc).timestamp()
        )

        with self.assertLogs("bot.scheduler", level="INFO") as cap:
            asyncio.run(
                sched._data_health_check(
                    pytz.timezone(TZ).localize(datetime(2026, 8, 27, 12, 0))
                )
            )

        self.assertIn("heartbeat.done", "\n".join(cap.output))
        sched._dm_owner.assert_not_awaited()


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
