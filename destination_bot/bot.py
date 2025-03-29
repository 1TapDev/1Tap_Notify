import discord
import json
import asyncio
import aiohttp
import redis
import logging
import hashlib
import argparse
import os
from discord.ext import tasks
from aiohttp import web
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--queue", default="message_queue", help="Redis queue name")
args = parser.parse_args()
QUEUE_NAME = args.queue

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

async def monitor_for_archive():
    await bot.wait_until_ready()
    guild = bot.get_guild(DESTINATION_SERVER_ID)

    if not guild:
        logging.error("❌ Destination server not found in monitor_for_archive")
        return

    def is_archive_message(msg):
        return (
                isinstance(msg.channel, discord.TextChannel)
                and "!archive" in msg.content.lower()
        )

    while not bot.is_closed():
        try:
            for channel in guild.text_channels:
                try:
                    messages = [message async for message in channel.history(limit=50)]
                    for msg in messages:
                        if isinstance(msg.channel, discord.TextChannel) and "!archive" in msg.content.lower():
                            logging.info(f"🗑️ Deleting channel '{channel.name}' due to archive trigger: {msg.content}")
                            await channel.delete(reason="Archive keyword detected in message")
                            break  # Stop checking more messages in this channel
                except discord.Forbidden:
                    logging.warning(f"⚠️ No permission to read/delete in channel: {channel.name}")
                except Exception as e:
                    logging.error(f"❌ Failed checking channel '{channel.name}': {e}")
            await asyncio.sleep(10)  # Check every 10 seconds
        except Exception as e:
            logging.error(f"❌ monitor_for_archive loop error: {e}")
            await asyncio.sleep(5)

async def process_redis_messages():
    try:
        while True:
            processed = 0
            while True:
                message_data = redis_client.rpop(QUEUE_NAME)
                if not message_data:
                    break

                try:
                    # Ensure we only process dict-like JSON objects
                    message = json.loads(message_data)

                    if not isinstance(message, dict):
                        logging.warning(f"⚠️ Skipped invalid message (not a dict): {repr(message)}")
                        continue

                    await send_to_webhook(message)
                    processed += 1

                except json.JSONDecodeError as je:
                    logging.error(f"❌ JSON decode error: {je} → Raw data: {repr(message_data)}")
                except Exception as e:
                    logging.error(f"❌ Failed to process single Redis message: {e} → Data: {repr(message_data)}")

            if processed > 0:
                logging.info(f"✅ Processed {processed} messages from queue.")

            await asyncio.sleep(1)
    except Exception as e:
        logging.error(f"❌ ERROR: Failed to process Redis messages: {e}")


async def clean_mentions(content: str, destination_guild: discord.Guild) -> str:
    # Replace <#channel_id> with #channel-name
    for channel in destination_guild.text_channels:
        mention = f"<#{channel.id}>"
        if mention in content:
            content = content.replace(mention, f"#{channel.name}")

    # Replace <@user_id> with @username#discriminator
    import re
    user_mentions = re.findall(r"<@!?(\d+)>", content)
    for user_id in user_mentions:
        try:
            user_obj = await bot.fetch_user(int(user_id))
            tag = f"@{user_obj.name}#{user_obj.discriminator}"
            content = re.sub(f"<@!?{user_id}>", tag, content)
        except Exception:
            content = content.replace(f"<@{user_id}>", "@unknown")

    return content

async def send_to_webhook(message_data):
    message_id = message_data.get("message_id")
    if message_id in recent_message_ids:
        return
    recent_message_ids.add(message_id)
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
        webhook_url = await create_channel_and_webhook(category_name, channel_name, server_name)
        if not webhook_url:
            return

    content = await clean_mentions(message_data.get("content", ""), bot.get_guild(DESTINATION_SERVER_ID))
    reply_text = f"[in reply to @{message_data['reply_to']}]" if message_data.get("reply_to") else ""
    if reply_text:
        content = f"{reply_text}\n{content}"

    attachments = message_data.get("attachments", [])
    embeds = message_data.get("embeds", [])

    cleaned_embeds = []
    for embed in embeds:
        if isinstance(embed, dict):
            cleaned = {
                "title": embed.get("title"),
                "description": embed.get("description", ""),
                "url": embed.get("url"),
                "color": embed.get("color", 0x000000),
                "fields": [
                    {"name": f["name"], "value": f["value"]}
                    for f in embed.get("fields", []) if "name" in f and "value" in f
                ],
                "thumbnail": {"url": embed.get("thumbnail")} if embed.get("thumbnail") else None,
                "image": {"url": embed.get("image")} if embed.get("image") else None,
                "footer": {"text": embed.get("footer", {}).get("text", "")} if embed.get("footer") else None,
                "author": {"name": embed.get("author", {}).get("name", "")} if embed.get("author") else None,
            }
            cleaned_embeds.append(cleaned)

    if not content and not cleaned_embeds and attachments:
        cleaned_embeds.append({"image": {"url": attachments[0]}})

    parts = [content[i:i + 2000] for i in range(0, len(content), 2000)] if content else [""]
    if not parts and not cleaned_embeds:
        return

    async with aiohttp.ClientSession() as session:
        for part in parts:
            for attempt in range(3):
                try:
                    payload = {
                        "content": part if part else None,
                        "username": message_data.get("author_name", "Unknown"),
                        "avatar_url": message_data.get("author_avatar"),
                        "embeds": cleaned_embeds if part == parts[0] else []
                    }
                    if not payload["content"]:
                        del payload["content"]

                    async with session.post(webhook_url, json=payload) as response:
                        if response.status in (200, 204):
                            return
                        elif response.status == 404 and "Unknown Webhook" in await response.text():
                            logging.warning(f"⚠️ Webhook deleted for {webhook_key}. Recreating...")
                            WEBHOOKS.pop(webhook_key, None)
                            redis_client.hdel("webhooks", webhook_key)
                            webhook_url = await create_channel_and_webhook(category_name, channel_name, server_name)
                            if not webhook_url:
                                return
                        elif response.status >= 500:
                            logging.warning(f"⚠️ Discord error {response.status}, retry {attempt + 1}")
                            await asyncio.sleep(2 * (attempt + 1))
                        else:
                            logging.error(f"❌ Webhook failed ({response.status}) → {await response.text()}")
                            return
                except Exception as e:
                    logging.error(f"❌ Exception during webhook post: {e}")
                    await asyncio.sleep(2 * (attempt + 1))

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
        asyncio.create_task(monitor_for_archive())

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
