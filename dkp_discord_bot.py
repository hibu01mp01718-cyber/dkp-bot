import discord
from discord.ext import commands
import random
import asyncio
import psycopg2
from psycopg2 import sql
import os

# Setup
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Database connection (PostgreSQL)
DATABASE_URL = os.getenv("DATABASE_URL")
conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# Helper functions
def create_pin():
    return random.randint(100000, 999999)

def check_valid_pin(pin):
    cursor.execute("SELECT * FROM pins WHERE pin = %s AND expiry > NOW()", (pin,))
    return cursor.fetchone()

def add_dkp(user_id, points):
    cursor.execute("INSERT INTO dkp (user_id, points) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET points = points + %s", (user_id, points, points))
    conn.commit()

def get_dkp(user_id):
    cursor.execute("SELECT points FROM dkp WHERE user_id = %s", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else 0

def get_leaderboard():
    cursor.execute("SELECT user_id, points FROM dkp ORDER BY points DESC LIMIT 10")
    return cursor.fetchall()

# Events & Commands

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

# Create an event and generate a pin for it
@bot.command()
@commands.has_role("Moderator")
async def create_event(ctx, event_name: str, points: int):
    pin = create_pin()
    cursor.execute("INSERT INTO events (event_name, points) VALUES (%s, %s)", (event_name, points))
    cursor.execute("INSERT INTO pins (pin, event_name, expiry) VALUES (%s, %s, NOW() + INTERVAL '1 hour')", (pin, event_name))
    conn.commit()
    await ctx.send(f'Event "{event_name}" created with {points} points. PIN: {pin}')

# Member enters PIN to gain points
@bot.command()
async def enter_pin(ctx, pin: int):
    user_id = ctx.author.id
    valid_pin = check_valid_pin(pin)
    
    if valid_pin:
        event_name = valid_pin[1]
        points = valid_pin[2]
        add_dkp(user_id, points)
        await ctx.send(f'{ctx.author.mention} successfully gained {points} DKP points for {event_name}!')
    else:
        await ctx.send(f'Invalid or expired PIN. Please try again with a valid PIN.')

# Start loot bidding
@bot.command()
@commands.has_role("Moderator")
async def start_loot(ctx, item_name: str, min_bid: int, increment: int, duration: int):
    cursor.execute("INSERT INTO loot (item_name, min_bid, increment, duration, end_time) VALUES (%s, %s, %s, %s, NOW() + INTERVAL '%s minute') RETURNING id", (item_name, min_bid, increment, duration, duration))
    loot_id = cursor.fetchone()[0]
    conn.commit()
    await ctx.send(f"Loot bidding for {item_name} has started! Minimum bid: {min_bid}, Increment: {increment}. Bidding ends in {duration} minutes.")

# Place a bid for loot
@bot.command()
async def bid(ctx, amount: int):
    user_id = ctx.author.id
    loot_id = cursor.execute("SELECT id FROM loot WHERE end_time > NOW() ORDER BY end_time ASC LIMIT 1").fetchone()
    
    if not loot_id:
        await ctx.send("No active loot bidding event found.")
        return

    current_dkp = get_dkp(user_id)
    
    if amount < current_dkp and amount >= loot_id[1]:  # Check if bid is within limits
        cursor.execute("INSERT INTO bids (user_id, loot_id, amount) VALUES (%s, %s, %s)", (user_id, loot_id[0], amount))
        conn.commit()
        await ctx.send(f'{ctx.author.mention} placed a bid of {amount} DKP for {loot_id[0]}!')
    else:
        await ctx.send(f"Invalid bid! Your current DKP: {current_dkp}, minimum bid: {loot_id[1]}.")

# Determine the winner for loot bidding
@bot.command()
@commands.has_role("Moderator")
async def close_bidding(ctx):
    cursor.execute("SELECT loot_id, max(amount) FROM bids GROUP BY loot_id ORDER BY max(amount) DESC LIMIT 1")
    highest_bid = cursor.fetchone()
    
    if highest_bid:
        cursor.execute("SELECT user_id FROM bids WHERE loot_id = %s AND amount = %s", (highest_bid[0], highest_bid[1]))
        winner_id = cursor.fetchone()[0]
        winner = await bot.fetch_user(winner_id)
        await ctx.send(f"{winner.mention} wins the loot with a bid of {highest_bid[1]} DKP!")
    else:
        await ctx.send("No bids placed yet!")

# Leaderboard command
@bot.command()
async def leaderboard(ctx):
    leaderboard = get_leaderboard()
    leaderboard_message = "Top DKP Players:\n"
    for idx, (user_id, points) in enumerate(leaderboard):
        user = await bot.fetch_user(user_id)
        leaderboard_message += f"{idx+1}. {user.name} - {points} DKP\n"
    await ctx.send(leaderboard_message)

# Run bot
bot.run('YOUR_BOT_TOKEN')
