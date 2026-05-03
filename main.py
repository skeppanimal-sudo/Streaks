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

intents = discord.Intents.default()
intents.message_content = True
intents.members = True 

class StreakBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db_pool = None

    async def setup_hook(self):
        # Create a connection pool for high-traffic multi-server support
        self.db_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.db_pool.acquire() as conn:
            # Table for user streaks & message tracking
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
            # Table for Webhook and Role Config
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

# --- HELPER: WEBHOOK SENDER ---
async def fire_streak_webhook(guild_id, user, streak_count):
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow("SELECT webhook_url FROM guild_settings WHERE guild_id = $1", guild_id)
    
    if not settings or not settings['webhook_url']:
        return

    content = (
        f"<:Sneeze:1495243609035899023> {user.mention}, You've acquired a Message Streak :UhOkay:\n"
        f"**Message Streak: ({streak_count})**"
    )
    
    async with aiohttp.ClientSession() as session:
        try:
            webhook = discord.Webhook.from_url(settings['webhook_url'], session=session)
            await webhook.send(content=content, username="Streak Tracker")
        except Exception as e:
            print(f"Webhook error in {guild_id}: {e}")

# --- HELPER: ROLE GIVER ---
async def check_and_assign_roles(member, streak_count):
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", member.guild.id)
    
    if not row: return

    # Map the streak number to the database column name
    milestones = {2: 'role_2', 7: 'role_7', 14: 'role_14', 30: 'role_30', 50: 'role_50', 100: 'role_100'}
    
    if streak_count in milestones:
        role_id = row[milestones[streak_count]]
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role)
                except discord.Forbidden:
                    print(f"Missing permissions to add role in {member.guild.name}")

# --- COMMANDS ---

@bot.tree.command(name="webhook", description="Set the streak announcement webhook URL")
@app_commands.describe(url="The Discord Webhook URL")
async def set_webhook(interaction: discord.Interaction, url: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need 'Manage Server' permissions.", ephemeral=True)
    
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, webhook_url) VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET webhook_url = $2
        ''', interaction.guild_id, url)
    await interaction.response.send_message("✅ Webhook configured successfully!", ephemeral=True)

@bot.tree.command(name="streak_roles", description="Configure roles for different streak milestones")
async def streak_roles(interaction: discord.Interaction, 
                       streak2: discord.Role, streak7: discord.Role, streak14: discord.Role,
                       streak30: discord.Role, streak50: discord.Role, streak100: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ You need 'Manage Roles' permissions.", ephemeral=True)

    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, role_2, role_7, role_14, role_30, role_50, role_100)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (guild_id) DO UPDATE SET 
            role_2=$2, role_7=$3, role_14=$4, role_30=$5, role_50=$6, role_100=$7
        ''', interaction.guild_id, streak2.id, streak7.id, streak14.id, streak30.id, streak50.id, streak100.id)
    await interaction.response.send_message("✅ Streak milestones updated!", ephemeral=True)

# --- MESSAGE LISTENER ---

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    today = datetime.now(timezone.utc).date()
    uid, gid = message.author.id, message.guild.id

    async with bot.db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM user_streaks WHERE user_id = $1 AND guild_id = $2", uid, gid)

        if not user:
            # First time ever seeing this user
            await conn.execute('''
                INSERT INTO user_streaks (user_id, guild_id, messages_today, last_streak_date) 
                VALUES ($1, $2, 1, $3)
            ''', uid, gid, today - timedelta(days=1))
            return

        last_date = user['last_streak_date']
        current_streak = user['current_streak']
        msgs_today = user['messages_today']

        # 1. Handling a New Day
        if last_date < today:
            # If they missed more than 1 day, reset streak to 0
            if last_date < today - timedelta(days=1):
                current_streak = 0
            
            # Reset daily message counter
            msgs_today = 1
            # Note: We don't update last_streak_date yet because they haven't earned the streak for TODAY.
            await conn.execute('''
                UPDATE user_streaks SET messages_today = $1, current_streak = $2 
                WHERE user_id = $3 AND guild_id = $4
            ''', msgs_today, current_streak, uid, gid)

        # 2. Handling the Current Day
        elif last_date == today:
            # They already earned their streak today, do nothing.
            return

        else: # user['last_streak_date'] == today (but wait, we handled < today, so this is just logic flow)
            pass

        # Increment message count for today
        if last_date < today: # Only if they haven't completed today yet
            new_msgs = msgs_today + 1
            
            # CHECKPOINT: Did they hit exactly 3 messages?
            if new_msgs == 3:
                new_streak = current_streak + 1
                await conn.execute('''
                    UPDATE user_streaks SET current_streak = $1, messages_today = $2, last_streak_date = $3 
                    WHERE user_id = $4 AND guild_id = $5
                ''', new_streak, new_msgs, today, uid, gid)
                
                # Success actions
                await fire_streak_webhook(gid, message.author, new_streak)
                await check_and_assign_roles(message.author, new_streak)
            else:
                # Just update the message count
                await conn.execute('''
                    UPDATE user_streaks SET messages_today = $1 
                    WHERE user_id = $2 AND guild_id = $3
                ''', new_msgs, uid, gid)

    await bot.process_commands(message)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} and Slash Commands Synced.")

bot.run(TOKEN)
