import discord
from discord.ext import commands, tasks
import asyncpg
import os
import io
import aiohttp
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

# --- CONFIG ---
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True 

class StreakBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db_pool = None

    async def setup_hook(self):
        self.db_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.db_pool.acquire() as conn:
            # Table for message streaks
            await conn.execute('''CREATE TABLE IF NOT EXISTS user_streaks (
                user_id BIGINT, guild_id BIGINT, current_streak INTEGER DEFAULT 0, 
                messages_today INTEGER DEFAULT 0, last_streak_date DATE, 
                PRIMARY KEY (user_id, guild_id))''')
            
            # Table for total message counts (Rank Card)
            await conn.execute('''CREATE TABLE IF NOT EXISTS message_stats (
                user_id BIGINT, guild_id BIGINT, total_messages INTEGER DEFAULT 0, 
                PRIMARY KEY (user_id, guild_id))''')
            
            # Table for server settings (Webhook/Roles)
            await conn.execute('''CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id BIGINT PRIMARY KEY, webhook_url TEXT, 
                role_2 BIGINT, role_7 BIGINT, role_14 BIGINT, 
                role_30 BIGINT, role_50 BIGINT, role_100 BIGINT)''')
            
        self.check_streak_expiry.start()
        print("🚀 All Systems Online")

    @tasks.loop(hours=1)
    async def check_streak_expiry(self):
        if not self.db_pool: return
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        async with self.db_pool.acquire() as conn:
            expired = await conn.fetch("SELECT user_id, guild_id FROM user_streaks WHERE last_streak_date < $1 AND current_streak > 0", yesterday)
            for record in expired:
                await conn.execute("UPDATE user_streaks SET current_streak = 0 WHERE user_id = $1 AND guild_id = $2", record['user_id'], record['guild_id'])
                await fire_webhook(record['guild_id'], f"💔 <@{record['user_id']}>, your streak has expired!")

bot = StreakBot()

# --- WEBHOOK HELPER ---
async def fire_webhook(guild_id, content):
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT webhook_url FROM guild_settings WHERE guild_id = $1", guild_id)
    if not row or not row['webhook_url']: return
    async with aiohttp.ClientSession() as session:
        try:
            webhook = discord.Webhook.from_url(row['webhook_url'], session=session)
            await webhook.send(content=content)
        except: pass

# --- IMAGE GENERATOR ---
async def create_rank_card(user, msg_count, rank):
    # Base Image
    try:
        bg = Image.open("background.jpg").convert("RGBA").resize((1000, 330))
    except:
        bg = Image.new("RGBA", (1000, 330), (135, 206, 235, 255))

    draw = ImageDraw.Draw(bg)
    
    # Rounded Dark Container (The greyish box)
    overlay = Image.new("RGBA", (940, 260), (0, 0, 0, 160)) 
    bg.paste(overlay, (30, 35), overlay)

    # Avatar
    async with aiohttp.ClientSession() as session:
        async with session.get(str(user.display_avatar.url)) as r:
            avatar_data = io.BytesIO(await r.read())
    
    avatar = Image.open(avatar_data).convert("RGBA").resize((190, 190))
    mask = Image.new("L", (190, 190), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 190, 190), fill=255)
    avatar.putalpha(mask)
    bg.paste(avatar, (65, 70), avatar)

    # Fonts
    try:
        name_font = ImageFont.truetype("font.ttf", 60)
        label_font = ImageFont.truetype("font.ttf", 40)
        value_font = ImageFont.truetype("font.ttf", 85)
    except:
        name_font = label_font = value_font = ImageFont.load_default()

    def draw_thick_text(pos, text, font, color):
        x, y = pos
        # Thick black outline
        for dx in range(-4, 5):
            for dy in range(-4, 5):
                draw.text((x+dx, y+dy), text, font=font, fill="black")
        # Main text
        draw.text(pos, text, font=font, fill=color)

    # Username
    draw_thick_text((290, 60), str(user.name), name_font, "white")
    
    # Stats
    draw_thick_text((290, 150), "MESSAGE COUNT", label_font, "white")
    draw_thick_text((290, 200), str(msg_count), value_font, "#FFCC4D")
    
    draw_thick_text((680, 150), "WEEKLY RANK", label_font, "white")
    draw_thick_text((680, 200), f"#{rank}", value_font, "#00D4FF")

    buf = io.BytesIO()
    bg.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(fp=buf, filename="rank.png")

# --- COMMANDS ---
@bot.tree.command(name="messages", description="Shows your message count and server rank")
async def messages(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    await interaction.response.defer()
    
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT total_messages FROM message_stats WHERE user_id = $1 AND guild_id = $2", user.id, interaction.guild_id)
        msg_count = row['total_messages'] if row else 0
        
        rank_data = await conn.fetch("SELECT user_id FROM message_stats WHERE guild_id = $1 ORDER BY total_messages DESC", interaction.guild_id)
        rank = next((i + 1 for i, r in enumerate(rank_data) if r['user_id'] == user.id), "?")

    file = await create_rank_card(user, msg_count, rank)
    await interaction.followup.send(file=file)

@bot.tree.command(name="webhook", description="Set the announcement channel webhook")
async def webhook(interaction: discord.Interaction, url: str):
    if not interaction.user.guild_permissions.manage_guild: return
    async with bot.db_pool.acquire() as conn:
        await conn.execute("INSERT INTO guild_settings (guild_id, webhook_url) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET webhook_url = $2", interaction.guild_id, url)
    await interaction.response.send_message("✅ Webhook active!", ephemeral=True)

# --- CORE TRACKING ---
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    uid, gid, today = message.author.id, message.guild.id, datetime.now(timezone.utc).date()
    
    async with bot.db_pool.acquire() as conn:
        # Increment message count
        await conn.execute('''INSERT INTO message_stats (user_id, guild_id, total_messages) VALUES ($1, $2, 1) 
            ON CONFLICT (user_id, guild_id) DO UPDATE SET total_messages = message_stats.total_messages + 1''', uid, gid)
        
        # Streak Logic
        user = await conn.fetchrow("SELECT * FROM user_streaks WHERE user_id = $1 AND guild_id = $2", uid, gid)
        if not user:
            await conn.execute("INSERT INTO user_streaks (user_id, guild_id, messages_today, last_streak_date) VALUES ($1, $2, 1, $3)", uid, gid, today - timedelta(days=1))
            return
        
        if user['last_streak_date'] == today: return

        # Check for streak completion (3 messages)
        new_msgs = user['messages_today'] + 1
        if new_msgs >= 3:
            new_streak = user['current_streak'] + 1
            await conn.execute("UPDATE user_streaks SET current_streak = $1, messages_today = 0, last_streak_date = $2 WHERE user_id = $3 AND guild_id = $4", new_streak, today, uid, gid)
            await fire_webhook(gid, f"🔥 {message.author.mention}, you're on a **{new_streak} day** streak!")
        else:
            await conn.execute("UPDATE user_streaks SET messages_today = $1 WHERE user_id = $2 AND guild_id = $3", new_msgs, uid, gid)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")

bot.run(TOKEN)
