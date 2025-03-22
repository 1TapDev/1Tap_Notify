import discord
import json
import asyncio
import aiohttp
import redis
import logging
from discord.ext import tasks
from aiohttp import web

logging.basicConfig(level=logging.ERROR, format="[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# Load configuration from config.json
CONFIG_FILE = "config.json"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

BOT_TOKEN = config.get("bot_token")
DESTINATION_SERVER_ID = config["destination_server"]
WEBHOOKS = config.get("webhooks", {})  # Ensure webhooks key exists

# Connect to Redis
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# Initialize bot
intents = discord.Intents.default()
intents.guilds = True  # ✅ Ensure guilds intent is enabled

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

async def create_channel_and_webhook(category_name, channel_name, server_name):
    """Ensure the category and channel exist before creating a webhook."""
    full_category_name = f"{category_name} [{server_name}]"

    guild = bot.get_guild(DESTINATION_SERVER_ID)
    if not guild:
        print(f"❌ ERROR: Destination server (ID: {DESTINATION_SERVER_ID}) not found!")
        return None

    print(f"🔍 Checking category '{full_category_name}' in {guild.name}...")

    # 🔁 Copy permissions from a template category
    template_category = discord.utils.get(guild.categories, name="INFORMATION [AK CHEFS]")
    overwrites = template_category.overwrites if template_category else {}

    category = discord.utils.get(guild.categories, name=full_category_name)
    if not category:
        try:
            category = await guild.create_category(full_category_name, overwrites=overwrites)
            print(f"✅ Created category: {full_category_name} (ID: {category.id})")
        except discord.Forbidden:
            print(f"❌ ERROR: Bot lacks permission to create category '{full_category_name}'!")
            return None
        except Exception as e:
            print(f"❌ ERROR: Failed to create category '{full_category_name}': {e}")
            return None

    print(f"🔍 Checking channel '{channel_name}' in category '{category.name}'...")

    channel = discord.utils.get(category.channels, name=channel_name)
    if not channel:
        try:
            # Use the same overwrites for the channel
            channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites)
            print(f"✅ Created channel: {channel_name} (ID: {channel.id})")
        except discord.Forbidden:
            print(f"❌ ERROR: Bot lacks permission to create channel '{channel_name}'!")
            return None
        except Exception as e:
            print(f"❌ ERROR: Failed to create channel '{channel_name}': {e}")
            return None

    print(f"🔍 Creating webhook for '{channel_name}'...")

    webhook = await bot.get_or_create_webhook(channel)
    if webhook:
        webhook_key = f"{category_name}/{channel_name}"
        WEBHOOKS[webhook_key] = webhook.url
        redis_client.hset("webhooks", webhook_key, webhook.url)
        bot.save_config()
        print(f"✅ Webhook created and saved for '{webhook_key}'")
        return webhook.url

    return None


async def send_to_webhook(message_data):
    """Send received message to the appropriate webhook."""
    category_name = message_data.get("category_name", "uncategorized").strip().lower()
    channel_name = message_data["channel_name"].strip().lower()

    server_name = message_data.get("server_name", f"Unknown Server ({message_data.get('server_id', '000000')})")

    category_name = category_name.replace(" ", "-").replace("|", "").strip()
    channel_name = channel_name.replace(" ", "-").replace("|", "").strip()

    webhook_key = f"{category_name}/{channel_name}"
    webhook_url = WEBHOOKS.get(webhook_key)

    if not webhook_url:
        logging.error(f"❌ ERROR: No matching webhook found for '{webhook_key}'. Creating channel & webhook...")
        webhook_url = await create_channel_and_webhook(category_name, channel_name, server_name)

    if not webhook_url:
        logging.error(f"❌ ERROR: Still no webhook found for '{webhook_key}' after creation.")
        return

    embeds = message_data.get("embeds", [])

    cleaned_embeds = []
    for embed in embeds:
        if not embed:
            continue

        cleaned_embed = {
            "title": embed.get("title") or "Untitled",
            "description": embed.get("description") or "",
            "url": embed.get("url") or None,
            "color": embed.get("color", 0x000000),
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

    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json={
            "content": message_data.get("content", ""),
            "username": message_data.get("author_name", "Unknown"),
            "avatar_url": message_data.get("author_avatar", None),
            "embeds": cleaned_embeds
        }) as response:
            if response.status == 204:
                return
            if response.status != 200:
                error_text = await response.text()
                logging.error(f"⚠️ Failed to send message to webhook ({response.status}) → {error_text}")

class DestinationBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.webhook_cache = WEBHOOKS
        self.event(self.on_ready)

    async def on_ready(self):
        print(f"✅ Bot {self.user} is running!")
        print("🔍 Checking available servers...")

        # Load webhooks from Redis after the bot is ready
        self.webhook_cache = redis_client.hgetall("webhooks")

        await self.ensure_webhooks()
        self.save_config()
        print("✅ Webhook setup complete. Bot is now processing messages.")

        for guild in self.guilds:
            print(f"➡️ {guild.name} (ID: {guild.id})")

        guild = self.get_guild(DESTINATION_SERVER_ID)
        if guild:
            print(f"✅ Connected to destination server: {guild.name} (ID: {DESTINATION_SERVER_ID})")
        else:
            print(f"❌ ERROR: Destination server (ID: {DESTINATION_SERVER_ID}) not found!")

        # ✅ Start processing Redis messages here
        process_redis_messages.start()

    async def ensure_webhooks(self):
        """Ensure webhooks exist for all channels in the destination server."""
        guild = self.get_guild(DESTINATION_SERVER_ID)
        if not guild:
            print("❌ ERROR: Destination server not found!")
            return

        for channel in guild.text_channels:
            # ✅ Normalize category and channel names for matching
            category_name = channel.category.name.lower().replace(" ", "-").replace("|", "").replace("│", "").replace(
                "︱", "").replace("⚡", "").strip() if channel.category else "uncategorized"
            channel_name = channel.name.lower().replace(" ", "-").replace("|", "").replace("│", "").replace("︱",
                                                                                                            "").strip()

            webhook_key = f"{category_name}/{channel_name}"

            if webhook_key in self.webhook_cache:
                continue

            webhook = await self.get_or_create_webhook(channel)
            if webhook:
                self.webhook_cache[webhook_key] = webhook.url
                redis_client.hset("webhooks", webhook_key, webhook.url)  # ✅ Store in Redis
                self.save_config()
                print(f"✅ Created webhook for {category_name}/{channel_name}")

    async def get_or_create_webhook(self, channel):
        try:
            webhooks = await channel.webhooks()

            # 🔧 Define category_name and channel_name safely
            category_name = channel.category.name.lower().replace(" ", "-") if channel.category else "uncategorized"
            channel_name = channel.name.lower().replace(" ", "-")
            webhook_key = f"{category_name}/{channel_name}"

            if webhooks:
                webhook = webhooks[0]
                category_name = channel.category.name.lower().replace(" ", "-") if channel.category else "uncategorized"
                channel_name = channel.name.lower().replace(" ", "-")
                webhook_key = f"{category_name}/{channel_name}"

                self.webhook_cache[webhook_key] = webhook.url
                redis_client.hset("webhooks", webhook_key, webhook.url)
                return webhook

            await asyncio.sleep(2)
            new_webhook = await channel.create_webhook(name="1Tap Notify")
            webhook_key = f"{category_name}/{channel_name}"

            self.webhook_cache[webhook_key] = new_webhook.url
            redis_client.hset("webhooks", webhook_key, new_webhook.url)
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

async def process_message(request):
    """Handle messages received from main.py"""
    try:
        message_data = await request.json()
        print(f"📩 Received message: {message_data}")  # ✅ Debug

        # Push the message into Redis queue
        redis_client.lpush("message_queue", json.dumps(message_data))
        return web.json_response({"status": "success", "message": "Message received"}, status=200)
    except Exception as e:
        print(f"❌ ERROR: Failed to process message: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def start_web_server():
    """Start aiohttp web server to listen for messages from main.py"""
    app = web.Application()
    app.router.add_post("/process_message", process_message)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 5000)
    await site.start()
    print("🌐 Web server running on http://127.0.0.1:5000")

async def run_bot():
    global bot
    bot = DestinationBot(intents=discord.Intents.default())
    # Load existing webhooks into cache
    bot.webhook_cache = redis_client.hgetall("webhooks")

    # Run bot and web server concurrently
    await asyncio.gather(bot.start(BOT_TOKEN), start_web_server())

if __name__ == "__main__":
    asyncio.run(run_bot())