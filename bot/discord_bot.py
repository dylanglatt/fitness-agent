"""
Core Discord bot setup — handles events, routes messages to the AI coach.

Commands are registered in bot/commands.py via register_commands(). This module
stays focused on bot lifecycle (intents, setup_hook, on_ready, on_message)
so command surface can grow without turning this file into a wall.
"""

import discord
from discord.ext import commands
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
        # Webhook server handles: None (disabled), or (runner, site) tuple.
        # Populated in setup_hook if WEBHOOK_PORT is configured.
        self._webhook_runner = None
        self._webhook_site = None

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

        # Start the webhook receiver in the SAME event loop as the bot, so we
        # don't end up with two processes both refreshing the same OAuth
        # tokens (which would race and invalidate each other — WHOOP's
        # refresh-token rotation is particularly unforgiving there).
        try:
            from integrations.webhook_server import start_webhook_server
            # Pass the scheduler so the WHOOP recovery webhook can fire the
            # morning brief on sleep-processed, with the timed poll as fallback.
            result = await start_webhook_server(
                self.config, self.db, self.coach, self.scheduler
            )
            if result is not None:
                self._webhook_runner, self._webhook_site = result
        except Exception as e:
            logger.error(f"Webhook server failed to start: {e}", exc_info=True)

        self.scheduler.start()
        logger.info("Bot setup complete.")

    async def close(self):
        """Shut down cleanly — stop the webhook server before the Discord client."""
        try:
            if self._webhook_site is not None:
                await self._webhook_site.stop()
            if self._webhook_runner is not None:
                await self._webhook_runner.cleanup()
        except Exception as e:
            logger.warning(f"Webhook server shutdown error: {e}")
        await super().close()

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

        # If not a command, treat as a conversational message to the coach.
        # BUT: if a guided lift session is active, route the message through
        # the session handler instead — every reply during a session is a
        # set log ("155 x 6"), a control word ("skip", "done", "stop"), or
        # an unparseable input that the session handler re-prompts. This is
        # the explicit-state design that makes the bot reliable mid-workout
        # rather than guessing from inferred context.
        if not message.content.startswith("!"):
            async with message.channel.typing():
                active_session = await self.db.get_active_lift_session()
                if active_session is not None:
                    response = await self.coach.handle_session_message(message.content)
                else:
                    response = await self.coach.chat(message.content)
                for chunk in _chunk_for_discord(response):
                    await message.channel.send(chunk)


def _chunk_for_discord(text: str, limit: int = 1990) -> list[str]:
    """
    Split a long message into Discord-safe chunks (Discord caps at 2000 chars
    per message). We prefer to break on paragraph boundaries, then lines, then
    finally mid-string, so replies don't get cut mid-sentence.
    """
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Find the latest paragraph break that fits
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
