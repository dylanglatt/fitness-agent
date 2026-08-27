"""
fitness-agent — entry point
Run with: python main.py
"""

import asyncio

# obs must be configured before anything else logs or raises. It imports no
# project code, so this is safe as the first import.
import obs

logger = obs.setup_logging("fitness-bot")
obs.init_sentry("fitness-bot")

from bot.discord_bot import FitnessBot  # noqa: E402  (after logging setup, on purpose)
from config import Config  # noqa: E402


async def main():
    config = Config()
    bot = FitnessBot(config)
    logger.info("Starting fitness-agent...")
    await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
