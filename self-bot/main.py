import discord
import json
import os
import aiohttp
import asyncio
import logging
import redis
from dotenv import load_dotenv

# Setup logging
if not os.path.exists("logs"):
    os.makedirs("logs")

logging.basicConfig(
    filename="logs/bot.log",
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def log_message(message):
    """Logs a message to both the console and log file."""
    print(message)
    logging.info(message)

# Connect to Redis
redis_client = redis.Redis(host="localhost", port=6379, db=0)

# Load environment variables
load_dotenv()

# Load configuration
CONFIG_FILE = "config.json"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

TOKENS = config["tokens"]
DESTINATION_SERVERS = config.get("destination_servers", {})
EXCLUDED_CATEGORIES = set(config.get("excluded_categories", []))  # Ensure valid set
WEBHOOKS = config.get("webhooks", {})
MESSAGE_DELAY = config["settings"].get("message_delay", 0.75)  # Default to 0.75s delay
DESTINATION_BOT_URL = "http://127.0.0.1:5000/process_message"  # Change if bot.py is remote

def get_excluded_categories(server_id):
    """Retrieve excluded categories for a given server."""
    return set(DESTINATION_SERVERS.get(str(server_id), {}).get("excluded_categories", []))

def get_server_info(server_id):
    """Retrieve human-readable server name from config.json."""
    return DESTINATION_SERVERS.get(str(server_id), {}).get("info", "Unknown Server")

def store_message_in_redis(message_data):
    """Store message in Redis only if it matches the destination server"""
    if message_data["server_id"] != str(DESTINATION_SERVER):  # Ensure server_id is a string
        print(f"⚠️ Skipping message from {message_data['server_id']} (Expected: {DESTINATION_SERVER})")
        return  # Do not store this message

    redis_client.lpush("message_queue", json.dumps(message_data))
    log_message(f"📩 Pushed message from {message_data['author_name']} in #{message_data['channel_name']} to Redis.")

class MirrorSelfBot(discord.Client):
    def __init__(self, token, monitored_servers):
        super().__init__()
        self.token = token
        self.monitored_servers = set(str(server) for server in monitored_servers)

    async def on_ready(self):
        self.session = aiohttp.ClientSession()  # ✅ Open session once when bot starts
        print(f"✅ Self-bot {self.user} is now monitoring: {self.monitored_servers}")

    async def on_message(self, message):
        """Push messages to Redis queue."""
        if not message.guild:
            return

        server_id = str(message.guild.id)
        server_name = get_server_info(server_id)

        print(f"📢 Processing message from {server_name} (ID: {server_id})")
        excluded_categories = get_excluded_categories(server_id)

        if message.channel.category and message.channel.category.id in excluded_categories:
            print(f"❌ Skipping message from {server_name} - Category is excluded")
            return  # ✅ Stop processing this message

        # ✅ Continue processing if not excluded
        message_data = {
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "channel_name": message.channel.name,
            "category_name": message.channel.category.name if message.channel.category else "uncategorized",
            "server_id": server_id,
            "content": message.content,
            "author_id": str(message.author.id),
            "author_name": str(message.author),
            "author_avatar": message.author.avatar.url if message.author.avatar else None,
            "timestamp": str(message.created_at),
            "attachments": [attachment.url for attachment in message.attachments],
            "embeds": [self.format_embed(embed) for embed in message.embeds],
        }

        # ✅ Push message to Redis
        redis_client.lpush("message_queue", json.dumps(message_data))
        log_message(f"📩 Pushed message from {message.author} in #{message.channel.name} to Redis.")

    def format_embed(self, embed):
        """Format an embed for sending to bot.py"""
        return {
            "title": embed.title or None,
            "description": embed.description or None,
            "url": embed.url or None,
            "color": embed.color.value if embed.color else None,
            "fields": [{"name": field.name, "value": field.value} for field in embed.fields] if embed.fields else [],
            "image": embed.image.url if embed.image else None,
            "thumbnail": embed.thumbnail.url if embed.thumbnail else None,
            "footer": embed.footer.text if embed.footer else None,
            "author": embed.author.name if embed.author else None,
        }

    async def send_to_destination(self, message_data, retries=3):
        if not self.session:
            print("⚠️ ERROR: aiohttp session is not initialized!")
            self.session = aiohttp.ClientSession()

        for attempt in range(retries):
            try:
                print(f"🚀 Attempt {attempt + 1}: Sending to bot.py → {DESTINATION_BOT_URL}")
                async with self.session.post(DESTINATION_BOT_URL, json=message_data) as response:
                    response_text = await response.text()
                    print(f"📨 Sent message to bot.py | Status: {response.status} | Response: {response_text}")

                    if response.status == 200:
                        print(f"✅ Successfully sent message: {message_data}")
                        return
                    else:
                        print(f"❌ ERROR: Failed to send message ({response.status}) - {response_text}")
            except Exception as e:
                print(f"❌ ERROR: Exception in sending message (attempt {attempt + 1}): {e}")
            await asyncio.sleep(2)

    async def close(self):
        if self.session:
            await self.session.close()  # Ensure session is properly closed

# Run multiple self-bots
async def start_self_bots():
    clients = []
    for token, monitored_servers in TOKENS.items():
        bot = MirrorSelfBot(token, monitored_servers)
        clients.append(bot.start(token))

    await asyncio.gather(*clients)


async def listen_for_missing_channels():
    """Listen for bot.py requests to provide missing category/channel structure."""
    while True:
        request_data = redis_client.rpop("missing_channels")
        if request_data:
            request = json.loads(request_data)
            category_name = request["category_name"]
            channel_name = request["channel_name"]
            print(f"📨 Received request for {category_name}/{channel_name}. Fetching details...")

            # Fetch details from monitored servers
            category_id, channel_id = await fetch_channel_structure(category_name, channel_name)

            if category_id and channel_id:
                redis_client.hset("channel_structure", f"{category_name}/{channel_name}", json.dumps({
                    "category_id": category_id,
                    "channel_id": channel_id
                }))
                print(f"✅ Provided structure for {category_name}/{channel_name} to bot.py")
            else:
                print(f"⚠️ Could not find {category_name}/{channel_name} in monitored servers.")
        await asyncio.sleep(2)  # Check every 2 seconds


async def fetch_channel_structure(category_name, channel_name):
    """Find the correct category and channel ID from monitored servers."""
    for guild in bot.guilds:
        category = discord.utils.get(guild.categories, name=category_name)
        if category:
            channel = discord.utils.get(category.channels, name=channel_name)
            if channel:
                return category.id, channel.id
    return None, None

# Start listening for requests
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(listen_for_missing_channels())  # Start the listener
    loop.run_until_complete(start_self_bots())
