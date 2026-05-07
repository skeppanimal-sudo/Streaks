import discord
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
import os
import json

# --- 1. GOOGLE SHEETS SETUP ---
# Define the scope of access
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# On Railway, you'll paste your JSON key into an environment variable named GOOGLE_CREDS
try:
    creds_json = os.getenv('GOOGLE_CREDS')
    if not creds_json:
        raise ValueError("GOOGLE_CREDS environment variable not found.")
    
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    gc = gspread.authorize(creds)

    # Replace with the EXACT name of your Google Sheet
    spreadsheet = gc.open("Your Sheet Name Here")
    data_sheet = spreadsheet.sheet1          # First tab
    settings_sheet = spreadsheet.worksheet("Settings") # Second tab
except Exception as e:
    print(f"Error connecting to Google Sheets: {e}")

# --- 2. DISCORD BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix='/', intents=intents)

# Global variable to hold the channel ID in memory
target_channel_id = None

@bot.event
async def on_ready():
    global target_channel_id
    print(f'Logged in as {bot.user.name}')
    
    # Try to load the saved channel ID from the Settings tab (Cell A1)
    try:
        val = settings_sheet.acell('A1').value
        if val:
            target_channel_id = int(val)
            print(f"Loaded tracked channel ID: {target_channel_id}")
    except Exception as e:
        print(f"No saved channel found or error loading: {e}")

@bot.command()
async def setchanchan(ctx):
    """Sets the current channel as the one to monitor."""
    global target_channel_id
    target_channel_id = ctx.channel.id
    
    # Save the ID to the Settings tab so it survives restarts
    try:
        settings_sheet.update_acell('A1', str(target_channel_id))
        await ctx.send(f"✅ Monitoring enabled for {ctx.channel.mention}. Data will be saved to Google Sheets.")
    except Exception as e:
        await ctx.send(f"⚠️ Channel set in memory, but failed to save to Sheets: {e}")

@bot.event
async def on_message(message):
    # Don't let the bot react to itself
    if message.author == bot.user:
        return
    
    # If this is the tracked channel, add the thumbs up
    if target_channel_id and message.channel.id == target_channel_id:
        await message.add_reaction("👍")
    
    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload):
    """Triggers when someone adds a reaction."""
    # Only care about 👍 in the specific channel
    if str(payload.emoji) == "👍" and payload.channel_id == target_channel_id:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        
        # Get the count of 👍 reactions
        reaction = discord.utils.get(message.reactions, emoji="👍")
        count = reaction.count if reaction else 0
        
        username = str(message.author)
        link = message.jump_url

        try:
            # Look for the message link in Column B (index 2)
            cell = data_sheet.find(link)
            # Update the count in Column C (3rd column) of that same row
            data_sheet.update_cell(cell.row, 3, count)
        except gspread.exceptions.CellNotFound:
            # If the link isn't in the sheet yet, add a new row
            # Format: [Username, Link, Count]
            data_sheet.append_row([username, link, count])
        except Exception as e:
            print(f"Error updating sheet: {e}")

# Run the bot using your Railway environment variable
bot.run(os.getenv('DISCORD_TOKEN'))
