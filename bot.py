import discord
from discord.ext import commands
import os
import psycopg2
import asyncio
from dotenv import load_dotenv
from psycopg2.extras import DictCursor
from discord_webhook import DiscordWebhook

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


# SelfBot class using commands.Bot (recommended)
class SelfBot(commands.Bot):
    def __init__(self, token, monitored_servers, **options):
        super().__init__(command_prefix="!", self_bot=True, **options)
        self.token = token
        self.monitored_servers = monitored_servers

    async def on_ready(self):
        print(f"✅ Logged in as {self.user} (ID: {self.user.id}) monitoring servers: {self.monitored_servers}")

    async def on_message(self, message):
        if message.guild and str(message.guild.id) not in self.monitored_servers:
            return  # Ignore messages from unmonitored servers

        if message.author.id == self.user.id:  # Ignore self-messages
            return

        # Get webhook for the channel
        webhook_url = get_webhook_for_channel(str(message.channel.id))
        if not webhook_url:
            return

        # Save message in PostgreSQL
        conn = connect_db()
        if conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO messages (message_id, channel_id, content, author_id) VALUES (%s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING;",
                    (str(message.id), str(message.channel.id), message.content, str(message.author.id))
                )
                conn.commit()
                print(f"📩 Stored message {message.id} from {message.author}")
            except Exception as e:
                print(f"❌ Error inserting message: {e}")
            finally:
                cursor.close()
                conn.close()

        # Prepare webhook message
        webhook = DiscordWebhook(url=webhook_url, content=message.content, username=message.author.name,
                                 avatar_url=str(message.author.avatar_url))
        webhook.execute()


# Function to start multiple bots
async def start_bots():
    bots = []
    for token in TOKENS:
        monitored_servers = get_monitored_servers(token)
        if not monitored_servers:
            print(f"⚠ No monitored servers for token: {token}")
            continue

        bot = SelfBot(token, monitored_servers, intents=discord.Intents.all())
        bots.append(bot)
        asyncio.create_task(bot.start(token, bot=False))  # Run each bot as a task

    await asyncio.gather(*[bot.wait_until_ready() for bot in bots])


# Run the bots
asyncio.run(start_bots())