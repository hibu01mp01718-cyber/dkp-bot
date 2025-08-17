import sys
import types
sys.modules['audioop'] = types.ModuleType('audioop')

import os
import asyncio
import logging
import random
import string
from typing import Literal
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands, Interaction
import asyncpg
from aiohttp import web
from dotenv import load_dotenv

# ---------- Config & Logging ----------
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s"
)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MOD_ROLE_NAME = os.getenv("MOD_ROLE", "Moderator")
GUILD_ID = os.getenv("GUILD_ID")
HEALTH_PORT = int(os.getenv("PORT", "8080"))  # Render sets PORT automatically

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is required")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required (e.g., from Render Postgres)")

# ---------- Helpers ----------
def gen_code(n: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))

async def ensure_user(pool: asyncpg.Pool, member: discord.abc.User | discord.Member):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (discord_id, username)
            VALUES ($1, $2)
            ON CONFLICT (discord_id)
            DO UPDATE SET username = EXCLUDED.username,
                          updated_at = now();
            """,
            int(member.id),
            f"{member.name}#{member.discriminator}" if hasattr(member, "discriminator") else member.name,
        )

async def has_mod_role(member: discord.Member) -> bool:
    # Admins always pass
    if getattr(member.guild_permissions, "administrator", False):
        return True
    return any(r.name == MOD_ROLE_NAME for r in getattr(member, "roles", []))

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

# ---------- Client (no text-prefix commands = no message_content intent needed) ----------
class DKPClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # We use slash commands only; no need for message_content intent.
        intents.guilds = True
        intents.members = True  # Needed to read roles at interaction time (enable in Discord dev portal)
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.pool: asyncpg.Pool | None = None

    async def setup_hook(self) -> None:
        # Create DB pool and migrate
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await self._create_tables()

        # Sync slash commands
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))  # Create a guild object from GUILD_ID
            await self.tree.clear_commands(guild=guild)  # Clear all guild commands
            await self.tree.sync(guild=guild)  # Sync for specific guild
            logging.info(f"Synced slash commands to guild {GUILD_ID}")
        else:
            await self.tree.clear_commands()  # Clear global commands
            await self.tree.sync()  # Sync global commands
            logging.info("Synced global slash commands (Discord may take up to ~1 hour to propagate)")

        # Start health server (so you can run as a Web Service on Render)
        asyncio.create_task(start_health_server(HEALTH_PORT))
        # Background task: auto-close expired auctions
        asyncio.create_task(auto_close_task(self))

    async def on_ready(self):
        logging.info(f"Bot is ready. Logged in as {self.user} (id={self.user.id})")

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            # enable uuid generation
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  discord_id BIGINT PRIMARY KEY,
                  username TEXT,
                  dkp INTEGER NOT NULL DEFAULT 0,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS event_types (
                  id SERIAL PRIMARY KEY,
                  name TEXT UNIQUE NOT NULL,
                  points INTEGER NOT NULL CHECK (points >= 0),
                  active BOOLEAN NOT NULL DEFAULT TRUE,
                  created_at TIMESTAMPTZ DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS pins (
                  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                  code TEXT UNIQUE NOT NULL,
                  event_type_id INTEGER REFERENCES event_types(id) ON DELETE SET NULL,
                  points INTEGER NOT NULL CHECK (points >= 0),
                  expires_at TIMESTAMPTZ NOT NULL,
                  created_by BIGINT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  active BOOLEAN NOT NULL DEFAULT TRUE
                );

                CREATE TABLE IF NOT EXISTS pin_redemptions (
                  id BIGSERIAL PRIMARY KEY,
                  pin_id UUID REFERENCES pins(id) ON DELETE CASCADE,
                  user_id BIGINT REFERENCES users(discord_id) ON DELETE CASCADE,
                  redeemed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE (pin_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS loot_auctions (
                  id BIGSERIAL PRIMARY KEY,
                  guild_id BIGINT NOT NULL,
                  channel_id BIGINT NOT NULL,
                  item_name TEXT NOT NULL,
                  min_bid INTEGER NOT NULL CHECK (min_bid >= 0),
                  increment INTEGER NOT NULL CHECK (increment >= 1),
                  style TEXT NOT NULL CHECK (style IN ('blind','fixed','zerosum')),
                  expires_at TIMESTAMPTZ,
                  created_by BIGINT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','cancelled')),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS bids (
                  id BIGSERIAL PRIMARY KEY,
                  auction_id BIGINT REFERENCES loot_auctions(id) ON DELETE CASCADE,
                  user_id BIGINT REFERENCES users(discord_id) ON DELETE CASCADE,
                  amount INTEGER NOT NULL CHECK (amount >= 0),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE (auction_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS loot_awards (
                  id BIGSERIAL PRIMARY KEY,
                  auction_id BIGINT UNIQUE REFERENCES loot_auctions(id) ON DELETE CASCADE,
                  winner_id BIGINT REFERENCES users(discord_id) ON DELETE SET NULL,
                  amount INTEGER NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

# ---------- Health server for Render ----------
async def handle_health(request):
    return web.Response(text="ok")

async def start_health_server(port: int):
    app = web.Application()
    app.add_routes([web.get('/', handle_health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Health server running on port {port}")

# ---------- auto_close_task function ----------
async def auto_close_task(cli: DKPClient):
    """Background task to auto-close expired loot auctions"""
    await cli.wait_until_ready()  # Wait until the bot is ready
    while not cli.is_closed():
        try:
            # Check for expired loot auctions and close them
            async with cli.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM loot_auctions WHERE status='open' AND expires_at IS NOT NULL AND expires_at < now()"
                )
                for auction in rows:
                    # Close the expired auction
                    msg = await resolve_auction(conn, auction)
                    guild = cli.get_guild(int(auction["guild_id"]))
                    if guild:
                        ch = guild.get_channel(int(auction["channel_id"]))
                        if isinstance(ch, (discord.TextChannel, discord.Thread)):
                            await ch.send(msg)
        except Exception as e:
            logging.exception("Auto-close task error: %s", e)
        await asyncio.sleep(20)  # Check every 20 seconds

# ---------- Common checks ----------
def mod_only():
    async def predicate(interaction: Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await safe_reply(interaction, "This command must be used in a server.", ephemeral=True)
            return False
        if not await has_mod_role(interaction.user):
            await safe_reply(interaction, f"You need the '{MOD_ROLE_NAME}' role.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

async def safe_reply(interaction: Interaction, content: str, ephemeral: bool = False):
    """Avoid 'application did not respond' by deferring when needed."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except Exception:
        # last resort
        try:
            await interaction.followup.send(content, ephemeral=ephemeral)
        except Exception:
            pass

# ---------- eventpin group ----------
eventpin_group = app_commands.Group(name="eventpin", description="Manage event PINs")

@eventpin_group.command(name="create", description="Create a new PIN for an event")
@mod_only()
async def eventpin_create(interaction: Interaction, event_name: str, duration_minutes: app_commands.Range[int, 1, 1440]):
    """Command to create an event PIN"""
    code = gen_code()  # Assuming gen_code() generates a random PIN
    await interaction.response.send_message(f"PIN created for event {event_name} with {duration_minutes} minutes duration.", ephemeral=True)

@eventpin_group.command(name="list", description="List all active event PINs")
async def eventpin_list(interaction: Interaction):
    """Command to list event pins"""
    await interaction.response.send_message("Here are the active event pins.", ephemeral=True)

# Register the eventpin group
client = DKPClient()  # Instantiate the client here
client.tree.add_command(eventpin_group)  # Register the eventpin group

# ---------- Run ----------
if __name__ == "__main__":
    client.run(DISCORD_TOKEN)  # This should be indented properly
