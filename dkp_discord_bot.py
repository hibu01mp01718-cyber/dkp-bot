"""
DKP Discord Bot – Render-ready (Postgres via asyncpg)
-----------------------------------------------------

Features
- Event Types CRUD (mods manage names & points)
- PIN generation (auto/manual) with expiry & one-redeem-per-user
- PIN redemption awards DKP and logs history
- Blind loot auctions (+ fixed-cost, zero-sum styles), min bid & increments
- Winner DKP deduction (and zero-sum redistribution)
- Leaderboard, points, recent loot history
- Moderator role gate (env MOD_ROLE, default "Moderator")
- Postgres auto-migrations on startup
- Tiny HTTP health server for Render (so you can run as a Web Service)

Env (.env locally; Environment Variables on Render)
- DISCORD_TOKEN=...
- DATABASE_URL=postgres://USER:PASS@HOST:5432/DBNAME?sslmode=require
- MOD_ROLE=Moderator
- GUILD_ID=<optional single guild id for fast slash sync>
- PORT=<Render provides automatically for Web Services, default 8080>

Requirements (see requirements.txt)
- discord.py >= 2.3
- asyncpg
- python-dotenv
- aiohttp
"""

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

        # Sync slash commands (fast if GUILD_ID set)
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

client = DKPClient()

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

# ---------- Event Types ----------
eventtype = app_commands.Group(name="eventtype", description="Manage event types")

@eventtype.command(name="add", description="Add an event type")
@mod_only()
async def eventtype_add(interaction: Interaction, name: str, points: app_commands.Range[int, 0, 100000]):
    await interaction.response.defer(ephemeral=True)
    async with client.pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO event_types (name, points) VALUES ($1, $2)",
                name.strip(),
                int(points)
            )
        except asyncpg.UniqueViolationError:
            await interaction.followup.send("That event type already exists.", ephemeral=True)
            return
    await interaction.followup.send(f"Added event type **{name}** = **{points} DKP**.", ephemeral=True)

@eventtype.command(name="edit", description="Edit an event type's points")
@mod_only()
async def eventtype_edit(interaction: Interaction, name: str, points: app_commands.Range[int, 0, 100000]):
    await interaction.response.defer(ephemeral=True)
    async with client.pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE event_types SET points=$1 WHERE name=LOWER($2) OR name=$2",
            int(points), name
        )
    if res.endswith("0"):
        await interaction.followup.send("Event type not found.", ephemeral=True)
    else:
        await interaction.followup.send(f"Updated **{name}** to **{points} DKP**.", ephemeral=True)

@eventtype.command(name="remove", description="Remove an event type")
@mod_only()
async def eventtype_remove(interaction: Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    async with client.pool.acquire() as conn:
        res = await conn.execute("DELETE FROM event_types WHERE name=LOWER($1) OR name=$1", name)
    if res.endswith("0"):
        await interaction.followup.send("Event type not found.", ephemeral=True)
    else:
        await interaction.followup.send(f"Removed event type **{name}**.", ephemeral=True)

@eventtype.command(name="list", description="List event types")
async def eventtype_list(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)
    async with client.pool.acquire() as conn:
        rows = await conn.fetch("SELECT name, points, active FROM event_types ORDER BY name")
    if not rows:
        await interaction.followup.send("No event types yet.", ephemeral=True)
        return
    lines = [f"• {r['name']} — {r['points']} DKP" + (" (inactive)" if not r['active'] else "") for r in rows]
    await interaction.followup.send("**Event Types**\n" + "\n".join(lines), ephemeral=True)

client.tree.add_command(eventtype)

# ---------- PINs ----------
pin = app_commands.Group(name="pin", description="Generate and manage event PINs")

@pin.command(name="create", description="Create a PIN for an event (auto or manual)")
@mod_only()
async def pin_create(
    interaction: Interaction,
    event_name: str,
    duration_minutes: app_commands.Range[int, 1, 1440],
    manual_code: str | None = None,
    points_override: app_commands.Range[int, 0, 100000] | None = None,
):
    await interaction.response.defer(ephemeral=True)
    async with client.pool.acquire() as conn:
        et = await conn.fetchrow(
            "SELECT id, points FROM event_types WHERE name=LOWER($1) OR name=$1",
            event_name
        )
        if not et:
            await interaction.followup.send("Event type not found. Create it with `/eventtype add`.", ephemeral=True)
            return
        code = manual_code.strip().upper() if manual_code else gen_code()
        expires = utcnow() + timedelta(minutes=int(duration_minutes))
        pts = int(points_override) if points_override is not None else int(et["points"])
        try:
            await conn.execute(
                """
                INSERT INTO pins (code, event_type_id, points, expires_at, created_by, active)
                VALUES ($1, $2, $3, $4, $5, TRUE)
                """,
                code, et["id"], pts, expires, int(interaction.user.id)
            )
        except asyncpg.UniqueViolationError:
            await interaction.followup.send("A PIN with that code already exists. Try another.", ephemeral=True)
            return

    await interaction.followup.send(
        f"**PIN:** `{code}`\nEvent: **{event_name}** (+{pts} DKP)\n"
        f"Expires: {expires.strftime('%Y-%m-%d %H:%M UTC')}\n*Share this code in-game.*",
        ephemeral=True,
    )

@pin.command(name="list", description="List active PINs")
@mod_only()
async def pin_list(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)
    async with client.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT code, points, expires_at, active FROM pins WHERE active=TRUE ORDER BY expires_at ASC"
        )
    if not rows:
        await interaction.followup.send("No active PINs.", ephemeral=True)
        return
    lines = [f"`{r['code']}` — +{r['points']} DKP, expires {r['expires_at'].strftime('%Y-%m-%d %H:%M UTC')}" for r in rows]
    await interaction.followup.send("**Active PINs**\n" + "\n".join(lines), ephemeral=True)

@pin.command(name="revoke", description="Deactivate a PIN")
@mod_only()
async def pin_revoke(interaction: Interaction, code: str):
    await interaction.response.defer(ephemeral=True)
    async with client.pool.acquire() as conn:
        res = await conn.execute("UPDATE pins SET active=FALSE WHERE code=UPPER($1)", code)
    if res.endswith("0"):
        await interaction.followup.send("PIN not found.", ephemeral=True)
    else:
        await interaction.followup.send(f"PIN `{code.upper()}` revoked.", ephemeral=True)

client.tree.add_command(pin)

# ---------- Redeem PIN ----------
@client.tree.command(name="redeem", description="Redeem a PIN for DKP")
@app_commands.describe(code="The PIN code from your mod")
async def redeem(interaction: Interaction, code: str):
    await interaction.response.defer(ephemeral=True)
    code = code.strip().upper()

    async with client.pool.acquire() as conn:
        await ensure_user(client.pool, interaction.user)
        pin_row = await conn.fetchrow(
            "SELECT id, points, expires_at, active FROM pins WHERE code=$1",
            code
        )
        if not pin_row or not pin_row["active"]:
            await interaction.followup.send("Invalid or revoked PIN.", ephemeral=True)
            return
        if pin_row["expires_at"] < utcnow():
            await interaction.followup.send("This PIN has expired.", ephemeral=True)
            return

        try:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT 1 FROM pin_redemptions WHERE pin_id=$1 AND user_id=$2",
                    pin_row["id"], int(interaction.user.id)
                )
                if existing:
                    await interaction.followup.send("You already redeemed this PIN.", ephemeral=True)
                    return

                # award DKP
                await conn.execute(
                    "UPDATE users SET dkp = dkp + $1, updated_at = now() WHERE discord_id=$2",
                    int(pin_row["points"]), int(interaction.user.id)
                )
                await conn.execute(
                    "INSERT INTO pin_redemptions (pin_id, user_id) VALUES ($1, $2)",
                    pin_row["id"], int(interaction.user.id)
                )
        except Exception as e:
            logging.exception("Redeem failed: %s", e)
            await interaction.followup.send("Something went wrong. Try again.", ephemeral=True)
            return

    await interaction.followup.send(f"Redeemed `{code}` for **+{pin_row['points']} DKP**!", ephemeral=True)

# ---------- Loot Auctions ----------
loot = app_commands.Group(name="loot", description="Loot auctions & bidding")

@loot.command(name="start", description="Start a loot auction")
@mod_only()
async def loot_start(
    interaction: Interaction,
    item_name: str,
    min_bid: app_commands.Range[int, 0, 100000],
    increment: app_commands.Range[int, 1, 100000],
    style: Literal["blind", "fixed", "zerosum"] = "blind",
    duration_minutes: app_commands.Range[int, 0, 1440] = 0,
):
    await interaction.response.defer()
    guild_id = int(interaction.guild_id)
    channel_id = int(interaction.channel_id)
    expires = None
    if duration_minutes and duration_minutes > 0:
        expires = utcnow() + timedelta(minutes=int(duration_minutes))

    async with client.pool.acquire() as conn:
        rec = await conn.fetchrow(
            """
            INSERT INTO loot_auctions
              (guild_id, channel_id, item_name, min_bid, increment, style, expires_at, created_by)
            VALUES
              ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id
            """,
            guild_id, channel_id, item_name, int(min_bid), int(increment), style, expires, int(interaction.user.id)
        )
    await interaction.followup.send(
        f"**Loot:** {item_name}\nStyle: `{style}` • Min: {min_bid} • Increment: {increment}"
        + (f" • Closes in {duration_minutes} min" if expires else " • (manual close)")
        + f"\nAuction ID: #{rec['id']}"
    )

@loot.command(name="status", description="Show current auction status in this channel")
async def loot_status(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)
    async with client.pool.acquire() as conn:
        a = await conn.fetchrow(
            """
            SELECT * FROM loot_auctions
            WHERE channel_id=$1 AND status='open'
            ORDER BY created_at DESC LIMIT 1
            """,
            int(interaction.channel_id),
        )
        if not a:
            await interaction.followup.send("No open auction here.", ephemeral=True)
            return
        bid_count = await conn.fetchval("SELECT COUNT(*) FROM bids WHERE auction_id=$1", int(a["id"]))
        await interaction.followup.send(
            f"Auction #{a['id']}: **{a['item_name']}** • style `{a['style']}` • "
            f"min {a['min_bid']} • inc {a['increment']} • bids: {bid_count}",
            ephemeral=True
        )

@loot.command(name="cancel", description="Cancel the open auction in this channel")
@mod_only()
async def loot_cancel(interaction: Interaction):
    await interaction.response.defer()
    async with client.pool.acquire() as conn:
        a = await conn.fetchrow(
            "SELECT * FROM loot_auctions WHERE channel_id=$1 AND status='open' ORDER BY created_at DESC LIMIT 1",
            int(interaction.channel_id),
        )
        if not a:
            await interaction.followup.send("No open auction to cancel.")
            return
        await conn.execute("UPDATE loot_auctions SET status='cancelled' WHERE id=$1", int(a["id"]))
    await interaction.followup.send(f"Auction #{a['id']} cancelled.")

@loot.command(name="close", description="Close and resolve the open auction in this channel")
@mod_only()
async def loot_close(interaction: Interaction):
    await interaction.response.defer()
    async with client.pool.acquire() as conn:
        a = await conn.fetchrow(
            "SELECT * FROM loot_auctions WHERE channel_id=$1 AND status='open' ORDER BY created_at DESC LIMIT 1",
            int(interaction.channel_id),
        )
        if not a:
            await interaction.followup.send("No open auction to close.")
            return
        msg = await resolve_auction(conn, a)
    await interaction.followup.send(msg)

client.tree.add_command(loot)

# ---------- /bid (ephemeral) ----------
@client.tree.command(name="bid", description="Place a bid on the current auction")
async def bid(interaction: Interaction, amount: app_commands.Range[int, 0, 100000]):
    await interaction.response.defer(ephemeral=True)
    async with client.pool.acquire() as conn:
        await ensure_user(client.pool, interaction.user)
        a = await conn.fetchrow(
            "SELECT * FROM loot_auctions WHERE channel_id=$1 AND status='open' ORDER BY created_at DESC LIMIT 1",
            int(interaction.channel_id),
        )
        if not a:
            await interaction.followup.send("No open auction in this channel.", ephemeral=True)
            return

        style = a["style"]
        if style == "fixed":
            # first valid claim at cost = min_bid; ignore amount
            cost = int(a["min_bid"])
            bal = await conn.fetchval("SELECT dkp FROM users WHERE discord_id=$1", int(interaction.user.id))
            if bal < cost:
                await interaction.followup.send(f"Insufficient DKP (need {cost}).", ephemeral=True)
                return

            existing_bids = await conn.fetchval("SELECT COUNT(*) FROM bids WHERE auction_id=$1", int(a["id"]))
            if existing_bids > 0:
                await interaction.followup.send("Too late—item already claimed.", ephemeral=True)
                return

            await conn.execute(
                "INSERT INTO bids (auction_id, user_id, amount) VALUES ($1,$2,$3)",
                int(a["id"]), int(interaction.user.id), cost
            )
            # close & award immediately
            msg = await resolve_auction(conn, a)
            await interaction.followup.send("Claimed!", ephemeral=True)
            # announce publicly
            ch = interaction.channel
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                await ch.send(msg)
            return

        # blind / zerosum
        min_bid = int(a["min_bid"])
        inc = int(a["increment"])
        if amount < min_bid or (amount - min_bid) % inc != 0:
            await interaction.followup.send(
                f"Bid must be ≥ {min_bid} and increase by steps of {inc}.",
                ephemeral=True
            )
            return

        bal = await conn.fetchval("SELECT dkp FROM users WHERE discord_id=$1", int(interaction.user.id))
        if bal < int(amount):
            await interaction.followup.send("Insufficient DKP for that bid.", ephemeral=True)
            return

        await conn.execute(
            """
            INSERT INTO bids (auction_id, user_id, amount)
            VALUES ($1,$2,$3)
            ON CONFLICT (auction_id, user_id)
            DO UPDATE SET amount = EXCLUDED.amount, created_at = now();
            """,
            int(a["id"]), int(interaction.user.id), int(amount)
        )
        await interaction.followup.send("Your bid is in. (Blind)", ephemeral=True)

# ---------- Auction Resolution & Auto-close ----------
async def resolve_auction(conn: asyncpg.Connection, a: asyncpg.Record) -> str:
    if a["status"] != "open":
        return f"Auction #{a['id']} already {a['status']}."

    style = a["style"]
    bids = await conn.fetch(
        "SELECT * FROM bids WHERE auction_id=$1 ORDER BY amount DESC, created_at ASC",
        int(a["id"])
    )

    if style == "fixed":
        if not bids:
            await conn.execute("UPDATE loot_auctions SET status='closed' WHERE id=$1", int(a["id"]))
            return f"Auction #{a['id']} for **{a['item_name']}** closed: no takers."
        win = bids[0]
        amount = int(a["min_bid"])
    else:
        if not bids:
            await conn.execute("UPDATE loot_auctions SET status='closed' WHERE id=$1", int(a["id"]))
            return f"Auction #{a['id']} for **{a['item_name']}** closed: no bids."
        win = bids[0]
        amount = int(win["amount"])

    winner_id = int(win["user_id"])

    # Deduct winner and (if zerosum) redistribute
    async with conn.transaction():
        # lock winner
        bal = await conn.fetchval(
            "SELECT dkp FROM users WHERE discord_id=$1 FOR UPDATE",
            winner_id
        )
        if bal < amount:
            # Remove their bid and retry resolution to next highest
            await conn.execute("DELETE FROM bids WHERE id=$1", int(win["id"]))
            refreshed = await conn.fetchrow("SELECT * FROM loot_auctions WHERE id=$1", int(a["id"]))
            return await resolve_auction(conn, refreshed)

        await conn.execute(
            "UPDATE users SET dkp = dkp - $1, updated_at = now() WHERE discord_id=$2",
            amount, winner_id
        )
        await conn.execute(
            "UPDATE loot_auctions SET status='closed' WHERE id=$1",
            int(a["id"])
        )
        await conn.execute(
            """
            INSERT INTO loot_awards (auction_id, winner_id, amount)
            VALUES ($1,$2,$3)
            ON CONFLICT (auction_id) DO NOTHING
            """,
            int(a["id"]), winner_id, amount
        )

        if style == "zerosum":
            losers = [b for b in bids if int(b["user_id"]) != winner_id]
            if losers:
                share = amount // len(losers)
                if share > 0:
                    for b in losers:
                        await conn.execute(
                            "UPDATE users SET dkp = dkp + $1, updated_at = now() WHERE discord_id=$2",
                            share, int(b["user_id"])
                        )

    winner_name = await conn.fetchval("SELECT username FROM users WHERE discord_id=$1", winner_id)
    summary = f"**{a['item_name']}** → **{winner_name}** ({amount} DKP) — style `{style}`"
    if style == "zerosum":
        losers = [b for b in bids if int(b["user_id"]) != winner_id]
        if losers:
            summary += f" • redistributed {amount // len(losers)} DKP to {len(losers)} bidders"
    return summary

async def auto_close_task(cli: DKPClient):
    await cli.wait_until_ready()
    while not cli.is_closed():
        try:
            async with cli.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM loot_auctions WHERE status='open' AND expires_at IS NOT NULL AND expires_at < now()"
                )
                for a in rows:
                    msg = await resolve_auction(conn, a)
                    guild = cli.get_guild(int(a["guild_id"]))
                    if guild:
                        ch = guild.get_channel(int(a["channel_id"]))
                        if isinstance(ch, (discord.TextChannel, discord.Thread)):
                            await ch.send(msg)
        except Exception as e:
            logging.exception("Auto-close task error: %s", e)
        await asyncio.sleep(20)

# ---------- Run ----------
if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
