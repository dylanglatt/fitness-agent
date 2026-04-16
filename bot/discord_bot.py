"""
Core Discord bot setup — handles events, routes messages to the AI coach.
"""

import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime
import pytz

from config import Config
from ai.coach import Coach
from data.database import Database
from bot.scheduler import Scheduler

logger = logging.getLogger(__name__)


class FitnessBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.config = config
        self.db = Database(config.DB_PATH)
        self.coach = Coach(config, self.db)
        self.scheduler = Scheduler(self, config, self.coach)

    async def setup_hook(self):
        await self.db.initialize()
        self.scheduler.start()
        logger.info("Bot setup complete.")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="your training 💪"
        ))

    async def on_message(self, message: discord.Message):
        # Ignore messages from the bot itself
        if message.author == self.user:
            return

        # Only respond to the owner
        if message.author.id != self.config.OWNER_USER_ID:
            return

        # Process commands first (e.g., !status)
        await self.process_commands(message)

        # If not a command, treat as a conversational message to the coach
        if not message.content.startswith("!"):
            async with message.channel.typing():
                response = await self.coach.chat(message.content)
                await message.channel.send(response)
