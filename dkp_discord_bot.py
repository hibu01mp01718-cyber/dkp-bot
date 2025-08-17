import sys
import types
sys.modules['audioop'] = types.ModuleType('audioop')


import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import os
from dotenv import load_dotenv
import random
from datetime import datetime, timedelta

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Database pool
db_pool = None

# In-memory storage for live events and auctions
active_pins = {}  # pin: {'event_name': str, 'points': int, 'expires_at': datetime}
active_auctions = {}  # item_name: {'min_bid': int, 'increment': int, 'bids': {user_id: bid}, 'end_time': datetime}


# Utility functions
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        # Users table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                dkp INT DEFAULT 0
            )
        """)
        # Events table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events(
                id SERIAL PRIMARY KEY,
                name TEXT,
                points INT,
                pin TEXT,
                expires_at TIMESTAMP
            )
        """)
        # Loot table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS loot_history(
                id SERIAL PRIMARY KEY,
                item_name TEXT,
                winner_id BIGINT,
                bid INT,
                timestamp TIMESTAMP
            )
        """)


async def give_dkp(user: discord.User, points: int):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users(user_id, username, dkp)
            VALUES($1, $2, $3)
            ON CONFLICT(user_id)
            DO UPDATE SET dkp = users.dkp + $3
        """, user.id, str(user), points)


async def get_dkp(user: discord.User):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT dkp FROM users WHERE user_id=$1", user.id)
        return row['dkp'] if row else 0


# -----------------------------
# Slash commands
# -----------------------------

@tree.command(
    name="eventpin",
    description="Enter an event PIN to earn DKP",
    guild=discord.Object(id=GUILD_ID)
)
async def eventpin(interaction: discord.Interaction, pin: str):
    pin_data = active_pins.get(pin)
    if not pin_data:
        await interaction.response.send_message("Invalid or expired PIN.", ephemeral=True)
        return
    if datetime.utcnow() > pin_data['expires_at']:
        del active_pins[pin]
        await interaction.response.send_message("PIN has expired.", ephemeral=True)
        return

    await give_dkp(interaction.user, pin_data['points'])
    del active_pins[pin]
    await interaction.response.send_message(f"✅ {pin_data['points']} DKP added for {interaction.user.name}!")


@tree.command(
    name="dkp",
    description="Check your current DKP",
    guild=discord.Object(id=GUILD_ID)
)
async def dkp(interaction: discord.Interaction):
    points = await get_dkp(interaction.user)
    await interaction.response.send_message(f"💰 You currently have {points} DKP.")


# Moderator-only commands
def is_mod():
    async def predicate(interaction: discord.Interaction):
        mod_role = discord.utils.get(interaction.guild.roles, name="Moderator")
        return mod_role in interaction.user.roles
    return app_commands.check(predicate)


@tree.command(
    name="generatepin",
    description="Generate a PIN for an event (mod only)",
    guild=discord.Object(id=GUILD_ID)
)
@is_mod()
async def generatepin(interaction: discord.Interaction, event_name: str, points: int, duration_minutes: int):
    pin = str(random.randint(100000, 999999))
    expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
    active_pins[pin] = {'event_name': event_name, 'points': points, 'expires_at': expires_at}

    # Save to database
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO events(name, points, pin, expires_at) VALUES($1,$2,$3,$4)",
            event_name, points, pin, expires_at
        )
    await interaction.response.send_message(f"Generated PIN: `{pin}` for {event_name} ({points} DKP) valid for {duration_minutes} minutes.")


@tree.command(
    name="loot",
    description="Start a loot auction (mod only)",
    guild=discord.Object(id=GUILD_ID)
)
@is_mod()
async def loot(interaction: discord.Interaction, item_name: str, min_bid: int, increment: int, duration_minutes: int = 5):
    end_time = datetime.utcnow() + timedelta(minutes=duration_minutes)
    active_auctions[item_name] = {'min_bid': min_bid, 'increment': increment, 'bids': {}, 'end_time': end_time}
    await interaction.response.send_message(
        f"🪙 Loot auction started for **{item_name}**! Minimum bid: {min_bid} DKP. Auction ends in {duration_minutes} minutes."
    )


@tree.command(
    name="bid",
    description="Place a bid on the current loot auction",
    guild=discord.Object(id=GUILD_ID)
)
async def bid(interaction: discord.Interaction, item_name: str, amount: int):
    auction = active_auctions.get(item_name)
    if not auction:
        await interaction.response.send_message("No active auction for this item.", ephemeral=True)
        return
    user_dkp = await get_dkp(interaction.user)
    if amount < auction['min_bid']:
        await interaction.response.send_message("Bid is below minimum.", ephemeral=True)
        return
    if amount > user_dkp:
        await interaction.response.send_message("You don't have enough DKP.", ephemeral=True)
        return
    auction['bids'][interaction.user.id] = amount
    await interaction.response.send_message(f"Bid of {amount} DKP placed for {item_name}.", ephemeral=True)


@tree.command(
    name="endauction",
    description="End a loot auction immediately (mod only)",
    guild=discord.Object(id=GUILD_ID)
)
@is_mod()
async def endauction(interaction: discord.Interaction, item_name: str):
    auction = active_auctions.get(item_name)
    if not auction:
        await interaction.response.send_message("No active auction for this item.", ephemeral=True)
        return
    if not auction['bids']:
        await interaction.response.send_message("No bids were placed.", ephemeral=True)
        del active_auctions[item_name]
        return

    winner_id = max(auction['bids'], key=auction['bids'].get)
    bid_amount = auction['bids'][winner_id]
    winner = interaction.guild.get_member(winner_id)
    await give_dkp(winner, -bid_amount)  # deduct DKP

    # Record loot history
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO loot_history(item_name, winner_id, bid, timestamp) VALUES($1,$2,$3,$4)",
            item_name, winner_id, bid_amount, datetime.utcnow()
        )

    del active_auctions[item_name]
    await interaction.response.send_message(f"🏆 {winner.name} won **{item_name}** with a bid of {bid_amount} DKP!")


# -----------------------------
# Events
# -----------------------------

@bot.event
async def on_ready():
    print(f"Bot is ready. Logged in as {bot.user}")
    await init_db()
    # Sync commands to guild
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("Slash commands synced!")


bot.run(DISCORD_TOKEN)
