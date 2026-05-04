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
            # User tracking table
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
            # Server settings table (Includes Nickname and Profile columns)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id BIGINT PRIMARY KEY,
                    webhook_url TEXT,
                    custom_name TEXT,
                    custom_avatar TEXT,
                    role_2 BIGINT, role_7 BIGINT, role_14 BIGINT, 
                    role_30 BIGINT, role_50 BIGINT, role_100 BIGINT
                )
            ''')
        # Start the background task (Inside the class)
        self.check_streak_expiry.start()
        print("✅ Database Ready & Expiry Task Started")

    @tasks.loop(hours=1)
    async def check_streak_expiry(self):
        """Wipes streaks and pings users the moment they miss their 24h window."""
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

                # Reset streak and LOCK date to today to prevent spam
                await conn.execute('''
                    UPDATE user_streaks SET current_streak = 0, messages_today = 0, last_streak_date = $1 
                    WHERE user_id = $2 AND guild_id = $3
                ''', today, record['user_id'], record['guild_id'])

                await remove_all_streak_roles(member)
                await fire_webhook(record['guild_id'], f"💔 {member.mention}, You've lost your message streak!")

bot = StreakBot()

# --- HELPERS ---

async def remove_all_streak_roles(member):
    """Strips all streak roles from the user (Hardcore mode)."""
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", member.guild.id)
    if not row: return

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
        except:
            print(f"❌ Hierarchy error in {member.guild.name}")

async def fire_webhook(guild_id, content):
    """Sends webhook pings using server-specific Nick and Profile image."""
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT webhook_url, custom_name, custom_avatar FROM guild_settings WHERE guild_id = $1", guild_id)
    
    if not row or not row['webhook_url']: return

    async with aiohttp.ClientSession() as session:
        try:
            webhook = discord.Webhook.from_url(row['webhook_url'], session=session)
            await webhook.send(
                content=content,
                username=row['custom_name'] if row['custom_name'] else None,
                avatar_url=row['custom_avatar'] if row['custom_avatar'] else None
            )
        except Exception as e:
            print(f"⚠️ Webhook error: {e}")

async def check_and_assign_roles(member, streak_count):
    """Grants the specific milestone role."""
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
                    pass

# --- SLASH COMMANDS ---

@bot.tree.command(name="setnick", description="Set bot's name for streak messages in this server")
async def set_nick(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    async with bot.db_pool.acquire() as conn:
        await conn.execute("INSERT INTO guild_settings (guild_id, custom_name) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET custom_name = $2", interaction.guild_id, name)
    await interaction.response.send_message(f"✅ Bot name set to: **{name}**", ephemeral=True)

@bot.tree.command(name="setprofile", description="Set bot's image URL for streak messages")
async def set_profile(interaction: discord.Interaction, image_url: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    async with bot.db_pool.acquire() as conn:
        await conn.execute("INSERT INTO guild_settings (guild_id, custom_avatar) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET custom_avatar = $2", interaction.guild_id, image_url)
    await interaction.response.send_message(f"✅ Bot profile image updated!", ephemeral=True)

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
        user = await conn.fetchrow("SELECT * FROM user_streaks WHERE user_id = $1 AND guild_id = $2", uid, gid)
        if not user:
            await conn.execute("INSERT INTO user_streaks (user_id, guild_id, messages_today, last_streak_date) VALUES ($1, $2, 1, $3)", uid, gid, today - timedelta(days=1))
            return
        if user['last_streak_date'] == today: return

        current_streak = user['current_streak']
        msgs_today = user['messages_today']

        # RESET CHECK
        if user['last_streak_date'] < today - timedelta(days=1):
            current_streak = 0
            msgs_today = 0
            # Update date to today immediately to prevent spamming loss message
            await conn.execute('''UPDATE user_streaks SET current_streak = 0, messages_today = 0, last_streak_date = $1 
                               WHERE user_id = $2 AND guild_id = $3''', today, uid, gid)
            await remove_all_streak_roles(message.author)
            await fire_webhook(gid, f"💔 {message.author.mention}, You've lost your message streak!")

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
    print(f"🚀 {bot.user} is live and fully loaded.")

bot.run(TOKEN)
