import discord
import json
import asyncio
import aiohttp
import redis
import logging
import hashlib
import os
from discord.ext import tasks
from aiohttp import web
from datetime import datetime

# Setup logging directory
if not os.path.exists("logs"):
    os.makedirs("logs")

# Generate log filename with date and time
log_filename = datetime.now().strftime("logs/bot_%Y-%m-%d_%H-%M-%S.log")

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

# Load configuration from config.json
CONFIG_FILE = "config.json"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

BOT_TOKEN = config.get("bot_token")
DESTINATION_SERVER_ID = config["destination_server"]
WEBHOOKS = config.get("webhooks", {})
TOKENS = config.get("tokens", {})

# Connect to Redis
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# Initialize bot
intents = discord.Intents.default()
intents.guilds = True

# Global cache to track recent message_ids and prevent duplicates
recent_message_ids = set()

def normalize_key(category_name, channel_name, server_name):
    norm_category = category_name.lower().replace(" ", "-").replace("|", "").replace("︱", "").replace("⚡", "").strip()
    norm_channel = channel_name.lower().replace(" ", "-").replace("|", "").replace("︱", "").strip()
    norm_server = server_name.lower().replace(" ", "-").replace("|", "").replace("︱", "").strip()
    return f"{norm_category}-[{norm_server}]/{norm_channel}"

async def process_redis_messages():
    try:
        while True:
            message_data = redis_client.rpop("message_queue")
            if message_data:
                message = json.loads(message_data)
                await send_to_webhook(message)
            await asyncio.sleep(2)
    except Exception as e:
        logging.error(f"❌ ERROR: Failed to process Redis messages: {e}")

async def send_to_webhook(message_data):
    message_id = message_data.get("message_id")
    if message_id in recent_message_ids:
        logging.info(f"🔁 Skipping duplicate message ID: {message_id}")
        return
    recent_message_ids.add(message_id)

    # Trim cache size (avoid memory bloat)
    if len(recent_message_ids) > 1000:
        recent_message_ids.pop()

    raw_cat = message_data.get("category_name", "uncategorized").strip()
    raw_srv = message_data.get("server_name", "Unknown Server").strip()
    raw_chan = message_data["channel_name"].strip()

    category_name = raw_cat.lower().replace(" ", "-").replace("|", "")
    server_name = raw_srv.lower().replace(" ", "-").replace("|", "")
    channel_name = raw_chan.lower().replace(" ", "-").replace("|", "")

    webhook_key = f"{category_name}-[{server_name}]/{channel_name}"
    webhook_url = WEBHOOKS.get(webhook_key)

    if not webhook_url:
        # Rebuild category/channel if needed
        webhook_url = await create_channel_and_webhook(category_name, channel_name, server_name)
        if not webhook_url:
            return

    embeds = message_data.get("embeds", [])
    attachments = message_data.get("attachments", [])

    cleaned_embeds = []

    # Handle standard embeds
    for embed in embeds:
        if not embed:
            continue
        cleaned_embed = {
            "title": embed.get("title") or "Untitled",
            "description": embed.get("description") or "",
            "url": embed.get("url"),
            "color": embed.get("color", 0x000000),
            "fields": [
                {"name": f["name"], "value": f["value"]}
                for f in embed.get("fields", []) if "name" in f and "value" in f
            ],
            "thumbnail": {"url": embed["thumbnail"]} if embed.get("thumbnail") else None,
            "image": {"url": embed["image"]} if embed.get("image") else None,
            "footer": {"text": embed["footer"]} if embed.get("footer") else None,
            "author": {"name": embed["author"]} if embed.get("author") else None
        }
        cleaned_embeds.append(cleaned_embed)

    # If no embeds and we have attachments, create a fallback image embed
    if not cleaned_embeds and attachments:
        cleaned_embeds.append({
            "title": None,
            "description": "",
            "image": {"url": attachments[0]}
        })

    cleaned_embeds = []
    for embed in embeds:
        if not embed:
            continue
        cleaned_embed = {
            "title": embed.get("title") or "Untitled",
            "description": embed.get("description") or "",
            "url": embed.get("url"),
            "color": embed.get("color", 0x000000),
            "fields": [
                {"name": f["name"], "value": f["value"]}
                for f in embed.get("fields", []) if "name" in f and "value" in f
            ],
            "thumbnail": {"url": embed["thumbnail"]} if embed.get("thumbnail") else None,
            "image": {"url": embed["image"]} if embed.get("image") else None,
            "footer": {"text": embed["footer"]} if embed.get("footer") else None,
            "author": {"name": embed["author"]} if embed.get("author") else None
        }
        cleaned_embeds.append(cleaned_embed)

    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json={
            "content": message_data.get("content", ""),
            "username": message_data.get("author_name", "Unknown"),
            "avatar_url": message_data.get("author_avatar"),
            "embeds": cleaned_embeds
        }) as response:
            if response.status != 204 and response.status != 200:
                error_text = await response.text()
                logging.error(f"⚠️ Failed to send message to webhook ({response.status}) → {error_text}")

class DestinationBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.webhook_cache = WEBHOOKS
        self.event(self.on_ready)

    async def on_ready(self):
        print(f"✅ Bot {self.user} is running!")
        self.webhook_cache = redis_client.hgetall("webhooks")
        await self.ensure_webhooks()
        self.save_config()
        print("✅ Webhook setup complete. Bot is now processing messages.")
        asyncio.create_task(process_redis_messages())

    async def ensure_webhooks(self):
        guild = self.get_guild(DESTINATION_SERVER_ID)
        if not guild:
            logging.error("❌ ERROR: Destination server not found!")
            return

        server_name = guild.name

        for channel in guild.text_channels:
            category_name = channel.category.name.lower().replace(" ", "-").replace("|", "") if channel.category else "uncategorized"
            channel_name = channel.name.lower().replace(" ", "-").replace("|", "")
            webhook_key = normalize_key(category_name, channel_name, server_name)

            if webhook_key in self.webhook_cache:
                continue

            webhook = await self.get_or_create_webhook(channel, server_name)
            if webhook:
                webhook_url = webhook.url
                WEBHOOKS[webhook_key] = webhook_url
                redis_client.hset("webhooks", webhook_key, webhook_url)
                self.save_config()
                logging.info(f"✅ Created webhook for {category_name}/{channel_name}")

    async def get_or_create_webhook(self, channel, server_name):
        try:
            webhooks = await channel.webhooks()
            if webhooks:
                return webhooks[0]
            await asyncio.sleep(1.5)
            return await channel.create_webhook(name="1Tap Notify")
        except Exception as e:
            logging.error(f"❌ ERROR: Failed to create webhook in {channel.name}: {e}")
            return None

    def save_config(self):
        config["webhooks"] = self.webhook_cache
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)

async def create_channel_and_webhook(category_name, channel_name, server_name):
    guild = bot.get_guild(DESTINATION_SERVER_ID)
    if not guild:
        return None

    # First, check if the channel already exists anywhere in the server
    existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
    if existing_channel:
        logging.info(f"📦 Found existing channel: {channel_name} (ID: {existing_channel.id})")
        webhook = await bot.get_or_create_webhook(existing_channel, server_name)
        if webhook:
            webhook_key = normalize_key(category_name, channel_name, server_name)
            WEBHOOKS[webhook_key] = webhook.url
            redis_client.hset("webhooks", webhook_key, webhook.url)
            bot.save_config()
            return webhook.url
        return None

    # ✅ FIX: Match any existing category that closely matches the intended category name
    category = None
    for cat in guild.categories:
        cat_normalized = cat.name.lower().replace(" ", "-").replace("|", "").replace("︱", "")
        if category_name in cat_normalized:
            category = cat
            break

    template_category = discord.utils.get(guild.categories, name="INFORMATION [AK CHEFS]")
    overwrites = template_category.overwrites if template_category else {}

    if not category:
        try:
            full_category_name = f"{category_name} [{server_name}]"
            category = await guild.create_category(full_category_name, overwrites=overwrites)
            logging.info(f"✅ Created category: {full_category_name}")
        except Exception as e:
            logging.error(f"❌ Failed to create category '{full_category_name}': {e}")
            return None

    try:
        channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites)
        logging.info(f"✅ Created channel: {channel_name}")
    except Exception as e:
        logging.error(f"❌ Failed to create channel '{channel_name}': {e}")
        return None

    webhook = await bot.get_or_create_webhook(channel, server_name)
    if webhook:
        webhook_key = normalize_key(category_name, channel_name, server_name)
        WEBHOOKS[webhook_key] = webhook.url
        redis_client.hset("webhooks", webhook_key, webhook.url)
        bot.save_config()
        return webhook.url

    return None


async def process_message(request):
    try:
        message_data = await request.json()
        logging.info(f"📩 Received message: {message_data}")
        redis_client.lpush("message_queue", json.dumps(message_data))
        return web.json_response({"status": "success", "message": "Message received"}, status=200)
    except Exception as e:
        logging.error(f"❌ ERROR: Failed to process message: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def start_web_server():
    app = web.Application()
    app.router.add_post("/process_message", process_message)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 5000)
    await site.start()

async def run_bot():
    global bot
    bot = DestinationBot(intents=discord.Intents.default())
    bot.webhook_cache = redis_client.hgetall("webhooks")
    await asyncio.gather(bot.start(BOT_TOKEN), start_web_server())

if __name__ == "__main__":
    asyncio.run(run_bot())
