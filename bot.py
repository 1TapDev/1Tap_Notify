import discord
import asyncio
import os
import subprocess
import sys
import time
import psycopg2
import datetime
from psycopg2.extras import DictCursor
from dotenv import load_dotenv
from discord_webhook import DiscordWebhook

while True:
    try:
        subprocess.run(["python", "bot.py"])
    except Exception as e:
        print(f"❌ Self-bot crashed: {e}")
        time.sleep(5)  # Wait before restarting

# Load environment variables
load_dotenv()

# Read multiple tokens from .env
TOKENS = os.getenv("DISCORD_TOKENS").split(",")

# PostgreSQL Configuration
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")

DESTINATION_SERVER_ID = int(os.getenv("DESTINATION_SERVER_ID"))

DESTINATION_BOT_PATH = os.path.join(os.getcwd(), "destination_bot", "destination_bot.py")
VENV_ACTIVATE = os.path.join(os.getcwd(), "destination_bot", ".venv", "Scripts", "activate.bat")  # Windows

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

# Fetch monitored servers for a specific token
def get_monitored_servers(token):
    conn = connect_db()
    if not conn:
        return []

    cursor = conn.cursor(cursor_factory=DictCursor)
    try:
        cursor.execute("SELECT server_id FROM servers WHERE token = %s;", (token,))
        servers = [row["server_id"] for row in cursor.fetchall()]

        print(f"✅ Token {token} is assigned to servers: {servers}")  # Debugging Output

        return servers
    except Exception as e:
        print(f"Error fetching monitored servers: {e}")
        return []
    finally:
        cursor.close()
        conn.close()

# Fetch channel-webhook mappings
def get_webhook_for_channel(channel_id):
    conn = connect_db()
    if not conn:
        return None

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT webhook_url FROM channels WHERE channel_id = %s;", (channel_id,))
        result = cursor.fetchone()
        return result[0] if result else None
    except Exception as e:
        print(f"Error fetching webhook: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

def get_excluded_categories():
    """Retrieve a set of category IDs to exclude from mirroring."""
    conn = connect_db()
    if not conn:
        return set()

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT category_id FROM excluded_categories;")
        excluded = {row[0] for row in cursor.fetchall()}
        return excluded
    except Exception as e:
        print(f"Error fetching excluded categories: {e}")
        return set()
    finally:
        cursor.close()
        conn.close()

def start_destination_bot():
    """Start the destination bot in a separate process."""
    if not os.path.exists(DESTINATION_BOT_PATH):
        print("❌ ERROR: destination_bot.py not found!")
        return

    print("🚀 Starting Destination Bot...")

    if sys.platform == "win32":
        command = f'cmd /c ""{VENV_ACTIVATE}" && python "{DESTINATION_BOT_PATH}""'
    else:
        venv_activate = os.path.join(os.getcwd(), "destination_bot", ".venv", "bin", "activate")
        command = f'bash -c "source {venv_activate} && python {DESTINATION_BOT_PATH}"'

    subprocess.Popen(command, shell=True)

# SelfBot class
class SelfBot(discord.Client):
    def __init__(self, token, monitored_servers, **options):
        super().__init__(**options)  # Remove intents
        self.token = token
        self.monitored_servers = monitored_servers

    async def on_ready(self):
        print(f"✅ Logged in as {self.user} (ID: {self.user.id}) monitoring servers: {self.monitored_servers}")
        await self.store_server_structure()  # Store categories & channels when the bot starts

    async def on_guild_channel_create(self, channel):
        """Detect and store new channels in the database."""
        if channel.guild.id not in self.monitored_servers:
            return

        conn = connect_db()
        if conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO channels (category_id, category_name, channel_id, channel_name, server_id) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (channel_id) DO NOTHING;",
                    (
                        str(channel.category.id) if channel.category else None,
                        channel.category.name if channel.category else None,
                        str(channel.id),
                        channel.name,
                        str(channel.guild.id)
                    )
                )
                conn.commit()
                print(f"📌 New channel detected: {channel.name}")
            except Exception as e:
                print(f"❌ Error storing new channel: {e}")
            finally:
                cursor.close()
                conn.close()

    async def store_server_structure(self):
        """Fetch and store categories & channels from source servers."""
        conn = connect_db()
        if not conn:
            return
        cursor = conn.cursor()

        for guild in self.guilds:
            if str(guild.id) not in self.monitored_servers:
                continue  # Skip unmonitored servers

            print(f"📂 Storing structure for {guild.name} ({guild.id})")

            # Store categories
            for category in guild.categories:
                cursor.execute("""
                    INSERT INTO categories (category_id, category_name, server_id) 
                    VALUES (%s, %s, %s) ON CONFLICT (category_id) DO NOTHING;
                """, (str(category.id), category.name, str(guild.id)))
                print(f"✅ Stored category: {category.name}")

            # Store text channels
            for channel in guild.text_channels:
                cursor.execute("""
                    INSERT INTO channels (category_id, category_name, channel_id, channel_name, server_id) 
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (channel_id) DO NOTHING;
                """, (
                    str(channel.category_id) if channel.category else None,
                    channel.category.name if channel.category else None,
                    str(channel.id),
                    channel.name,
                    str(guild.id)
                ))
                print(f"✅ Stored channel: {channel.name}")

        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Finished storing server structure.")

    async def on_message(self, message):
        if message.guild and str(message.guild.id) not in self.monitored_servers:
            return  # Ignore messages from unmonitored servers

        if message.author.id == self.user.id:  # Ignore self-messages
            return

        # Get webhook for the channel
        webhook_url = get_webhook_for_channel(str(message.channel.id))
        if not webhook_url:
            return

        has_attachment = False
        attachment_data = []

        # Process attachments
        if message.attachments:
            has_attachment = True
            for attachment in message.attachments:
                attachment_data.append((str(message.id), attachment.url, attachment.filename, attachment.size))

        # Save message & attachments in PostgreSQL
        conn = connect_db()
        if conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO messages (message_id, channel_id, content, author_id, timestamp, has_attachment) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING;",
                    (str(message.id), str(message.channel.id), message.content, str(message.author.id),
                     message.created_at, has_attachment)
                )

                # Insert attachments if available
                if has_attachment:
                    cursor.executemany(
                        "INSERT INTO attachments (message_id, url, filename, size) VALUES (%s, %s, %s, %s);",
                        attachment_data
                    )

                conn.commit()
                print(f"📩 Stored message {message.id} from {message.author} with {len(attachment_data)} attachment(s).")
            except Exception as e:
                print(f"❌ Error inserting message: {e}")
            finally:
                cursor.close()
                conn.close()

        # Prepare webhook message
        webhook = DiscordWebhook(url=webhook_url, content=message.content, username=message.author.name,
                                 avatar_url=str(message.author.avatar_url))

        # Add attachments to webhook
        for attachment in message.attachments:
            webhook.add_file(url=attachment.url, filename=attachment.filename)

        webhook.execute()

    async def on_message_edit(self, before, after):
        """Detect and store message edits."""
        if before.guild and str(before.guild.id) not in self.monitored_servers:
            return  # Ignore messages from unmonitored servers

        conn = connect_db()
        if conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "UPDATE messages SET content = %s, edited_at = %s, is_edited = TRUE WHERE message_id = %s;",
                    (after.content, datetime.datetime.utcnow(), str(after.id))
                )
                conn.commit()
                print(f"✏️ Edited message {after.id} from {after.author}")
            except Exception as e:
                print(f"❌ Error updating edited message: {e}")
            finally:
                cursor.close()
                conn.close()

        # Notify Webhook about the edit
        webhook_url = get_webhook_for_channel(str(after.channel.id))
        if webhook_url:
            webhook = DiscordWebhook(url=webhook_url, content=f"✏ **Message Edited:**\n{after.content}",
                                     username=after.author.name)
            webhook.execute()

    async def on_message_delete(self, message):
        """Detect and store message deletions."""
        if message.guild and str(message.guild.id) not in self.monitored_servers:
            return  # Ignore messages from unmonitored servers

        conn = connect_db()
        if conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "UPDATE messages SET is_deleted = TRUE WHERE message_id = %s;",
                    (str(message.id),)
                )
                conn.commit()
                print(f"🗑 Deleted message {message.id} from {message.author}")
            except Exception as e:
                print(f"❌ Error updating deleted message: {e}")
            finally:
                cursor.close()
                conn.close()

        # Notify Webhook about the deletion
        webhook_url = get_webhook_for_channel(str(message.channel.id))
        if webhook_url:
            webhook = DiscordWebhook(url=webhook_url, content=f"🗑 **Message Deleted**", username=message.author.name)
            webhook.execute()

    async def on_thread_create(self, thread):
        """Detect and store new threads."""
        if str(thread.guild.id) not in self.monitored_servers:
            return  # Ignore threads from unmonitored servers

        conn = connect_db()
        if conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO threads (thread_id, channel_id, name, creator_id, created_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (thread_id) DO NOTHING;",
                    (str(thread.id), str(thread.parent.id), thread.name, str(thread.owner_id), thread.created_at)
                )
                conn.commit()
                print(f"🧵 Created thread: {thread.name} in {thread.parent.name}")
            except Exception as e:
                print(f"❌ Error inserting thread: {e}")
            finally:
                cursor.close()
                conn.close()

        # Forward thread creation to webhook
        webhook_url = get_webhook_for_channel(str(thread.parent.id))
        if webhook_url:
            webhook = DiscordWebhook(url=webhook_url, content=f"🧵 **New Thread Created:** {thread.name}",
                                     username="Thread Bot")
            webhook.execute()

    async def on_thread_update(self, before, after):
        """Detect when a thread is archived or unarchived."""
        if str(before.guild.id) not in self.monitored_servers:
            return

        conn = connect_db()
        if conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "UPDATE threads SET is_archived = %s WHERE thread_id = %s;",
                    (after.archived, str(after.id))
                )
                conn.commit()
                status = "archived" if after.archived else "unarchived"
                print(f"📂 Thread {after.name} has been {status}.")
            except Exception as e:
                print(f"❌ Error updating thread: {e}")
            finally:
                cursor.close()
                conn.close()

        # Notify Webhook about archive status
        webhook_url = get_webhook_for_channel(str(after.parent.id))
        if webhook_url:
            status_text = "📂 **Thread Archived:**" if after.archived else "📂 **Thread Unarchived:**"
            webhook = DiscordWebhook(url=webhook_url, content=f"{status_text} {after.name}", username="Thread Bot")
            webhook.execute()

    async def sync_category(self, category):
        """Create a mirrored category in the destination server with the same permissions."""
        destination_guild = discord.utils.get(self.guilds, id=DESTINATION_SERVER_ID)
        if not destination_guild:
            print(f"❌ Destination server not found.")
            return

        # Check if the category already exists
        existing_category = discord.utils.get(destination_guild.categories, name=category.name)
        if existing_category:
            print(f"✅ Category {category.name} already exists in destination.")
        else:
            # Copy permissions
            overwrites = {destination_guild.get_member(role.id): discord.PermissionOverwrite(**vars(perm))
                          for role, perm in category.overwrites.items()}

            # Create the category in the destination server
            new_category = await destination_guild.create_category(name=category.name, overwrites=overwrites)
            print(f"📂 Created category {category.name} in destination.")

            # Sync all channels inside the category
            for channel in category.channels:
                await self.sync_channel(channel, new_category)

    async def sync_channel(self, channel, new_category):
        """Create a mirrored channel in the destination server with the same permissions."""
        destination_guild = discord.utils.get(self.guilds, id=DESTINATION_SERVER_ID)
        if not destination_guild:
            print(f"❌ Destination server not found.")
            return

        # Check if the channel already exists
        existing_channel = discord.utils.get(destination_guild.text_channels, name=channel.name)
        if existing_channel:
            print(f"✅ Channel {channel.name} already exists in destination.")
            return

        # Copy permissions
        overwrites = {destination_guild.get_member(role.id): discord.PermissionOverwrite(**vars(perm))
                      for role, perm in channel.overwrites.items()}

        # Create the channel inside the new category
        await destination_guild.create_text_channel(name=channel.name, category=new_category, overwrites=overwrites)
        print(f"📌 Created channel {channel.name} in destination.")

# Function to start multiple bots
async def start_bots():
    bots = []
    for token in TOKENS:
        monitored_servers = get_monitored_servers(token)
        if not monitored_servers:
            print(f"⚠ No monitored servers for token: {token}")
            continue

        bot = SelfBot(token, monitored_servers)
        bots.append(bot)
        asyncio.create_task(bot.start(token))  # ✅ Corrected, runs inside function

    await asyncio.gather(*[bot.wait_until_ready() for bot in bots])

if __name__ == "__main__":
    start_destination_bot()  # 🚀 Start destination bot automatically
    asyncio.run(start_bots())  # Start self-bot