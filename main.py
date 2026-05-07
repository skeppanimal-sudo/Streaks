import discord
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
import os
import json

# --- 1. GOOGLE SHEETS SETUP ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Global variables for the sheets
data_sheet = None
settings_sheet = None

def connect_to_sheets():
    global data_sheet, settings_sheet
    try:
        creds_json = os.getenv('GOOGLE_CREDS')
        if not creds_json:
            print("❌ ERROR: GOOGLE_CREDS environment variable is missing!")
            return False
        
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        gc = gspread.authorize(creds)

        # CHANGE "DiscordBotData" to your actual Google Sheet name
        spreadsheet = gc.open("DiscordBotData")
        
        data_sheet = spreadsheet.sheet1
        settings_sheet = spreadsheet.worksheet("Settings")
        print("✅ Successfully connected to Google Sheets.")
        return True
    except Exception as e:
        print(f"❌ Failed to connect to Sheets: {e}")
        return False

# Connect before the bot starts
sheets_ready = connect_to_sheets()

# --- 2. DISCORD BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix='/', intents=intents)

target_channel_id = None

@bot.event
async def on_ready():
    global target_channel_id
    print(f'🤖 Logged in as {bot.user.name}')
    
    # Try to load the saved channel ID from Settings tab Cell A1
    if sheets_ready and settings_sheet:
        try:
            val = settings_sheet.acell('A1').value
            if val:
                target_channel_id = int(val)
                print(f"📂 Loaded tracked channel ID: {target_channel_id}")
        except Exception as e:
            print(f"⚠️ Could not load saved channel: {e}")

@bot.command()
async def setchanchan(ctx):
    """Sets the current channel as the one to monitor."""
    global target_channel_id
    target_channel_id = ctx.channel.id
    
    if sheets_ready and settings_sheet:
        try:
            settings_sheet.update_acell('A1', str(target_channel_id))
            await ctx.send(f"✅ Channel {ctx.channel.mention} is now being tracked and saved to Sheets.")
        except Exception as e:
            await ctx.send(f"⚠️ Channel set in memory, but failed to save to Sheets: {e}")
    else:
        await ctx.send("❌ Error: Sheets not connected. Check bot logs.")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    # Auto-react with 👍 in the tracked channel
    if target_channel_id and message.channel.id == target_channel_id:
        await message.add_reaction("👍")
    
    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload):
    """Triggers when someone adds a reaction."""
    # Only track 👍 in the target channel, and ignore the bot's own reactions
    if str(payload.emoji) == "👍" and payload.channel_id == target_channel_id:
        if payload.user_id == bot.user.id:
            return

        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        
        reaction = discord.utils.get(message.reactions, emoji="👍")
        count = reaction.count if reaction else 0
        
        username = str(message.author)
        link = message.jump_url

        if sheets_ready and data_sheet:
            try:
                # Search for the message link in Column B
                cell = data_sheet.find(link)
                # Update count in Column C
                data_sheet.update_cell(cell.row, 3, count)
            except gspread.exceptions.CellNotFound:
                # Link not found, add new row: [User, Link, Count]
                data_sheet.append_row([username, link, count])
            except Exception as e:
                print(f"❌ Sheet update error: {e}")

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
