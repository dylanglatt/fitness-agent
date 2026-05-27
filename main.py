"""
fitness-agent — entry point
Run with: python main.py
"""

import asyncio
import logging
from bot.discord_bot import FitnessBot
from config import Config

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
