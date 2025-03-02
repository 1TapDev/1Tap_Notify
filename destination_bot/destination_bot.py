import discord
import os
import subprocess
import time
from discord.ext import commands
from discord import app_commands
import psycopg2
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

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

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Destination bot logged in as {bot.user}")

    try:
        bot.tree.clear_commands(guild=discord.Object(id=DESTINATION_SERVER_ID))  # Ensure clean state
        await bot.tree.sync(guild=discord.Object(id=DESTINATION_SERVER_ID))  # Register commands
        print(f"✅ Slash commands synced for {bot.user} in server {DESTINATION_SERVER_ID}")
    except Exception as e:
        print(f"❌ Error syncing slash commands: {e}")


@bot.tree.command(name="sync_categories", description="Sync categories from the source server",
                  guild=discord.Object(id=DESTINATION_SERVER_ID))
async def sync_categories(interaction: discord.Interaction):
    """Sync categories from the database and create them in the destination server, excluding certain categories."""
    await interaction.response.defer(thinking=True)

    conn = connect_db()
    if not conn:
        await interaction.followup.send("❌ Database connection failed.")
        return

    cursor = conn.cursor()

    # Get excluded categories
    cursor.execute("SELECT category_id FROM excluded_categories;")
    excluded_categories = {row[0] for row in cursor.fetchall()}

    # Fetch categories
    cursor.execute("SELECT category_id, category_name FROM categories;")
    categories = cursor.fetchall()

    if not categories:
        await interaction.followup.send("❌ No categories found in the database!")
        return

    destination_guild = discord.utils.get(bot.guilds, id=DESTINATION_SERVER_ID)
    if not destination_guild:
        await interaction.followup.send("❌ Destination server not found.")
        return

    response = "📂 **Creating Missing Categories:**\n"
    for category_id, category_name in categories:
        if category_id in excluded_categories:
            print(f"⏩ Skipping excluded category: {category_name}")
            continue  # Skip excluded categories

        existing_category = discord.utils.get(destination_guild.categories, name=category_name)
        if existing_category:
            response += f"✅ Already exists: {category_name}\n"
            continue

        new_category = await destination_guild.create_category(name=category_name)
        response += f"✅ Created: {category_name}\n"

    await interaction.followup.send(response)
    cursor.close()
    conn.close()

@bot.tree.command(name="force_sync", description="Force a full sync of categories & channels",
                  guild=discord.Object(id=DESTINATION_SERVER_ID))
async def force_sync(interaction: discord.Interaction):
    """Manually trigger a full sync of categories and channels."""
    await sync_categories(interaction)  # Call the sync function
    await interaction.followup.send("✅ Full sync completed.")

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
                print(f"✅ Category {category_name} already exists.")
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
        print(f"❌ Error syncing permissions: {e}")
    finally:
        cursor.close()
        conn.close()

bot.run(DESTINATION_BOT_TOKEN)
@bot.event
async def on_message(message):
    """Debug incoming messages."""
    print(f"📥 Received message: {message.content} from {message.author} in {message.guild.name}")

    await bot.process_commands(message)  # Ensure commands still work
