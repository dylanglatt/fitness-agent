"""
Central config — loads all secrets from environment variables.
Copy .env.example to .env and fill in your values.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Discord
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    # Optional: if set, slash commands sync to this single guild (instant).
    # If 0/unset, sync is global and can take up to an hour to propagate.
    DISCORD_GUILD_ID: int = int(os.getenv("DISCORD_GUILD_ID", "0"))

    # Strava
    STRAVA_CLIENT_ID: str = os.getenv("STRAVA_CLIENT_ID", "")
    STRAVA_CLIENT_SECRET: str = os.getenv("STRAVA_CLIENT_SECRET", "")
    STRAVA_REFRESH_TOKEN: str = os.getenv("STRAVA_REFRESH_TOKEN", "")

    # WHOOP
    WHOOP_CLIENT_ID: str = os.getenv("WHOOP_CLIENT_ID", "")
    WHOOP_CLIENT_SECRET: str = os.getenv("WHOOP_CLIENT_SECRET", "")
    WHOOP_REFRESH_TOKEN: str = os.getenv("WHOOP_REFRESH_TOKEN", "")

    # Anthropic (Claude)
    #
    # Two-model split (cost lever — see ai/coach.py):
    #   CLAUDE_MODEL  — used for the morning brief, weekly summary, Sunday
    #                   reflection, and post-workout debriefs. Quality of
    #                   prose matters here; Sonnet pays for itself.
    #   CHAT_MODEL    — used for the conversational chat path. Most chats
    #                   are short ("log this lift", "how was my HRV") and
    #                   Haiku does them just as well at ~3× lower cost.
    # COACH_CHEAP_MODE flips the chat path on/off without redeploying — when
    # set to 0 the coach uses CLAUDE_MODEL for chat too. Default is 1 (cheap).
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    CHAT_MODEL: str = os.getenv("CHAT_MODEL", "claude-haiku-4-5-20251001")
    COACH_CHEAP_MODE: bool = os.getenv("COACH_CHEAP_MODE", "1") not in ("0", "false", "False", "")

    # Notion — five-database model (per user's spreadsheet mock):
    #
    #   SCHEDULE   — day-level index (Training Group, Workout, date). One row
    #                per day; relates to the other DBs.
    #   LIFTS      — one row per exercise per workout (workout-summary level).
    #                Sets, Reps, Weight lb, RPE columns describe the
    #                prescription / aggregate; granular set data lives in
    #                LIFT_SETS (linked by a Parent Lift relation).
    #   LIFT_SETS  — one row per SET performed. Linked back to the parent
    #                Lifts row via the "Parent Lift" relation. Backs the
    #                strength-progression views; mirrors SQLite lift_sets.
    #   RUNS       — one row per cardio activity (Distance mi, Pace, Zone %).
    #                Holds rides/hikes/swims/walks too, tagged by Type.
    #   DAILY LOG  — one row per day with WHOOP physiology + morning brief text.
    #
    # Each DB id is independently optional; leave one blank/placeholder and the
    # bot skips writes to that DB (no crash). See NotionClient.is_configured_*.
    NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
    NOTION_SCHEDULE_DATABASE_ID: str = os.getenv("NOTION_SCHEDULE_DATABASE_ID", "")
    NOTION_LIFTS_DATABASE_ID: str = os.getenv("NOTION_LIFTS_DATABASE_ID", "")
    NOTION_LIFT_SETS_DATABASE_ID: str = os.getenv("NOTION_LIFT_SETS_DATABASE_ID", "")
    NOTION_RUNS_DATABASE_ID: str = os.getenv("NOTION_RUNS_DATABASE_ID", "")
    NOTION_DAILY_DATABASE_ID: str = os.getenv("NOTION_DAILY_DATABASE_ID", "")

    # Backwards-compat — earlier revisions used a single NOTION_DATABASE_ID for
    # the daily log and (briefly) a NOTION_WORKOUTS_DATABASE_ID for a unified
    # workouts table. Honor either one for the daily DB if NOTION_DAILY_DATABASE_ID
    # is unset, so upgrades don't silently break the morning brief.
    if not NOTION_DAILY_DATABASE_ID:
        NOTION_DAILY_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")

    # Bot settings
    # Daily brief is triggered by WHOOP-data-arrival inside a window, not a fixed
    # clock time — see bot/scheduler.py. These bound the window.
    DAILY_BRIEF_POLL_START: str = os.getenv("DAILY_BRIEF_POLL_START", "05:30")  # earliest fire
    DAILY_BRIEF_BACKSTOP: str = os.getenv("DAILY_BRIEF_BACKSTOP", "11:30")      # fire no matter what
    TIMEZONE: str = os.getenv("TIMEZONE", "America/New_York")
    OWNER_USER_ID: int = int(os.getenv("OWNER_USER_ID", "0"))  # Your Discord user ID

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "data/fitness_agent.db")

    # Home location (for weather forecast + air quality).
    # Set HOME_LAT, HOME_LNG, HOME_CITY in .env. If the lat/lng pair is
    # 0.0/0.0 or HOME_CITY is empty, weather + AQI are skipped from the
    # morning brief (the rest of the bot still works). See
    # integrations/weather.py for the skip logic.
    HOME_LAT: float = float(os.getenv("HOME_LAT", "0.0") or 0.0)
    HOME_LNG: float = float(os.getenv("HOME_LNG", "0.0") or 0.0)
    HOME_CITY: str = os.getenv("HOME_CITY", "")

    # ── Webhook server (Strava + WHOOP push) ────────────────────────────────
    # We co-host an aiohttp server in the same event loop as the Discord bot
    # so Strava/WHOOP can push events as they happen instead of the bot
    # polling on a 3 AM cron. Leave WEBHOOK_PORT=0 (or unset) to disable.
    #
    # The recommended deploy: Caddy terminates TLS on the public DO VPS and
    # reverse-proxies https://<your-host>/webhooks/* to 127.0.0.1:<WEBHOOK_PORT>.
    # Keep WEBHOOK_HOST=127.0.0.1 so the raw port isn't reachable from the
    # internet — Caddy (or whatever TLS front) is the only ingress.
    WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "127.0.0.1")
    WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "0") or 0)
    # The public URL Caddy maps to the local server — used by the one-time
    # scripts/strava_subscribe.py to register the callback with Strava.
    WEBHOOK_PUBLIC_URL: str = os.getenv("WEBHOOK_PUBLIC_URL", "")
    # Strava's GET-verify requires us to echo a `hub.challenge` iff the
    # `hub.verify_token` query param matches a secret we chose at subscribe
    # time. Any sufficiently-long random string works; rotate it if leaked.
    STRAVA_WEBHOOK_VERIFY_TOKEN: str = os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN", "")
