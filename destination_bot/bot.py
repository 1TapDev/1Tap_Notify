import discord
import json
import os
import asyncio
import aiohttp
import redis
import logging
from discord.ext import tasks
from dotenv import load_dotenv
from aiohttp import web

logging.basicConfig(level=logging.ERROR, format="[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("DESTINATION_BOT_TOKEN")

# Load configuration
CONFIG_FILE = "config.json"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

# Connect to Redis
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

DESTINATION_SERVER_ID = config["destination_server"]
EXCLUDED_CATEGORIES = set(config.get("excluded_categories", []))  # Ensure valid set
WEBHOOKS = config.get("webhooks", {})  # Ensure webhooks key exists

# Initialize bot
intents = discord.Intents.default()
bot = discord.Client(intents=intents)

@tasks.loop(seconds=2)  # Poll Redis every 2 seconds
async def process_redis_messages():
    try:
        message_data = redis_client.rpop("message_queue")
        if message_data:
            message = json.loads(message_data)
            await send_to_webhook(message)  # ✅ Forward message silently
    except Exception as e:
        logging.error(f"❌ ERROR: Failed to process Redis messages: {e}")


async def refresh_webhook_cache():
    """Refresh webhook mappings from Redis or config file."""
    global WEBHOOKS
    print("🔄 Refreshing webhook cache...")

    # Fetch updated webhooks from Redis
    try:
        updated_webhooks = redis_client.hgetall("webhooks")  # Ensure this key exists in Redis
        if updated_webhooks:
            WEBHOOKS = updated_webhooks
            print("✅ Webhook cache updated from Redis.")
        else:
            print("⚠️ No webhooks found in Redis. Check if they were stored properly.")
    except Exception as e:
        print(f"❌ ERROR: Failed to refresh webhook cache: {e}")

async def create_channel_and_webhook(category_name, channel_name):
    """Ensure the channel exists in the destination server and create a webhook for it."""
    guild = bot.get_guild(DESTINATION_SERVER_ID)
    if not guild:
        print("❌ ERROR: Destination server not found!")
        return None

    # Find or create category
    category = discord.utils.get(guild.categories, name=category_name)
    if not category:
        print(f"➕ Creating category: {category_name}")
        category = await guild.create_category(category_name)

    # Find or create channel
    channel = discord.utils.get(category.channels, name=channel_name)
    if not channel:
        print(f"➕ Creating channel: {channel_name} under category {category_name}")
        channel = await guild.create_text_channel(name=channel_name, category=category)

    # Get or create webhook
    webhook = await get_or_create_webhook(channel)
    if webhook:
        webhook_key = f"{category_name}/{channel_name}"
        WEBHOOKS[webhook_key] = webhook.url
        redis_client.hset("webhooks", webhook_key, webhook.url)  # Save in Redis
        save_config()  # Save in config.json
        print(f"✅ Webhook created and saved for {category_name}/{channel_name}")
        return webhook.url
    return None


async def send_to_webhook(message_data):
    """Send received message to the appropriate webhook."""
    category_name = message_data.get("category_name", "uncategorized").strip().lower()
    channel_name = message_data["channel_name"].strip().lower()

    # Normalize names
    category_name = category_name.replace(" ", "-").replace("|", "").strip()
    channel_name = channel_name.replace(" ", "-").replace("|", "").strip()

    webhook_key = f"{category_name}/{channel_name}"
    webhook_url = WEBHOOKS.get(webhook_key)

    # If webhook doesn't exist, create the channel & webhook
    if not webhook_url:
        logging.error(f"❌ ERROR: No matching webhook found for '{webhook_key}'. Creating channel & webhook...")
        webhook_url = await create_channel_and_webhook(category_name, channel_name)

    if not webhook_url:
        logging.error(f"❌ ERROR: Still no webhook found for '{webhook_key}' after creation.")
        return

    # ✅ Ensure embeds are valid
    embeds = message_data.get("embeds", [])

    cleaned_embeds = []
    for embed in embeds:
        if not embed:  # Skip empty embeds
            continue

        cleaned_embed = {
            "title": embed.get("title") or "Untitled",
            "description": embed.get("description") or "",
            "url": embed.get("url") or None,
            "color": embed.get("color", 0x000000),  # Default to black if None
            "fields": [
                {"name": field["name"], "value": field["value"]}
                for field in embed.get("fields", []) if "name" in field and "value" in field
            ],
            "thumbnail": {"url": embed["thumbnail"]} if embed.get("thumbnail") else None,
            "image": {"url": embed["image"]} if embed.get("image") else None,
            "footer": {"text": embed["footer"]} if embed.get("footer") else None,
            "author": {"name": embed["author"]} if embed.get("author") else None
        }

        cleaned_embeds.append(cleaned_embed)

    # Only add embeds if they exist
    embeds_to_send = cleaned_embeds if cleaned_embeds else None

    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json={
            "content": message_data.get("content", ""),
            "username": message_data["author_name"],
            "avatar_url": message_data["author_avatar"],
            "embeds": cleaned_embeds
        }) as response:
            if response.status == 204:
                return  # ✅ Treat 204 as success, no need to log it

            if response.status != 200:
                error_text = await response.text()
                logging.error(f"⚠️ Failed to send message to webhook ({response.status}) → {error_text}")


class DestinationBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.webhook_cache = WEBHOOKS  # Load existing webhooks

    async def on_ready(self):
        print(f"✅ Bot {self.user} is running!")

        # Load webhooks from Redis after the bot is ready
        self.webhook_cache = redis_client.hgetall("webhooks")

        await self.ensure_webhooks()
        self.save_config()
        print("✅ Webhook setup complete. Bot is now processing messages.")

        # ✅ Start processing Redis messages
        if not process_redis_messages.is_running():
            process_redis_messages.start()

    async def ensure_webhooks(self):
        """Ensure webhooks exist for all channels in the destination server."""
        guild = self.get_guild(DESTINATION_SERVER_ID)
        if not guild:
            print("❌ ERROR: Destination server not found!")
            return

        for channel in guild.text_channels:
            if channel.category and channel.category.id in EXCLUDED_CATEGORIES:
                continue  # Skip excluded categories

            # ✅ Normalize category and channel names for matching
            category_name = channel.category.name.lower().replace(" ", "-").replace("|", "").replace("│", "").replace(
                "︱", "").replace("⚡", "").strip() if channel.category else "uncategorized"
            channel_name = channel.name.lower().replace(" ", "-").replace("|", "").replace("│", "").replace("︱",
                                                                                                            "").strip()

            webhook_key = f"{category_name}/{channel_name}"

            if webhook_key in self.webhook_cache:
                print(f"🔄 Webhook exists for {category_name}/{channel_name}, skipping creation.")
                continue

            webhook = await self.get_or_create_webhook(channel)
            if webhook:
                self.webhook_cache[webhook_key] = webhook.url
                redis_client.hset("webhooks", webhook_key, webhook.url)  # ✅ Store in Redis
                self.save_config()
                print(f"✅ Created webhook for {category_name}/{channel_name}")

    async def get_or_create_webhook(self, channel):
        """Fetch existing webhook or create a new one."""
        try:
            webhooks = await channel.webhooks()

            if webhooks:
                webhook = webhooks[0]  # ✅ Use existing webhook if available
                category_name = channel.category.name.lower().replace(" ", "-") if channel.category else "uncategorized"
                channel_name = channel.name.lower().replace(" ", "-")  # ✅ Normalize name
                webhook_key = f"{category_name}/{channel_name}"

                self.webhook_cache[webhook_key] = webhook.url
                redis_client.hset("webhooks", webhook_key, webhook.url)  # ✅ Store in Redis
                return webhook

            await asyncio.sleep(2)  # ⏳ Delay before creating new webhook
            new_webhook = await channel.create_webhook(name="MirrorBot Webhook")
            webhook_key = f"{category_name}/{channel_name}"

            self.webhook_cache[webhook_key] = new_webhook.url
            redis_client.hset("webhooks", webhook_key, new_webhook.url)  # ✅ Store in Redis
            print(f"✅ Created webhook for {category_name}/{channel_name}")
            return new_webhook

        except discord.HTTPException as e:
            if e.status == 429:  # Handle rate limits
                retry_after = int(e.response.headers.get("Retry-After", 60))
                print(f"⚠️ Rate limited! Waiting {retry_after} seconds before retrying.")
                await asyncio.sleep(retry_after)  # Wait and retry
                return await self.get_or_create_webhook(channel)

        except discord.Forbidden:
            print(f"❌ ERROR: Missing permissions to create webhook in {channel.name}")
        except Exception as e:
            print(f"❌ ERROR: Failed to create webhook in {channel.name}: {e}")

        return None

    def save_config(self):
        """Save updated config with webhook mappings."""
        config["webhooks"] = self.webhook_cache
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)

async def run_bot():
    """Run the bot"""
    bot = DestinationBot(intents=discord.Intents.default())

    # Load existing webhooks into cache
    bot.webhook_cache = redis_client.hgetall("webhooks")

    await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(run_bot())

