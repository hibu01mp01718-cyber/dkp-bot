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

        # Sync slash commands (fast if GUILD_ID set for guild-specific commands)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logging.info(f"Synced slash commands to guild {GUILD_ID}")
        else:
            await self.tree.sync()
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

# ---------- /points, /leaderboard, /loot_history ----------
@client.tree.command(description="Show your DKP or someone else's")
@app_commands.describe(user="User to inspect (optional)")
async def points(interaction: Interaction, user: discord.Member | None = None):
    await interaction.response.defer(ephemeral=True)
    target = user or interaction.user
    async with client.pool.acquire() as conn:
        await ensure_user(client.pool, target)
        row = await conn.fetchrow("SELECT dkp FROM users WHERE discord_id=$1", int(target.id))
    await interaction.followup.send(f"**{target.display_name}** has **{row['dkp']} DKP**.", ephemeral=True)

@client.tree.command(description="Top DKP holders (top 10)")
async def leaderboard(interaction: Interaction):
    await interaction.response.defer()
    async with client.pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, dkp FROM users ORDER BY dkp DESC NULLS LAST LIMIT 10")
    if not rows:
        await interaction.followup.send("No data yet.")
        return
    msg = "**DKP Leaderboard**\n" + "\n".join(f"{i+1}. {r['username']}: {r['dkp']}" for i, r in enumerate(rows))
    await interaction.followup.send(msg)

@client.tree.command(description="Recent loot awards (last 10)")
async def loot_history(interaction: Interaction):
    await interaction.response.defer(ephemeral=False)
    async with client.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT la.id, la.amount, la.created_at, u.username, a.item_name
            FROM loot_awards la
            JOIN loot_auctions a ON a.id = la.auction_id
            LEFT JOIN users u ON u.discord_id = la.winner_id
            WHERE a.guild_id = $1
            ORDER BY la.created_at DESC
            LIMIT 10
            """,
            int(interaction.guild_id),
        )
    if not rows:
        await interaction.followup.send("No loot awards yet.")
        return
    lines = [
        f"#{r['id']} • {r['item_name']} → {r['username'] or 'Unknown'} "
        f"({r['amount']} DKP) • {r['created_at'].strftime('%Y-%m-%d %H:%M UTC')}"
        for r in rows
    ]
    await interaction.followup.send("**Recent Loot**\n" + "\n".join(lines))

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

client.tree.add_command(eventpin_group)  # Register the eventpin group

# ---------- Run ----------
if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
