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
DESTINATION_SERVER = config["destination_server"]
EXCLUDED_CATEGORIES = set(config.get("excluded_categories", []))  # Ensure valid set
WEBHOOKS = config.get("webhooks", {})
MESSAGE_DELAY = config["settings"].get("message_delay", 0.75)  # Default to 0.75s delay
DESTINATION_BOT_URL = "http://127.0.0.1:5000/process_message"  # Change if bot.py is remote

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

        # ✅ Only process messages from the monitored servers in config.json
        if str(message.guild.id) not in self.monitored_servers:
            return

        # ✅ Ignore messages sent by the self-bot to prevent infinite loops
        if message.author.id == self.user.id:
            return

        # ✅ Exclude messages from categories specified in `config.json`
        if message.channel.category and message.channel.category.id in EXCLUDED_CATEGORIES:
            return  # ❌ Skip this message

        message_data = {
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "channel_name": message.channel.name,
            "category_name": message.channel.category.name if message.channel.category else "uncategorized",
            "server_id": str(message.guild.id),
            "content": message.content,
            "author_id": str(message.author.id),
            "author_name": str(message.author),
            "author_avatar": message.author.avatar.url if message.author.avatar else None,
            "timestamp": str(message.created_at),
            "attachments": [attachment.url for attachment in message.attachments],
            "embeds": [self.format_embed(embed) for embed in message.embeds],
        }

        # Push message to Redis
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


# Run self-bots safely
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_self_bots())
