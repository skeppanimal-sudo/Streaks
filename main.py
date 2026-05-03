import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg
import os
import aiohttp
from datetime import datetime, timedelta, timezone

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
            # Table for streaks
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
            # Table for settings
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id BIGINT PRIMARY KEY,
                    webhook_url TEXT,
                    role_2 BIGINT, role_7 BIGINT, role_14 BIGINT, 
                    role_30 BIGINT, role_50 BIGINT, role_100 BIGINT
                )
            ''')
        # Start the background task
        self.check_streak_expiry.start()
        print("✅ Database Ready & Expiry Task Started")

bot = StreakBot()

# --- HELPERS ---

async def remove_all_streak_roles(member):
    """Helper to wipe all streak-related roles from a member."""
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", member.guild.id)
    
    if not row:
        return

    role_keys = ['role_2', 'role_7', 'role_14', 'role_30', 'role_50', 'role_100']
    roles_to_remove = []
    
    for key in role_keys:
        r_id = row[key]
        if r_id:
            role = member.guild.get_role(r_id)
            if role and role in member.roles:
                roles_to_remove.append(role)

    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove)
            print(f"🧹 Roles cleared for {member.name}")
        except:
            print(f"❌ Failed role removal for {member.name}")

async def fire_webhook(guild_id, content):
    """Universal helper to send messages via the guild's webhook."""
    async with bot.db_pool.acquire() as conn:
        url = await conn.fetchval("SELECT webhook_url FROM guild_settings WHERE guild_id = $1", guild_id)
    
    if not url:
        return

    async with aiohttp.ClientSession() as session:
        try:
            webhook = discord.Webhook.from_url(url, session=session)
            await webhook.send(content=content)
        except Exception as e:
            print(f"⚠️ Webhook error: {e}")

async def check_and_assign_roles(member, streak_count):
    """Checks if a user hit a milestone and adds the role."""
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", member.guild.id)
    
    if not row: return

    milestones = {2: 'role_2', 7: 'role_7', 14: 'role_14', 30: 'role_30', 50: 'role_50', 100: 'role_100'}
    
    if streak_count in milestones:
        role_id = row[milestones[streak_count]]
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role)
                except:
                    print(f"❌ Hierarchy Error in {member.guild.name}")

# --- BACKGROUND TASK: AUTOMATIC EXPIRY ---

@tasks.loop(hours=1)
async def check_streak_expiry():
    """Wipes streaks and pings users the moment they miss their 24h window."""
    if not bot.db_pool:
        return
    
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    
    async with bot.db_pool.acquire() as conn:
        # Find active streaks that haven't been updated since before yesterday
        expired = await conn.fetch(
            "SELECT user_id, guild_id FROM user_streaks WHERE last_streak_date < $1 AND current_streak > 0", 
            yesterday
        )

        for record in expired:
            guild = bot.get_guild(record['guild_id'])
            if not guild: continue
            
            member = guild.get_member(record['user_id'])
            if not member: continue

            # Reset in DB
            await conn.execute(
                "UPDATE user_streaks SET current_streak = 0, messages_today = 0 WHERE user_id = $1 AND guild_id = $2",
                record['user_id'], record['guild_id']
            )

            # Strip roles and ping
            await remove_all_streak_roles(member)
            await fire_webhook(record['guild_id'], f"💔 {member.mention}, You've lost your message streak!")

# --- SLASH COMMANDS ---

@bot.tree.command(name="webhook", description="Set the streak announcement webhook URL")
async def set_webhook(interaction: discord.Interaction, url: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, webhook_url) VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET webhook_url = $2
        ''', interaction.guild_id, url)
    await interaction.response.send_message("✅ Webhook configured!", ephemeral=True)

@bot.tree.command(name="streak_roles", description="Set roles for milestones")
async def streak_roles(interaction: discord.Interaction, 
                       s2: discord.Role, s7: discord.Role, s14: discord.Role,
                       s30: discord.Role, s50: discord.Role, s100: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)

    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, role_2, role_7, role_14, role_30, role_50, role_100)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (guild_id) DO UPDATE SET 
            role_2=$2, role_7=$3, role_14=$4, role_30=$5, role_50=$6, role_100=$7
        ''', interaction.guild_id, s2.id, s7.id, s14.id, s30.id, s50.id, s100.id)
    await interaction.response.send_message("✅ Roles updated!", ephemeral=True)

# --- MAIN MESSAGE LOGIC ---

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    today = datetime.now(timezone.utc).date()
    uid, gid = message.author.id, message.guild.id

    async with bot.db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM user_streaks WHERE user_id = $1 AND guild_id = $2", uid, gid)

        # Initialize new user
        if not user:
            await conn.execute('''
                INSERT INTO user_streaks (user_id, guild_id, messages_today, last_streak_date) 
                VALUES ($1, $2, 1, $3)
            ''', uid, gid, today - timedelta(days=1))
            return

        # Stop if they already hit streak today
        if user['last_streak_date'] == today:
            return

        current_streak = user['current_streak']
        
        # Check if they missed a day (Manual check in case task hasn't run yet)
        if user['last_streak_date'] < today - timedelta(days=1):
            current_streak = 0
            await remove_all_streak_roles(message.author)
            await fire_webhook(gid, f"💔 {message.author.mention}, You've lost your message streak!")

        new_msgs = user['messages_today'] + 1

        if new_msgs >= 3:
            # SUCCESS
            new_streak = current_streak + 1
            await conn.execute('''
                UPDATE user_streaks SET current_streak = $1, messages_today = 0, last_streak_date = $2 
                WHERE user_id = $3 AND guild_id = $4
            ''', new_streak, today, uid, gid)
            
            # Fire Success Webhook
            await fire_webhook(gid, f"🔥 {message.author.mention}, You've acquired a Message Streak 🔥\n**Message Streak: {new_streak}**")
            
            # Assign roles
            await check_and_assign_roles(message.author, new_streak)
        else:
            # Increment count
            await conn.execute('''
                UPDATE user_streaks SET messages_today = $1, current_streak = $2 
                WHERE user_id = $3 AND guild_id = $4
            ''', new_msgs, current_streak, uid, gid)

    await bot.process_commands(message)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"🚀 {bot.user} is active.")

bot.run(TOKEN)
