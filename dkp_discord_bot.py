import sys
import types
sys.modules['audioop'] = types.ModuleType('audioop')
import discord
from discord.ext import commands
import psycopg2
import os
from dotenv import load_dotenv
import random
import string
from datetime import datetime, timedelta

load_dotenv()

# Set up intents
intents = discord.Intents.default()
intents.messages = True  # Enables message-related events (you can customize this further as needed)

# Bot Setup with intents
bot = commands.Bot(command_prefix="/", intents=intents)

# Connect to PostgreSQL
conn = psycopg2.connect(os.getenv('DB_URL'))
cursor = conn.cursor()

# Create Tables if not exist (you can remove this if you're using an already created database)
def create_tables():
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            discord_id BIGINT UNIQUE NOT NULL,
            points INT DEFAULT 0
        );
        
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255),
            points INT
        );
        
        CREATE TABLE IF NOT EXISTS pins (
            code VARCHAR(255) PRIMARY KEY,
            event_id INT,
            expiration TIMESTAMP,
            FOREIGN KEY (event_id) REFERENCES events(id)
        );
        
        CREATE TABLE IF NOT EXISTS loot (
            id SERIAL PRIMARY KEY,
            item_name VARCHAR(255),
            min_bid INT,
            increment INT,
            winner_id BIGINT,
            duration TIMESTAMP
        );
        
        CREATE TABLE IF NOT EXISTS bids (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            loot_id INT,
            bid INT,
            FOREIGN KEY (user_id) REFERENCES users(discord_id),
            FOREIGN KEY (loot_id) REFERENCES loot(id)
        );
    """)
    conn.commit()

create_tables()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

# Command to view points of a user
@bot.command()
async def points(ctx, user: discord.User = None):
    """View points of a user"""
    if user is None:
        user = ctx.author
    cursor.execute("SELECT points FROM users WHERE discord_id = %s", (user.id,))
    result = cursor.fetchone()
    if result:
        await ctx.send(f'{user.name} has {result[0]} DKP points.')
    else:
        await ctx.send(f'{user.name} has no DKP points.')

# Command to view the leaderboard
@bot.command()
async def leaderboard(ctx):
    """Display the leaderboard"""
    cursor.execute("SELECT discord_id, points FROM users ORDER BY points DESC LIMIT 10")
    results = cursor.fetchall()
    leaderboard = "\n".join([f"{idx+1}. <@{row[0]}> - {row[1]} points" for idx, row in enumerate(results)])
    await ctx.send(f"**Leaderboard**:\n{leaderboard}")

# Command to add a new event type (with points)
@bot.command()
async def eventtype(ctx, action: str, name: str = None, points: int = None):
    """Add/edit/remove event type with points"""
    if action == "add" and name and points is not None:
        cursor.execute("INSERT INTO events (name, points) VALUES (%s, %s)", (name, points))
        conn.commit()
        await ctx.send(f"Event '{name}' added with {points} points.")
    elif action == "remove" and name:
        cursor.execute("DELETE FROM events WHERE name = %s", (name,))
        conn.commit()
        await ctx.send(f"Event '{name}' removed.")
    elif action == "list":
        cursor.execute("SELECT name, points FROM events")
        events = cursor.fetchall()
        events_list = "\n".join([f"{event[0]} - {event[1]} points" for event in events])
        await ctx.send(f"**Event Types**:\n{events_list}")

# Command to generate a random pin or manual pin
@bot.command()
async def pin(ctx, action: str, event_name: str = None, duration_minutes: int = 60, manual_code: str = None, points_override: int = None):
    """Create, list, or revoke PIN"""
    if action == "create" and event_name:
        cursor.execute("SELECT id, points FROM events WHERE name = %s", (event_name,))
        event = cursor.fetchone()
        if not event:
            await ctx.send(f"Event '{event_name}' not found.")
            return

        event_id, event_points = event
        if points_override:
            points_to_award = points_override
        else:
            points_to_award = event_points
        
        # Generate random pin or use manual code
        if manual_code:
            pin_code = manual_code
        else:
            pin_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        
        expiration_time = datetime.utcnow() + timedelta(minutes=duration_minutes)
        cursor.execute("INSERT INTO pins (code, event_id, expiration) VALUES (%s, %s, %s)", (pin_code, event_id, expiration_time))
        conn.commit()
        
        await ctx.send(f"PIN '{pin_code}' created for event '{event_name}', expires in {duration_minutes} minutes.")

    elif action == "list":
        cursor.execute("SELECT code, expiration FROM pins WHERE expiration > NOW()")
        active_pins = cursor.fetchall()
        active_pins_list = "\n".join([f"{pin[0]} - expires at {pin[1]}" for pin in active_pins])
        await ctx.send(f"**Active Pins**:\n{active_pins_list}")
        
    elif action == "revoke" and manual_code:
        cursor.execute("DELETE FROM pins WHERE code = %s", (manual_code,))
        conn.commit()
        await ctx.send(f"PIN '{manual_code}' revoked.")

# Command for redeeming DKP with PIN
@bot.command()
async def redeem(ctx, pin_code: str):
    """Redeem points with PIN"""
    cursor.execute("SELECT event_id FROM pins WHERE code = %s AND expiration > NOW()", (pin_code,))
    event = cursor.fetchone()
    if not event:
        await ctx.send("Invalid or expired PIN.")
        return

    event_id = event[0]
    cursor.execute("SELECT points FROM events WHERE id = %s", (event_id,))
    event_points = cursor.fetchone()[0]

    cursor.execute("SELECT points FROM users WHERE discord_id = %s", (ctx.author.id,))
    user_points = cursor.fetchone()
    if not user_points:
        cursor.execute("INSERT INTO users (discord_id, points) VALUES (%s, %s)", (ctx.author.id, event_points))
        conn.commit()
    else:
        new_points = user_points[0] + event_points
        cursor.execute("UPDATE users SET points = %s WHERE discord_id = %s", (new_points, ctx.author.id))
        conn.commit()

    await ctx.send(f"{ctx.author.name} redeemed {event_points} DKP points with PIN '{pin_code}'.")

# Command to place a bid for loot
@bot.command()
async def loot(ctx, action: str, item_name: str = None, min_bid: int = None, increment: int = None, duration_minutes: int = 60):
    """Manage loot events"""
    if action == "start" and item_name and min_bid is not None and increment is not None:
        cursor.execute("INSERT INTO loot (item_name, min_bid, increment, duration) VALUES (%s, %s, %s, NOW() + INTERVAL '%s minutes')", (item_name, min_bid, increment, duration_minutes))
        conn.commit()
        await ctx.send(f"Loot bidding for '{item_name}' started with a minimum bid of {min_bid} DKP and increment of {increment} DKP.")

    elif action == "status":
        cursor.execute("SELECT item_name, min_bid, increment, winner_id FROM loot WHERE duration > NOW() ORDER BY duration LIMIT 1")
        loot_event = cursor.fetchone()
        if loot_event:
            item_name, min_bid, increment, winner_id = loot_event
            await ctx.send(f"Current loot event: {item_name}, Minimum bid: {min_bid}, Increment: {increment}, Winner: <@{winner_id}>")
        else:
            await ctx.send("No active loot events.")

    elif action == "close":
        cursor.execute("SELECT id, item_name FROM loot WHERE duration > NOW() ORDER BY duration LIMIT 1")
        loot_event = cursor.fetchone()
        if loot_event:
            loot_id, item_name = loot_event
            cursor.execute("SELECT user_id, bid FROM bids WHERE loot_id = %s ORDER BY bid DESC LIMIT 1", (loot_id,))
            highest_bid = cursor.fetchone()
            if highest_bid:
                user_id, bid = highest_bid
                cursor.execute("UPDATE loot SET winner_id = %s WHERE id = %s", (user_id, loot_id))
                cursor.execute("UPDATE users SET points = points - %s WHERE discord_id = %s", (bid, user_id))
                conn.commit()
                await ctx.send(f"The loot '{item_name}' was won by <@{user_id}> with a bid of {bid} DKP.")
            else:
                await ctx.send("No bids placed.")
        else:
            await ctx.send("No active loot events to close.")

# Command to place a bid
@bot.command()
async def bid(ctx, amount: int):
    """Place a bid for the loot"""
    cursor.execute("SELECT id, item_name, min_bid, increment FROM loot WHERE duration > NOW() ORDER BY duration LIMIT 1")
    loot_event = cursor.fetchone()
    if not loot_event:
        await ctx.send("No active loot event.")
        return
    
    loot_id, item_name, min_bid, increment = loot_event
    if amount < min_bid:
        await ctx.send(f"The bid must be at least {min_bid} DKP.")
        return
    
    cursor.execute("SELECT points FROM users WHERE discord_id = %s", (ctx.author.id,))
    user_points = cursor.fetchone()[0]
    
    if amount > user_points:
        await ctx.send("You don't have enough DKP points to place this bid.")
        return
    
    cursor.execute("INSERT INTO bids (user_id, loot_id, bid) VALUES (%s, %s, %s)", (ctx.author.id, loot_id, amount))
    conn.commit()
    await ctx.send(f"<@{ctx.author.id}> placed a bid of {amount} DKP for '{item_name}'.")

# Run the bot
bot.run(os.getenv("DISCORD_TOKEN"))
