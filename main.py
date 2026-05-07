import discord
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
import os
import json

# --- 1. GOOGLE SHEETS SETUP ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

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

        # Ensure this matches your Google Sheet name exactly
        spreadsheet = gc.open("DiscordBotData")
        
        # We use sheet1 for data and worksheet("Settings") for the ID
        data_sheet = spreadsheet.sheet1
        settings_sheet = spreadsheet.worksheet("Settings")
        print("✅ Successfully connected to Google Sheets.")
        return True
    except Exception as e:
        print(f"❌ Failed to connect to Sheets: {e}")
        return False

# Initial connection attempt
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
    
    if sheets_ready and settings_sheet:
        try:
            val = settings_sheet.acell('A1').value
            if val:
                target_channel_id = int(val)
                print(f"📂 Loaded tracked channel ID: {target_channel_id}")
        except Exception as e:
            print(f"⚠️ Could not load saved channel from Sheets: {e}")

@bot.command()
async def setchanchan(ctx):
    global target_channel_id
    target_channel_id = ctx.channel.id
    
    if sheets_ready and settings_sheet:
        try:
            settings_sheet.update_acell('A1', str(target_channel_id))
            await ctx.send(f"✅ Channel {ctx.channel.mention} is now being tracked.")
        except Exception as e:
            await ctx.send(f"⚠️ Channel set in memory, but failed to save to Sheets: {e}")
    else:
        await ctx.send("❌ Error: Sheets not connected.")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    # Auto-react only in the tracked channel
    if target_channel_id and message.channel.id == target_channel_id:
        await message.add_reaction("👍")
    
    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload):
    # Ignore the bot's own reactions
    if payload.user_id == bot.user.id:
        return

    # Check if it's the tracked channel and a 👍 emoji
    if str(payload.emoji) == "👍" and payload.channel_id == target_channel_id:
        print(f"🔍 Reaction detected on message {payload.message_id}")
        
        try:
            channel = bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            
            reaction = discord.utils.get(message.reactions, emoji="👍")
            count = reaction.count if reaction else 0
            username = str(message.author)
            link = message.jump_url

            if sheets_ready and data_sheet:
                print(f"📡 Syncing to Sheets: {username} | Count: {count}")
                
                # Try to find if this link already exists in the sheet
                cell = None
                try:
                    cell = data_sheet.find(link)
                except gspread.exceptions.CellNotFound:
                    cell = None

                if cell:
                    # Update existing row (Column 3 is Reaction Count)
                    data_sheet.update_cell(cell.row, 3, count)
                    print(f"📝 Updated existing row for {username}")
                else:
                    # Add new row: [Username, Link, Count]
                    data_sheet.append_row([username, link, count])
                    print(f"🆕 Added new row for {username}")
            else:
                print("❌ Sheets connection is not ready.")

        except Exception as e:
            print(f"❌ Error in on_raw_reaction_add: {e}")

bot.run(os.getenv('DISCORD_TOKEN'))
