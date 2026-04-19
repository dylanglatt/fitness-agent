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
    DISCORD_CHANNEL_DAILY: int = int(os.getenv("DISCORD_CHANNEL_DAILY", "0"))
    DISCORD_CHANNEL_TRAINING: int = int(os.getenv("DISCORD_CHANNEL_TRAINING", "0"))
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
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    # Notion
    NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
    NOTION_DATABASE_ID: str = os.getenv("NOTION_DATABASE_ID", "")

    # Bot settings
    # Daily brief is triggered by WHOOP-data-arrival inside a window, not a fixed
    # clock time — see bot/scheduler.py. These bound the window.
    DAILY_BRIEF_POLL_START: str = os.getenv("DAILY_BRIEF_POLL_START", "05:30")  # earliest fire
    DAILY_BRIEF_BACKSTOP: str = os.getenv("DAILY_BRIEF_BACKSTOP", "10:00")      # fire no matter what
    # Kept for backwards compatibility / display; no longer the trigger.
    DAILY_BRIEF_TIME: str = os.getenv("DAILY_BRIEF_TIME", "07:30")
    TIMEZONE: str = os.getenv("TIMEZONE", "America/New_York")
    OWNER_USER_ID: int = int(os.getenv("OWNER_USER_ID", "0"))  # Your Discord user ID

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "data/fitness_bot.db")

    # Home location (for weather forecast + air quality). Default is
    # East Village, Manhattan (10009); override per user.
    HOME_LAT: float = float(os.getenv("HOME_LAT", "0.0"))
    HOME_LNG: float = float(os.getenv("HOME_LNG", "0.0"))
    HOME_CITY: str = os.getenv("HOME_CITY", "New York, NY 10009")

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
