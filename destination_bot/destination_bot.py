import discord
import os
import subprocess
import time
import logging
import psycopg2
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

try:
    intents = discord.Intents.default()  # ✅ Correct method for discord.py
    intents.messages = True  # Ensure the bot can read messages
    intents.guilds = True  # Ensure the bot can interact with servers
except AttributeError as e:
    print(f"❌ Error initializing intents: {e}")
    exit(1)  # Exit the script if intents are not available

# Load environment variables
load_dotenv()

# Create logs directory if it doesn't exist
if not os.path.exists("logs"):
    os.makedirs("logs")

# Setup logging
logging.basicConfig(
    filename="logs/destination_bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log_message(message):
    """Logs a message to both the console and log file."""
    print(message)
    logging.info(message)

DESTINATION_BOT_TOKEN = os.getenv("DESTINATION_BOT_TOKEN")
DESTINATION_SERVER_ID = int(os.getenv("DESTINATION_SERVER_ID"))

# PostgreSQL Configuration
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")

# Connect to PostgreSQL
def connect_db():
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT
        )
        return conn
    except Exception as e:
        print(f"Database connection error: {e}")
        return None

# Define bot with command tree (for slash commands)
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True  # ✅ Ensure bot receives messages

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Destination bot logged in as {bot.user}")
    await bot.tree.sync()
    print("✅ Slash commands synced.")

@bot.tree.command(name="exclude_category", description="Exclude a category from syncing",
                  guild=discord.Object(id=DESTINATION_SERVER_ID))
async def exclude_category(interaction: discord.Interaction, category_id: str):
    """Adds a category ID to the exclusion list so it won't be synced."""
    await interaction.response.defer(thinking=True)

    conn = connect_db()
    if not conn:
        await interaction.followup.send("Database connection failed.")
        return

    cursor = conn.cursor()
    try:
        # Check if category exists in the database
        cursor.execute("SELECT category_name FROM categories WHERE category_id = %s;", (category_id,))
        category = cursor.fetchone()

        if not category:
            await interaction.followup.send(f"Category with ID `{category_id}` not found in the database.")
            return

        # Insert into excluded_categories table
        cursor.execute("INSERT INTO excluded_categories (category_id) VALUES (%s) ON CONFLICT DO NOTHING;", (category_id,))
        conn.commit()

        await interaction.followup.send(f"Excluded category `{category[0]}` (ID: `{category_id}`) from syncing.")
        print(f"⏩ Category {category[0]} ({category_id}) is now excluded.")

    except Exception as e:
        await interaction.followup.send(f"Error excluding category: {e}")
    finally:
        cursor.close()
        conn.close()

@bot.tree.command(name="force_sync", description="Force a full sync of categories & channels",
                  guild=discord.Object(id=DESTINATION_SERVER_ID))
async def force_sync(interaction: discord.Interaction):
    """Manually trigger a full sync of categories and channels."""
    await sync_categories(interaction)  # Call the sync function
    await interaction.followup.send("Full sync completed.")

@bot.command()
async def sync_permissions(ctx):
    """Copies permissions from the source server to the destination server."""
    if ctx.guild.id != DESTINATION_SERVER_ID:
        return

    conn = connect_db()
    if not conn:
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT category_id, category_name FROM channels WHERE category_id IS NOT NULL;")
        categories = cursor.fetchall()

        for category_id, category_name in categories:
            existing_category = discord.utils.get(ctx.guild.categories, name=category_name)
            if existing_category:
                print(f"Category {category_name} already exists.")
                continue

            # Fetch category permissions from source server
            cursor.execute("SELECT role_id, permissions FROM category_permissions WHERE category_id = %s;",
                           (category_id,))
            permissions = cursor.fetchall()
            overwrites = {}

            for role_id, permission in permissions:
                role = ctx.guild.get_role(int(role_id))
                if role:
                    overwrite = discord.PermissionOverwrite(**eval(permission))
                    overwrites[role] = overwrite

            # Create the category with permissions
            new_category = await ctx.guild.create_category(category_name, overwrites=overwrites)
            print(f"📂 Created category {category_name} with permissions")

    except Exception as e:
        print(f"Error syncing permissions: {e}")
    finally:
        cursor.close()
        conn.close()

bot.run(DESTINATION_BOT_TOKEN)
@bot.event
async def on_message(message):
    """Debug incoming messages."""
    print(f"Received message: {message.content} from {message.author} in {message.guild.name}")

    await bot.process_commands(message)  # Ensure commands still work
