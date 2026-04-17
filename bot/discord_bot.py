"""
Core Discord bot setup — handles events, routes messages to the AI coach.

Commands are registered in bot/commands.py via register_commands(). This module
stays focused on bot lifecycle (intents, setup_hook, on_ready, on_message)
so command surface can grow without turning this file into a wall.
"""

import discord
from discord.ext import commands, tasks
import logging

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

        # Register all commands (hybrid prefix+slash + slash-only groups).
        # Import is inline so commands.py can reach back into bot state without
        # a circular import at module load.
        from bot.commands import register_commands
        register_commands(self)

        # Sync the slash-command tree with Discord. Guild-scoped sync is
        # instant and ideal for dev; global sync can take up to an hour to
        # propagate. Set DISCORD_GUILD_ID in .env to dev-mode sync.
        await self._sync_slash_tree()

        self.scheduler.start()
        logger.info("Bot setup complete.")

    async def _sync_slash_tree(self):
        """Sync app_commands to Discord. Guild-scoped if DISCORD_GUILD_ID is set."""
        try:
            guild_id = getattr(self.config, "DISCORD_GUILD_ID", 0)
            if guild_id:
                guild_obj = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                logger.info(
                    f"Slash commands synced to guild {guild_id} — {len(synced)} commands."
                )
            else:
                synced = await self.tree.sync()
                logger.info(
                    f"Slash commands synced globally — {len(synced)} commands. "
                    "Global sync can take up to an hour to show up in clients."
                )
        except Exception as e:
            logger.error(f"Slash-command sync failed: {e}", exc_info=True)

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
