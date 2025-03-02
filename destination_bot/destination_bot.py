import discord
from discord.ext import commands
from discord import app_commands
import os
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
    await bot.tree.sync(guild=discord.Object(id=DESTINATION_SERVER_ID))  # ✅ Use bot.tree instead of tree
    print(f"✅ Slash commands synced for {bot.user} in server {DESTINATION_SERVER_ID}")

@bot.tree.command(name="sync_categories", description="Sync categories from the source server",
                  guild=discord.Object(id=DESTINATION_SERVER_ID))
async def sync_categories(interaction: discord.Interaction):
    """Sync categories from the database."""
    await interaction.response.defer(thinking=True)  # Indicate processing

    conn = connect_db()
    if not conn:
        await interaction.followup.send("❌ Database connection failed.")
        return

    cursor = conn.cursor()
    cursor.execute("SELECT category_id, category_name FROM categories;")
    categories = cursor.fetchall()

    if not categories:
        await interaction.followup.send("❌ No categories found in the database!")
    else:
        response = "📂 **Categories to Sync:**\n"
        for category_id, category_name in categories:
            response += f"- {category_name} (ID: {category_id})\n"

        await interaction.followup.send(response)

    cursor.close()
    conn.close()
    try:
        # Fetch categories from the database
        cursor.execute("SELECT category_id, category_name FROM categories;")
        categories = cursor.fetchall()

        for category_id, category_name in categories:
            # Check if category exists in the destination server
            existing_category = discord.utils.get(ctx.guild.categories, name=category_name)
            if existing_category:
                print(f"✅ Category {category_name} already exists.")
                continue

            # Create the category
            new_category = await ctx.guild.create_category(category_name)
            print(f"📂 Created category {category_name}")

            # Fetch channels under this category
            cursor.execute("SELECT channel_id, channel_name FROM channels WHERE category_id = %s;", (category_id,))
            channels = cursor.fetchall()

            for channel_id, channel_name in channels:
                # Create the channel in the new category
                new_channel = await ctx.guild.create_text_channel(name=channel_name, category=new_category)
                print(f"📌 Created channel {channel_name}")

                # Create a webhook for this channel
                webhook = await new_channel.create_webhook(name="MirrorWebhook")
                webhook_url = webhook.url

                # Store webhook in the database
                cursor.execute("INSERT INTO destination_webhooks (channel_id, webhook_url) VALUES (%s, %s) ON CONFLICT (channel_id) DO NOTHING;", (str(channel_id), webhook_url))
                conn.commit()
                print(f"🔗 Created webhook for {channel_name}")

    except Exception as e:
        print(f"❌ Error syncing categories: {e}")
    finally:
        cursor.close()
        conn.close()

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
