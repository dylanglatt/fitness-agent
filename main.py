"""
fitness-agent — entry point
Run with: python main.py
"""

import asyncio
import logging
import os

from bot.discord_bot import FitnessBot
from config import Config

# Crash reporting → Sentry (alerts land in Discord #fitness-bot).
# Errors only — no tracing, no PII. Override DSN with SENTRY_DSN.
import sentry_sdk
sentry_sdk.init(
    dsn=os.environ.get(
        "SENTRY_DSN",
        "https://8c0abd7435fe5906322bfcc32a2d1125@o4511632555048960.ingest.us.sentry.io/4511702338764800",
    ),
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    config = Config()
    bot = FitnessBot(config)
    logger.info("Starting fitness-agent...")
    await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
