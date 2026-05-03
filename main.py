import discord
from discord.ext import commands
from discord import app_commands
import asyncpg
import os
import aiohttp
from datetime import datetime, timedelta, timezone

# --- CONFIGURATION ---
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# CRITICAL: Intents must be enabled in Discord Dev Portal
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
            # Table for tracking user progress
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
            # Table for server-specific settings
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id BIGINT PRIMARY KEY,
                    webhook_url TEXT,
                    role_2 BIGINT, role_7 BIGINT, role_14 BIGINT, 
                    role_30 BIGINT, role_50 BIGINT, role_100 BIGINT
                )
            ''')
        print("✅ Database & Tables Ready")

bot = StreakBot()

# --- HELPERS ---

async def remove_all_streak_roles(member):
    """Removes all milestone roles when a streak is broken."""
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", member.guild.id)
    
    if not row: return

    # Gather all IDs from settings
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
            print(f"🧹 Removed {len(roles_to_remove)} roles from {member.name} due to streak loss.")
        except Exception as e:
            print(f"❌ Failed to remove roles: {e}")

async def fire_streak_webhook(guild_id, user, streak_count):
    """Sends the announcement via the configured webhook."""
    async with bot.db_pool.acquire() as conn:
        url = await conn.fetchval("SELECT webhook_url FROM guild_settings WHERE guild_id = $1", guild_id)
    
    if not url: return

    content = (
        f"<:Sneeze:1495243609035899023> {user.mention}, You've acquired a Message Streak <:UhOkay:1495243635132731702>\n"
        f"**Message Streak: {streak_count}**"
    )
    
    async with aiohttp.ClientSession() as session:
        try:
            webhook = discord.Webhook.from_url(url, session=session)
            await webhook.send(content=content)
        except Exception as e:
            print(f"⚠️ Webhook error: {e}")

async def check_and_assign_roles(member, streak_count):
    """Assigns the specific role for a milestone reached."""
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
                    print(f"❌ Role Error: Check hierarchy for {member.guild.name}")

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
                       streak2: discord.Role, streak7: discord.Role, streak14: discord.Role,
                       streak30: discord.Role, streak50: discord.Role, streak100: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)

    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, role_2, role_7, role_14, role_30, role_50, role_100)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (guild_id) DO UPDATE SET 
            role_2=$2, role_7=$3, role_14=$4, role_30=$5, role_50=$6, role_100=$7
        ''', interaction.guild_id, streak2.id, streak7.id, streak14.id, streak30.id, streak50.id, streak100.id)
    await interaction.response.send_message("✅ Milestone roles updated!", ephemeral=True)

# --- THE LOGIC ---

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    today = datetime.now(timezone.utc).date()
    uid, gid = message.author.id, message.guild.id

    async with bot.db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM user_streaks WHERE user_id = $1 AND guild_id = $2", uid, gid)

        if not user:
            await conn.execute('''
                INSERT INTO user_streaks (user_id, guild_id, messages_today, last_streak_date) 
                VALUES ($1, $2, 1, $3)
            ''', uid, gid, today - timedelta(days=1))
            return

        last_date = user['last_streak_date']
        current_streak = user['current_streak']
        msgs_today = user['messages_today']

        # Prevent double counting if they already hit their streak today
        if last_date == today:
            return

        # STREAK RESET: If user missed more than 1 full day
        if last_date < today - timedelta(days=1):
            current_streak = 0
            await remove_all_streak_roles(message.author)

        new_msgs = msgs_today + 1

        if new_msgs >= 3:
            # STREAK ACHIEVED
            new_streak = current_streak + 1
            await conn.execute('''
                UPDATE user_streaks SET current_streak = $1, messages_today = 0, last_streak_date = $2 
                WHERE user_id = $3 AND guild_id = $4
            ''', new_streak, today, uid, gid)
            
            await fire_streak_webhook(gid, message.author, new_streak)
            await check_and_assign_roles(message.author, new_streak)
        else:
            # Incrementing daily count
            await conn.execute('''
                UPDATE user_streaks SET messages_today = $1, current_streak = $2
                WHERE user_id = $3 AND guild_id = $4
            ''', new_msgs, current_streak, uid, gid)

    await bot.process_commands(message)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"🚀 Logged in as {bot.user} and Slash Commands Synced.")

bot.run(TOKEN)
