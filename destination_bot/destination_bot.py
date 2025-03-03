import discord
import os
import logging
import psycopg2
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

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
        log_message(f"Database connection error: {e}")
        return None

# Define bot with command tree (for slash commands)
intents = discord.Intents.all()
intents.guilds = True
intents.messages = True  # ✅ Ensure bot receives messages

bot = commands.Bot(command_prefix="!", intents=intents)  # ✅ FIXED SYNTAX ERROR (removed extra ')')

@bot.event
async def on_ready():
    log_message(f"✅ Destination bot logged in as {bot.user}")
    try:
        await bot.tree.sync(guild=discord.Object(id=DESTINATION_SERVER_ID))
        log_message("✅ Slash commands synced successfully.")
    except Exception as e:
        log_message(f"❌ Failed to sync commands: {e}")

# ✅ ADDED sync_categories FUNCTION
async def sync_categories(interaction: discord.Interaction):
    """Syncs categories from the database to the destination server."""
    conn = connect_db()
    if not conn:
        await interaction.response.send_message("Database connection failed.")
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT category_id, category_name FROM categories;")
        categories = cursor.fetchall()

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Error: Could not fetch guild data.")
            return

        synced_categories = []
        for category_id, category_name in categories:
            existing_category = discord.utils.get(guild.categories, name=category_name)
            if existing_category:
                continue

            # Create category
            await guild.create_category(category_name)
            synced_categories.append(category_name)

        await interaction.response.send_message(f"✅ Synced {len(synced_categories)} new categories.")

    except Exception as e:
        await interaction.response.send_message(f"Error syncing categories: {e}")
        log_message(f"Error syncing categories: {e}")
    finally:
        cursor.close()
        conn.close()

# Slash Command to Exclude Categories
@bot.tree.command(name="exclude_category", description="Exclude a category from syncing")
async def exclude_category(interaction: discord.Interaction, category_id: str):
    await interaction.response.defer(thinking=True)

    conn = connect_db()
    if not conn:
        await interaction.followup.send("Database connection failed.")
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT category_name FROM categories WHERE category_id = %s;", (category_id,))
        category = cursor.fetchone()

        if not category:
            await interaction.followup.send(f"Category with ID `{category_id}` not found in the database.")
            return

        cursor.execute("INSERT INTO excluded_categories (category_id) VALUES (%s) ON CONFLICT DO NOTHING;", (category_id,))
        conn.commit()
        await interaction.followup.send(f"Excluded category `{category[0]}` (ID: `{category_id}`) from syncing.")
        log_message(f"⏩ Category {category[0]} ({category_id}) is now excluded.")

    except Exception as e:
        await interaction.followup.send(f"Error excluding category: {e}")
        log_message(f"Error excluding category: {e}")
    finally:
        cursor.close()
        conn.close()

# Slash Command to Force Sync
@bot.tree.command(name="force_sync", description="Force a full sync of categories & channels")
async def force_sync(interaction: discord.Interaction):
    """Manually trigger a full sync of categories and channels."""
    log_message("⚡ force_sync command triggered")
    await interaction.response.defer(thinking=True)

    # Ensure sync_categories is available
    if "sync_categories" in globals():
        await sync_categories(interaction)
        await interaction.followup.send("✅ Full sync completed.")
    else:
        await interaction.followup.send("❌ sync_categories function is missing.")


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
            cursor.execute("SELECT role_id, permissions FROM category_permissions WHERE category_id = %s;", (category_id,))
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

@bot.event
async def on_message(message):
    """Debug incoming messages."""
    print(f"Received message: {message.content} from {message.author} in {message.guild.name}")
    await bot.process_commands(message)  # Ensure commands still work

bot.run(DESTINATION_BOT_TOKEN)  # ✅ MOVED TO THE END (fixes missing event execution)
