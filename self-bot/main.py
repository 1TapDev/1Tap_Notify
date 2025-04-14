import discord
import json
import os
import aiohttp
import asyncio
import logging
import redis
import hashlib
import traceback
import signal
from dotenv import load_dotenv
from datetime import datetime

# Setup logging directory
if not os.path.exists("logs"):
    os.makedirs("logs")

# Generate log filename with date and time
log_filename = datetime.now().strftime("logs/main_%Y-%m-%d_%H-%M-%S.log")

# Configure logging
logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Configure console to only show errors
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)
console_formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
console_handler.setFormatter(console_formatter)
logging.getLogger().addHandler(console_handler)

def log_message(message):
    logging.info(message)  # No print

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
    """Retrieve excluded categories for a given server from TOKENS data."""
    for token_data in TOKENS.values():
        if "servers" in token_data and server_id in token_data["servers"]:
            return set(token_data["servers"][server_id].get("excluded_categories", []))

    return set()  # Return an empty set if no excluded categories are found

def get_excluded_channels(server_id):
    """Retrieve excluded channels for a given server from TOKENS data."""
    for token_data in TOKENS.values():
        if "servers" in token_data and server_id in token_data["servers"]:
            return set(token_data["servers"][server_id].get("excluded_channels", []))
    return set()

def get_server_info(server_id):
    """Retrieve human-readable server name from config.json."""
    return DESTINATION_SERVERS.get(str(server_id), {}).get("info", f"Unknown Server ({server_id})")

class MirrorSelfBot(discord.Client):
    def __init__(self, token, monitored_servers):
        super().__init__(enable_guild_compression=True)
        self.token = token
        self.monitored_servers = {str(server_id) for server_id in monitored_servers}
        self.session = aiohttp.ClientSession()  # ✅ Initialize session here

    async def on_ready(self):
        await self.fetch_guilds()
        print(f"✅ Self-bot {self.user} is now monitoring servers: {self.monitored_servers}")

    async def on_disconnect(self):
        logging.warning(f"🔌 Disconnected from Discord at {datetime.utcnow().isoformat()}")

    async def on_resumed(self):
        logging.info(f"🔄 Connection resumed with Discord at {datetime.utcnow().isoformat()}")

    async def on_message(self, message):
        await asyncio.sleep(0.5)  # Small delay to let Discord register attachments
        """Process messages and ensure they belong to monitored servers."""
        if not message.guild:
            return  # Ignore DMs

        if (
                message.author.bot
                and "posted by" in message.content.lower()
                and len(message.attachments) > 0
        ):
            return

        server = message.guild
        server_id = str(server.id) if server else "Unknown"

        # Fetch the correct server name
        server_name = server.name if server else f"Unknown Server ({server_id})"

        # ✅ Only process messages from servers explicitly listed in config.json
        if server_id not in self.monitored_servers:
            return  # ❌ Skip processing if the server is not listed

        excluded_categories = get_excluded_categories(server_id)
        excluded_channels = get_excluded_channels(server_id)

        # Replace this block inside on_message
        if message.channel.category:
            category_id = message.channel.category.id
            if category_id in excluded_categories:
                return

        if message.channel.id in excluded_channels:
            return

        if not message.content and not message.attachments:
            return  # 🛑 Skip empty messages without content or attachments

        logging.info(f"✅ ACCEPTED: Message from {server_name} (ID: {server_id}) in #{message.channel.name}")

        message_data = {
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "channel_name": message.channel.name,
            "category_name": message.channel.category.name if message.channel.category else "uncategorized",
            "server_id": str(server_id),
            "server_name": server_name,
            "content": message.content,
            "author_id": str(message.author.id),
            "author_name": str(message.author),
            "author_avatar": message.author.avatar.url if message.author.avatar else None,
            "timestamp": str(message.created_at),
            "attachments": [attachment.url for attachment in message.attachments],
            "embeds": [self.format_embed(embed) for embed in message.embeds] or (
                [{"image": {"url": message.attachments[0].url}}] if message.attachments and message.attachments[
                    0].content_type and message.attachments[0].content_type.startswith("image/") else []
            ),

        }

        try:
            redis_client.lpush("message_queue", json.dumps(message_data))
            log_message(f"📩 Pushed message from {message.author} in #{message.channel.name} to Redis.")
        except Exception as e:
            print(f"❌ ERROR: Failed to push message to Redis: {e}")

        # ✅ Send to bot.py
        await self.send_to_destination(message_data)

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
        """Send the message to bot.py and print debug output."""
        if not self.session:
            print("⚠️ ERROR: aiohttp session is not initialized!")
            self.session = aiohttp.ClientSession()

        for attempt in range(retries):
            try:
                async with self.session.post(DESTINATION_BOT_URL, json=message_data) as response:
                    response_text = await response.text()
                    if response.status == 200:
                        return
                    else:
                        print(f"❌ ERROR: Failed to send message ({response.status}) - {response_text}")
            except Exception as e:
                print(f"❌ ERROR: Exception in sending message (attempt {attempt + 1}): {e}")
            await asyncio.sleep(2)

    async def close(self):
        if self.session:
            await self.session.close()  # Ensure session is properly closed

async def start_self_bots():
    for token, token_data in TOKENS.items():
        if token_data.get("disabled", False):
            logging.warning(f"⛔ Token disabled: {token[:10]}... Skipping.")
            continue
        server_ids = set(token_data["servers"].keys())
        print(f"🔹 Loading bot with token {token[:10]}... Monitoring servers: {server_ids}")

        bot = MirrorSelfBot(token, server_ids)

        async def try_start_bot(bot_instance, token):
            delay = 5
            while True:
                try:
                    await bot_instance.start(token)
                except Exception as e:
                    logging.error(f"❌ Bot crashed or disconnected. Reconnecting in {delay} seconds. Error: {e}")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 300)  # cap backoff at 5 minutes

        asyncio.create_task(try_start_bot(bot, token))

def shutdown_handler(bot_instances):
    async def handler():
        logging.info("👋 Shutting down gracefully...")
        for bot in bot_instances:
            await bot.close()
        await asyncio.sleep(2)
        os._exit(0)
    return handler

# Start listening for requests
async def main():
    bot_instances = []

    enabled_tokens = [(t, d) for t, d in TOKENS.items() if not d.get("disabled", False)]
    for index, (token, token_data) in enumerate(enabled_tokens):
        server_ids = set(token_data["servers"].keys())
        print(f"🔹 Loading bot with token {token[:10]}... Monitoring servers: {server_ids}")
        bot = MirrorSelfBot(token, server_ids)
        bot_instances.append(bot)

        async def try_start_bot(bot_instance, token):
            delay = 5
            while True:
                try:
                    await bot_instance.start(token)
                except Exception as e:
                    logging.error(f"❌ Bot crashed or disconnected. Reconnecting in {delay} seconds. Error: {e}")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 300)

        # ⏳ Stagger bot launches by 5 seconds each
        await asyncio.sleep(index * 5)
        asyncio.create_task(try_start_bot(bot, token))

    print("⏳ Waiting for self-bots to finish login...")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        print("👋 Shutting down...")
        for bot in bot_instances:
            await bot.close()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
