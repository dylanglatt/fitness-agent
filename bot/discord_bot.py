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
        self._register_commands()
        self.scheduler.start()
        logger.info("Bot setup complete.")

    def _register_commands(self):
        """Register prefix commands. Kept minimal — owner-only."""

        @self.command(name="brief", help="Fire the morning brief on demand (owner only).")
        async def brief_cmd(ctx):
            if ctx.author.id != self.config.OWNER_USER_ID:
                return
            # Pre-sync today's WHOOP data so the brief sees today's row in the
            # 7-day block, just like the scheduled path does.
            async with ctx.typing():
                try:
                    await self.scheduler._refresh_recent_whoop_into_db(days=2)
                except Exception as e:
                    logger.warning(f"Pre-brief refresh failed (non-fatal): {e}")
                text = await self.coach.daily_brief()
                await ctx.send(text)

        @self.command(name="context", help="Dump the layered context the coach sees (owner only).")
        async def context_cmd(ctx):
            if ctx.author.id != self.config.OWNER_USER_ID:
                return
            async with ctx.typing():
                text = await self.coach._build_layered_context()
                # Discord messages cap at 2000 chars — chunk if needed.
                for i in range(0, len(text), 1900):
                    await ctx.send(f"```\n{text[i:i+1900]}\n```")

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
