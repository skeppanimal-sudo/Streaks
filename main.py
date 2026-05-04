import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg
import os
import io
import aiohttp
from datetime import datetime, timedelta, timezone
from easy_pillow import Editor, Canvas, Font, load_image

# --- CONFIGURATION ---
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
        # Initialize Database Pool
        self.db_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.db_pool.acquire() as conn:
            # Table for Streaks
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_streaks (
                    user_id BIGINT,
                    guild_id BIGINT,
                    current_streak INTEGER DEFAULT 0,
                    messages_today INTEGER DEFAULT 0,
                    last_streak_date DATE,
                    PRIMARY KEY (user_id, guild_id)
                )
            ''')
            # Table for Global Message Counting and Ranking
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS message_stats (
                    user_id BIGINT,
                    guild_id BIGINT,
                    total_messages INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, guild_id)
                )
            ''')
            # Table for Server Settings
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id BIGINT PRIMARY KEY,
                    webhook_url TEXT,
                    role_2 BIGINT, role_7 BIGINT, role_14 BIGINT, 
                    role_30 BIGINT, role_50 BIGINT, role_100 BIGINT
                )
            ''')
        self.check_streak_expiry.start()
        print("✅ Database & Tracking System Ready")

    @tasks.loop(hours=1)
    async def check_streak_expiry(self):
        if not self.db_pool: return
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        
        async with self.db_pool.acquire() as conn:
            expired = await conn.fetch(
                "SELECT user_id, guild_id FROM user_streaks WHERE last_streak_date < $1 AND current_streak > 0", 
                yesterday
            )
            for record in expired:
                guild = self.get_guild(record['guild_id'])
                if not guild: continue
                member = guild.get_member(record['user_id'])
                if not member: continue

                await conn.execute('''
                    UPDATE user_streaks SET current_streak = 0, messages_today = 0, last_streak_date = $1 
                    WHERE user_id = $2 AND guild_id = $3
                ''', today, record['user_id'], record['guild_id'])
                await remove_all_streak_roles(member)
                await fire_webhook(record['guild_id'], f"💔 {member.mention}, You've lost your message streak!")

bot = StreakBot()

# --- HELPERS ---

async def remove_all_streak_roles(member):
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", member.guild.id)
    if not row: return
    role_keys = ['role_2', 'role_7', 'role_14', 'role_30', 'role_50', 'role_100']
    roles_to_remove = [member.guild.get_role(row[k]) for k in role_keys if row[k] and member.guild.get_role(row[k]) in member.roles]
    if roles_to_remove:
        try: await member.remove_roles(*[r for r in roles_to_remove if r])
        except: pass

async def fire_webhook(guild_id, content):
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT webhook_url FROM guild_settings WHERE guild_id = $1", guild_id)
    if not row or not row['webhook_url']: return
    async with aiohttp.ClientSession() as session:
        try:
            webhook = discord.Webhook.from_url(row['webhook_url'], session=session)
            await webhook.send(content=content)
        except: pass

async def check_and_assign_roles(member, streak_count):
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", member.guild.id)
    if not row: return
    milestones = {2: 'role_2', 7: 'role_7', 14: 'role_14', 30: 'role_30', 50: 'role_50', 100: 'role_100'}
    if streak_count in milestones:
        r_id = row[milestones[streak_count]]
        if r_id:
            role = member.guild.get_role(r_id)
            if role:
                try: await member.add_roles(role)
                except: pass

# --- IMAGE GENERATION ---

async def create_rank_card(user, msg_count, rank):
    # Create Editor with background
    try:
        # Tries to load background.jpg from your folder
        background = Editor("background.jpg").resize((900, 270))
    except:
        # Fallback if image isn't found
        background = Editor(Canvas((900, 270), color="#1e1e1e"))

    # Avatar processing
    avatar_image = await load_image(str(user.display_avatar.url))
    avatar = Editor(avatar_image).resize((170, 170)).circle_image()
    
    # Dark Overlay Box (Modern Rounded Look)
    background.rectangle((25, 25), 850, 220, fill="#000000aa", radius=25)
    
    # Paste Avatar
    background.paste(avatar, (50, 50))
    
    # Text Handling
    white = "white"
    blue = "#00d4ff"
    yellow = "#ffcc4d"
    
    font_name = Font.poppins(variant="bold", size=45)
    font_label = Font.poppins(variant="light", size=28)
    font_value = Font.poppins(variant="bold", size=60)

    # Name
    background.text((250, 55), str(user.display_name), font=font_name, color=white)
    
    # Message Count Column
    background.text((250, 130), "MESSAGE COUNT", font=font_label, color="#d1d1d1")
    background.text((250, 170), str(msg_count), font=font_value, color=yellow)
    
    # Rank Column
    background.text((600, 130), "SERVER RANK", font=font_label, color="#d1d1d1")
    background.text((600, 170), f"#{rank}", font=font_value, color=blue)

    # Convert to Discord File
    file = discord.File(fp=background.image_bytes, filename="rank.png")
    return file

# --- COMMANDS ---

@bot.tree.command(name="messages", description="View message count and rank card")
async def messages(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    await interaction.response.defer() # Defer because image processing takes time

    async with bot.db_pool.acquire() as conn:
        # Get count
        row = await conn.fetchrow("SELECT total_messages FROM message_stats WHERE user_id = $1 AND guild_id = $2", user.id, interaction.guild_id)
        msg_count = row['total_messages'] if row else 0
        
        # Calculate Rank
        rank_data = await conn.fetch("SELECT user_id FROM message_stats WHERE guild_id = $1 ORDER BY total_messages DESC", interaction.guild_id)
        rank = next((i + 1 for i, r in enumerate(rank_data) if r['user_id'] == user.id), "N/A")

    card_file = await create_rank_card(user, msg_count, rank)
    await interaction.followup.send(file=card_file)

@bot.tree.command(name="webhook", description="Set the streak announcement webhook URL")
async def set_webhook(interaction: discord.Interaction, url: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    async with bot.db_pool.acquire() as conn:
        await conn.execute("INSERT INTO guild_settings (guild_id, webhook_url) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET webhook_url = $2", interaction.guild_id, url)
    await interaction.response.send_message("✅ Webhook configured!", ephemeral=True)

@bot.tree.command(name="streak_roles", description="Set roles for milestones")
async def streak_roles(interaction: discord.Interaction, s2: discord.Role, s7: discord.Role, s14: discord.Role, s30: discord.Role, s50: discord.Role, s100: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''INSERT INTO guild_settings (guild_id, role_2, role_7, role_14, role_30, role_50, role_100) VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (guild_id) DO UPDATE SET role_2=$2, role_7=$3, role_14=$4, role_30=$5, role_50=$6, role_100=$7''', 
            interaction.guild_id, s2.id, s7.id, s14.id, s30.id, s50.id, s100.id)
    await interaction.response.send_message("✅ Milestone roles updated!", ephemeral=True)

# --- CORE LOGIC ---

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    today = datetime.now(timezone.utc).date()
    uid, gid = message.author.id, message.guild.id

    async with bot.db_pool.acquire() as conn:
        # 1. TRACK GLOBAL MESSAGES (For /messages command)
        await conn.execute('''
            INSERT INTO message_stats (user_id, guild_id, total_messages) 
            VALUES ($1, $2, 1) 
            ON CONFLICT (user_id, guild_id) 
            DO UPDATE SET total_messages = message_stats.total_messages + 1
        ''', uid, gid)

        # 2. TRACK STREAK LOGIC
        user = await conn.fetchrow("SELECT * FROM user_streaks WHERE user_id = $1 AND guild_id = $2", uid, gid)
        if not user:
            await conn.execute("INSERT INTO user_streaks (user_id, guild_id, messages_today, last_streak_date) VALUES ($1, $2, 1, $3)", uid, gid, today - timedelta(days=1))
            return
        
        if user['last_streak_date'] == today: return

        current_streak = user['current_streak']
        msgs_today = user['messages_today']

        # Check for loss
        if user['last_streak_date'] < today - timedelta(days=1):
            current_streak = 0
            msgs_today = 0
            await conn.execute("UPDATE user_streaks SET current_streak = 0, messages_today = 0, last_streak_date = $1 WHERE user_id = $2 AND guild_id = $3", today, uid, gid)
            await remove_all_streak_roles(message.author)
            await fire_webhook(gid, f"💔 {message.author.mention}, You've lost your message streak!")

        # Update progress
        new_msgs = msgs_today + 1
        if new_msgs >= 3:
            new_streak = current_streak + 1
            await conn.execute("UPDATE user_streaks SET current_streak = $1, messages_today = 0, last_streak_date = $2 WHERE user_id = $3 AND guild_id = $4", new_streak, today, uid, gid)
            await fire_webhook(gid, f"🔥 {message.author.mention}, You've acquired a Message Streak 🔥\n**Message Streak: {new_streak}**")
            await check_and_assign_roles(message.author, new_streak)
        else:
            await conn.execute("UPDATE user_streaks SET messages_today = $1, current_streak = $2 WHERE user_id = $3 AND guild_id = $4", new_msgs, current_streak, uid, gid)

    await bot.process_commands(message)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"🚀 {bot.user} is tracking messages and streaks.")

bot.run(TOKEN)
