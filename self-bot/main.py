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
import re
from dotenv import load_dotenv
from datetime import datetime, timezone

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


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config_data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=4)


config = load_config()

TOKENS = config["tokens"]
DESTINATION_SERVERS = config.get("destination_servers", {})
EXCLUDED_CATEGORIES = set(config.get("excluded_categories", []))  # Ensure valid set
WEBHOOKS = config.get("webhooks", {})
MESSAGE_DELAY = config["settings"].get("message_delay", 0.75)  # Default to 0.75s delay
DESTINATION_BOT_URL = "http://127.0.0.1:5000/process_message"  # Change if bot.py is remote
MAX_LOGIN_ATTEMPTS = config["settings"].get("max_login_attempts", 3)  # Add this setting

# Track failed tokens
failed_tokens = set()


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


def save_user_info(token, user_info):
    """Save user info to config for the given token."""
    config = load_config()
    if token in config["tokens"]:
        config["tokens"][token]["user_info"] = {
            "id": str(user_info.id),
            "name": str(user_info),
            "discriminator": user_info.discriminator if hasattr(user_info, 'discriminator') else "0",
            "last_successful_login": datetime.now(timezone.utc).isoformat()
        }
        save_config(config)
        logging.info(f"✅ Saved user info for {user_info} to config")


def mark_token_as_failed(token, error_message):
    """Mark token as failed in config."""
    config = load_config()
    if token in config["tokens"]:
        config["tokens"][token]["status"] = "failed"
        config["tokens"][token]["last_error"] = error_message
        config["tokens"][token]["last_failed_attempt"] = datetime.now(timezone.utc).isoformat()
        save_config(config)


class MirrorSelfBot(discord.Client):
    def __init__(self, token, monitored_servers):
        super().__init__(enable_guild_compression=True)
        self.token = token
        self.monitored_servers = {str(server_id) for server_id in monitored_servers}
        self.session = aiohttp.ClientSession()  # ✅ Initialize session here
        self.login_attempts = 0
        self.max_attempts = MAX_LOGIN_ATTEMPTS

    async def on_ready(self):
        await self.fetch_guilds()
        print(f"✅ Self-bot {self.user} is now monitoring servers: {self.monitored_servers}")

        # Save user info on successful login
        save_user_info(self.token, self.user)

        # Reset login attempts on successful connection
        self.login_attempts = 0

    async def on_disconnect(self):
        logging.warning(f"🔌 Disconnected from Discord at {datetime.now(timezone.utc).isoformat()}")

    async def on_resumed(self):
        logging.info(f"🔄 Connection resumed with Discord at {datetime.now(timezone.utc).isoformat()}")

    def is_time_or_date_based(self, name):
        clean_name = re.sub(r'[^\w\s:-]', '', name.lower())

        date_match = re.search(r'\b(\d{1,2})[-/](\d{1,2})\b', clean_name)
        time_match = re.search(r'\b(\d{1,2})(am|pm)\b', clean_name)

        return bool(date_match or time_match)

    async def send_channel_delete(self, channel):
        server_real_name = self.get_server_real_name(channel.guild)
        channel_real_name = self.clean_channel_name(channel.name)

        data = {
            "action": "delete_channel",
            "server_real_name": server_real_name,
            "channel_real_name": channel_real_name
        }
        await self.send_to_destination(data)

    async def monitor_deleted_channels(self):
        await self.wait_until_ready()
        logging.info("🛡️ Starting deleted channel monitor (only time/date based channels)")

        # Build a dict to track monitored channels: {channel_id: channel_object}
        monitored_channels = {}

        # Initial population
        for guild in self.guilds:
            for channel in guild.text_channels:
                if self.is_time_or_date_based(channel.name):
                    monitored_channels[channel.id] = channel

        while True:
            try:
                # Rebuild monitored_channels continuously
                for guild in self.guilds:
                    live_channels = await guild.fetch_channels()
                    live_channel_ids = {c.id for c in live_channels if isinstance(c, discord.TextChannel)}

                    # Add new channels if they match time/date format
                    for channel in live_channels:
                        if isinstance(channel, discord.TextChannel):
                            if self.is_time_or_date_based(channel.name) and channel.id not in monitored_channels:
                                monitored_channels[channel.id] = channel
                                logging.info(
                                    f"➕ Now monitoring new time/date channel: {channel.name} (ID: {channel.id})")

                # Check if any monitored channel has been deleted
                for channel_id, channel in list(monitored_channels.items()):
                    guild = channel.guild
                    live_channels = await guild.fetch_channels()
                    live_channel_ids = {c.id for c in live_channels if isinstance(c, discord.TextChannel)}

                    if channel_id not in live_channel_ids:
                        logging.info(f"🗑️ Detected deletion of {channel.name} (ID {channel.id}). Notifying bot.py...")
                        await self.send_channel_delete(channel)
                        monitored_channels.pop(channel_id, None)

            except Exception as e:
                logging.error(f"⚠️ Error in monitor_deleted_channels: {e}")

            await asyncio.sleep(10)  # Recheck every 10 seconds

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

        logging.info(f"✅ ACCEPTED: Message from {server_name} (ID: {server_id}) in #{message.channel.name}")

        # Map all roles mentioned in the message (id → name)
        role_mentions = {}
        for role in message.role_mentions:
            role_mentions[str(role.id)] = role.name

        reply_to = None
        if message.reference and isinstance(message.reference.resolved, discord.Message):
            reply_to = (message.reference.resolved.author.nick or str(message.reference.resolved.author)).replace("#0",
                                                                                                                  "") \
                if hasattr(message.reference.resolved.author, "nick") else str(
                message.reference.resolved.author).replace("#0", "")

        if message.reference and isinstance(message.reference.resolved, discord.Message):
            original_msg = message.reference.resolved
            reply_to = original_msg.author.display_name
            reply_text = original_msg.clean_content[:180]  # clip to 180 chars
        else:
            reply_to = None
            reply_text = None

        forwarded_from = None
        forwarded_embeds = []

        if (
                not message.embeds
                and not message.attachments
                and message.reference
                and isinstance(message.reference.resolved, discord.Message)
        ):
            ref_msg = message.reference.resolved
            if ref_msg and ref_msg.embeds and ref_msg.author.bot:
                forwarded_from = ref_msg.author.display_name or str(ref_msg.author)
                forwarded_embeds = [self.format_embed(embed) for embed in ref_msg.embeds]

        message_data = {
            "reply_to": reply_to,
            "reply_text": reply_text,
            "channel_real_name": message.channel.name,
            "server_real_name": server.name,
            "mentioned_roles": role_mentions,
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "channel_name": message.channel.name,
            "category_name": message.channel.category.name if message.channel.category else "uncategorized",
            "server_id": str(server_id),
            "server_name": server_name,
            "content": message.content,
            "author_id": str(message.author.id),
            "author_name": (getattr(message.author, "nick", None) or str(message.author)).replace("#0", ""),
            "author_avatar": message.author.avatar.url if message.author.avatar else None,
            "timestamp": str(message.created_at),
            "attachments": [attachment.url for attachment in message.attachments],
            "forwarded_from": forwarded_from,
            "embeds": (
                [self.format_embed(embed) for embed in message.embeds]
                if message.embeds else
                [{"image": {"url": message.attachments[0].url}}] if message.attachments else []
            ),
        }
        logging.debug(f"Queued: {message.id} from {server_name}#{message.channel.name}")

        try:
            redis_client.lpush("message_queue", json.dumps(message_data))
            logging.info(f"✅ QUEUED to Redis: message_id={message.id}")
            log_message(f"📩 Pushed message from {message.author} in #{message.channel.name} to Redis.")
        except Exception as e:
            print(f"❌ ERROR: Failed to push message to Redis: {e}")

        # ✅ Send to bot.py
        await self.send_to_destination(message_data)

    def format_embed(self, embed):
        return {
            "title": embed.title or None,
            "description": embed.description or None,
            "url": embed.url or None,
            "color": embed.color.value if embed.color else None,
            "fields": [{"name": field.name, "value": field.value} for field in embed.fields] if embed.fields else [],
            "image": {"url": embed.image.url} if embed.image else None,
            "thumbnail": {"url": embed.thumbnail.url} if embed.thumbnail else None,
            "footer": {"text": embed.footer.text} if embed.footer else None,
            "author": {"name": embed.author.name} if embed.author else None,
        }

    async def send_to_destination(self, message_data, retries=3):
        """Send the message to bot.py and print debug output."""
        if not self.session:
            print("⚠️ ERROR: aiohttp session is not initialized!")
            self.session = aiohttp.ClientSession()

        attempt = 0
        while True:
            try:
                async with self.session.post(DESTINATION_BOT_URL, json=message_data) as response:
                    if response.status == 200:
                        return
                    else:
                        text = await response.text()
                        logging.error(f"❌ ERROR: Failed to send message ({response.status}) → {text}")
                        await asyncio.sleep(5)
            except aiohttp.ClientConnectionError as e:
                if attempt == 0:
                    print("🔌 Connection to destination bot failed. Will keep retrying silently...")
                attempt += 1
                await asyncio.sleep(10)  # Wait longer before retrying
            except Exception as e:
                logging.error(f"❌ Unexpected error in send_to_destination: {e}")
                await asyncio.sleep(10)

    async def close(self):
        if self.session:
            await self.session.close()  # Ensure session is properly closed


async def start_self_bots():
    for token, token_data in TOKENS.items():
        if token_data.get("disabled", False):
            logging.warning(f"⛔ Token disabled: {token[:10]}... Skipping.")
            continue

        # Skip if token has failed status
        if token_data.get("status") == "failed":
            user_info = token_data.get("user_info", {})
            username = user_info.get("name", "Unknown User")
            logging.warning(f"⛔ Token for {username} marked as failed: {token[:10]}... Skipping.")
            print(f"⛔ Skipping failed token for user: {username}")
            continue

        server_ids = set(token_data["servers"].keys())
        print(f"🔹 Loading bot with token {token[:10]}... Monitoring servers: {server_ids}")

        bot = MirrorSelfBot(token, server_ids)

        async def try_start_bot(bot_instance, token):
            delay = 5
            attempts = 0
            max_attempts = bot_instance.max_attempts

            while attempts < max_attempts:
                try:
                    attempts += 1
                    print(f"🔄 Login attempt {attempts}/{max_attempts} for token {token[:10]}...")
                    await bot_instance.start(token)
                except discord.LoginFailure as e:
                    error_msg = str(e)
                    if "improper token" in error_msg.lower() or "401" in error_msg:
                        print(f"❌ Invalid token detected: {token[:10]}... Stopping login attempts.")
                        logging.error(f"❌ Invalid token: {token[:10]}... Error: {e}")
                        mark_token_as_failed(token, error_msg)
                        failed_tokens.add(token)

                        # Try to get user info from config if available
                        token_data = TOKENS.get(token, {})
                        user_info = token_data.get("user_info", {})
                        if user_info:
                            print(f"ℹ️ This token belongs to user: {user_info.get('name', 'Unknown')}")
                        else:
                            print(f"ℹ️ No user info available for this token. Fix the token in config.json")
                        break
                except Exception as e:
                    logging.error(f"❌ Bot crashed (attempt {attempts}/{max_attempts}). Error: {e}")
                    if attempts < max_attempts:
                        print(f"⏳ Reconnecting in {delay} seconds...")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 60)  # cap backoff at 60 seconds
                    else:
                        print(f"❌ Max login attempts reached for token {token[:10]}... Stopping.")
                        mark_token_as_failed(token, f"Max attempts reached: {str(e)}")
                        failed_tokens.add(token)
                        break

        asyncio.create_task(try_start_bot(bot, token))
        asyncio.create_task(bot.monitor_deleted_channels())


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

    # First, add the max_login_attempts setting if it doesn't exist
    if "max_login_attempts" not in config.get("settings", {}):
        config["settings"]["max_login_attempts"] = 3
        save_config(config)

    enabled_tokens = [(t, d) for t, d in TOKENS.items() if not d.get("disabled", False) and d.get("status") != "failed"]

    if not enabled_tokens:
        print("❌ No valid tokens found. Please check your config.json")
        return

    for index, (token, token_data) in enumerate(enabled_tokens):
        server_ids = set(token_data["servers"].keys())

        # Show user info if available
        user_info = token_data.get("user_info", {})
        if user_info:
            print(
                f"🔹 Loading bot for user {user_info.get('name', 'Unknown')} with token {token[:10]}... Monitoring servers: {server_ids}")
        else:
            print(f"🔹 Loading bot with token {token[:10]}... Monitoring servers: {server_ids}")

        bot = MirrorSelfBot(token, server_ids)
        bot_instances.append(bot)

        async def try_start_bot(bot_instance, token):
            delay = 5
            attempts = 0
            max_attempts = bot_instance.max_attempts

            while attempts < max_attempts:
                try:
                    attempts += 1
                    print(f"🔄 Login attempt {attempts}/{max_attempts} for token {token[:10]}...")
                    await bot_instance.start(token)
                except discord.LoginFailure as e:
                    error_msg = str(e)
                    if "improper token" in error_msg.lower() or "401" in error_msg:
                        print(f"❌ Invalid token detected: {token[:10]}... Stopping login attempts.")
                        logging.error(f"❌ Invalid token: {token[:10]}... Error: {e}")
                        mark_token_as_failed(token, error_msg)

                        # Try to get user info from config if available
                        token_data = TOKENS.get(token, {})
                        user_info = token_data.get("user_info", {})
                        if user_info:
                            print(f"ℹ️ This token belongs to user: {user_info.get('name', 'Unknown')}")
                        else:
                            print(f"ℹ️ No user info available for this token. Fix the token in config.json")
                        break
                except Exception as e:
                    logging.error(f"❌ Bot crashed (attempt {attempts}/{max_attempts}). Error: {e}")
                    if attempts < max_attempts:
                        print(f"⏳ Reconnecting in {delay} seconds...")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 60)
                    else:
                        print(f"❌ Max login attempts reached for token {token[:10]}... Stopping.")
                        mark_token_as_failed(token, f"Max attempts reached: {str(e)}")
                        break

        # ⏳ Stagger bot launches by 5 seconds each
        await asyncio.sleep(index * 5)
        asyncio.create_task(try_start_bot(bot, token))

    print("⏳ Waiting for self-bots to finish login...")

    # Show summary of failed tokens at the end
    if failed_tokens:
        print("\n⚠️ Summary of failed tokens:")
        for token in failed_tokens:
            token_data = TOKENS.get(token, {})
            user_info = token_data.get("user_info", {})
            username = user_info.get("name", "Unknown User")
            print(f"  - Token {token[:10]}... for user: {username}")
        print("\nPlease update these tokens in config.json\n")

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