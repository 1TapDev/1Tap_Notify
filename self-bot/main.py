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
import threading
from dotenv import load_dotenv
from datetime import datetime, timezone
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

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

# Global variables to track active bots for dynamic reloading
active_bots = {}  # Dictionary to store active bot instances
config_lock = threading.Lock()  # Thread lock for config updates
config_reload_flag = False  # Flag to signal config reload needed
server_name_cache = {}  # Cache for server names to avoid repeated API calls

class ConfigFileHandler(FileSystemEventHandler):
    """Handle config.json file changes and reload configuration dynamically."""
    
    def __init__(self):
        super().__init__()
        self.last_modified = 0
    
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('config.json'):
            # Debounce file changes (avoid multiple triggers)
            current_time = datetime.now().timestamp()
            if current_time - self.last_modified < 1:  # 1 second debounce
                return
            self.last_modified = current_time
            
            print("\n📝 Config file changed - reloading configuration...")
            # Set flag for main loop to handle config reload
            global config_reload_flag
            config_reload_flag = True

async def reload_config_dynamically():
    """Reload config and update active bots with new exclusions."""
    global TOKENS, EXCLUDED_CATEGORIES, MESSAGE_DELAY, MAX_LOGIN_ATTEMPTS, server_name_cache
    
    try:
        with config_lock:
            # Clear server name cache to refresh names
            server_name_cache.clear()
            
            # Reload config from file
            new_config = load_config()
            
            # Update global variables
            TOKENS = new_config["tokens"]
            EXCLUDED_CATEGORIES = set(new_config.get("excluded_categories", []))
            MESSAGE_DELAY = new_config["settings"].get("message_delay", 0.75)
            MAX_LOGIN_ATTEMPTS = new_config["settings"].get("max_login_attempts", 3)
            
            # Update monitored servers for all active bots
            new_monitored_servers = get_monitored_server_ids()
            
            for token, bot_instance in active_bots.items():
                if hasattr(bot_instance, 'monitored_servers'):
                    # Update the bot's monitored servers
                    bot_instance.monitored_servers = {str(server_id) for server_id in new_monitored_servers}
                    print(f"✅ Updated monitoring config for bot {token[:8]}***")
            
            print("🔄 Configuration reloaded successfully!")
            print(f"📊 Monitoring {len(new_monitored_servers)} servers")
            print(f"🚫 Excluding {len(EXCLUDED_CATEGORIES)} global categories")
            
            # Log excluded channels per server
            for token_data in TOKENS.values():
                if "servers" in token_data:
                    for server_id, server_config in token_data["servers"].items():
                        excluded_channels = server_config.get("excluded_channels", [])
                        excluded_categories = server_config.get("excluded_categories", [])
                        if excluded_channels or excluded_categories:
                            server_name = get_server_info(server_id)
                            print(f"🔒 {server_name}: {len(excluded_channels)} excluded channels, {len(excluded_categories)} excluded categories")
            
    except Exception as e:
        print(f"❌ Failed to reload config: {e}")
        logging.error(f"Config reload error: {e}")

def start_config_watcher():
    """Start watching config.json for changes."""
    event_handler = ConfigFileHandler()
    observer = Observer()
    observer.schedule(event_handler, path='.', recursive=False)
    observer.start()
    print("👀 Started watching config.json for changes...")
    return observer

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
    """Retrieve human-readable server name from active bot instances or config.json."""
    server_id_str = str(server_id)
    
    # Check cache first
    if server_id_str in server_name_cache:
        return server_name_cache[server_id_str]
    
    server_name = None
    
    # First, try to get server name from active bot instances
    try:
        for bot_instance in active_bots.values():
            if hasattr(bot_instance, 'guilds') and bot_instance.guilds:
                for guild in bot_instance.guilds:
                    if str(guild.id) == server_id_str:
                        server_name = guild.name
                        break
            if server_name:
                break
    except Exception as e:
        # Silently handle any errors accessing bot instances
        pass
    
    # Fallback to config.json destination servers info
    if not server_name:
        server_info = DESTINATION_SERVERS.get(server_id_str, {}).get("info")
        if server_info:
            server_name = server_info
    
    # Final fallback
    if not server_name:
        server_name = f"Unknown Server ({server_id})"
    
    # Cache the result for future use
    server_name_cache[server_id_str] = server_name
    return server_name


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


def is_dm_mirroring_enabled(token):
    """Check if DM mirroring is enabled for this token."""
    token_data = TOKENS.get(token, {})
    dm_config = token_data.get("dm_mirroring", {})
    return dm_config.get("enabled", False)


def get_dm_destination_server(token):
    """Get the destination server ID for DM mirroring."""
    token_data = TOKENS.get(token, {})
    dm_config = token_data.get("dm_mirroring", {})
    return dm_config.get("destination_server_id")


def normalize_username_for_channel(username):
    """Normalize a username to be safe for Discord channel names."""
    # Remove emojis, special characters, and normalize
    cleaned = re.sub(r'[^\w\s\-_]', '', username)
    # Replace spaces with hyphens and convert to lowercase
    cleaned = cleaned.replace(' ', '-').lower()
    # Remove multiple consecutive hyphens
    cleaned = re.sub(r'-+', '-', cleaned)
    # Remove underscores and replace with hyphens
    cleaned = cleaned.replace('_', '-')
    # Ensure it starts and ends with alphanumeric
    cleaned = cleaned.strip('-_')
    # Ensure it's not empty
    if not cleaned:
        cleaned = "unknown-user"
    return cleaned


def find_token_for_user(target_user_id):
    """Find which token corresponds to a specific user ID."""
    for token, token_data in TOKENS.items():
        user_info = token_data.get("user_info", {})
        if user_info.get("id") == str(target_user_id):
            return token
    return None


def find_token_by_username(username):
    """Find token by username (fallback method)."""
    for token, token_data in TOKENS.items():
        user_info = token_data.get("user_info", {})
        if user_info.get("name", "").lower() == username.lower():
            return token
    return None


def get_user_display_name(user):
    """Get the best display name for a user."""
    # Priority: global_name > display_name > username
    if hasattr(user, 'global_name') and user.global_name:
        return user.global_name
    elif hasattr(user, 'display_name') and user.display_name:
        return user.display_name
    else:
        return str(user).replace("#0", "")


def is_spam_dm(message):
    """Check if a DM message appears to be spam."""
    content = message.content.lower()

    # Common spam indicators
    spam_keywords = [
        "free", "money", "profit", "trading", "investment", "crypto", "bitcoin",
        "earn", "daily", "guaranteed", "risk-free", "expert", "forex", "stocks",
        "options", "mutual server", "click", "link", "http", "www", ".com",
        "discord.gg", "join", "server", "community", "telegram", "@everyone",
        "nitro", "gift", "giveaway", "winner", "congratulations", "claim",
        "verify", "account", "suspended", "banned", "appeal", "support",
        "official", "staff", "admin", "moderator", "team discord"
    ]

    # Check for spam keywords
    spam_count = sum(1 for keyword in spam_keywords if keyword in content)
    if spam_count >= 2:  # If 2 or more spam keywords
        return True

    # Check for excessive links
    if content.count("http") > 1 or content.count(".com") > 1:
        return True

    # Check for excessive emojis (spam often has many emojis)
    emoji_count = len([char for char in content if ord(char) > 127])
    if emoji_count > 10:
        return True

    # Check message length patterns
    if len(content) > 500:  # Very long messages are often spam
        return True

    return False


def is_friend_request_dm(message):
    """Check if this is a friend request related DM."""
    # If the user is not in any mutual servers and sends a DM, it's likely a friend request
    if not message.author.mutual_guilds:
        return True

    # Check if user has very few mutual servers (likely spam)
    if len(message.author.mutual_guilds) < 2:
        return True

    return False


def should_allow_dm(message):
    """Determine if a DM should be allowed through the filter."""
    # Always allow DMs from users in monitored servers
    for guild in message.author.mutual_guilds:
        if str(guild.id) in get_monitored_server_ids():
            return True

    # Block spam messages
    if is_spam_dm(message):
        logging.info(f"🚫 Blocked spam DM from {message.author}: {message.content[:50]}...")
        return False

    # Block friend request DMs (no mutual servers)
    if is_friend_request_dm(message):
        logging.info(f"🚫 Blocked friend request DM from {message.author}: {message.content[:50]}...")
        return False

    return True


def get_monitored_server_ids():
    """Get all server IDs that are being monitored."""
    monitored_servers = set()
    for token_data in TOKENS.values():
        if "servers" in token_data:
            monitored_servers.update(token_data["servers"].keys())
    return monitored_servers


def is_allowed_bot(user):
    """Check if this bot is allowed to send DMs."""
    # List of allowed bot IDs or names
    allowed_bots = [
        "zebra check",
        "divine monitor",
        "divine",
        "hidden clearance bot",
        "monitor",
        "ticket tool",
        "notification",
        "alert",
        "checker",
        "1tap",
        "sneaker",
        "cook"
    ]

    # Check if bot name contains allowed keywords
    bot_name = user.display_name.lower() if user.display_name else str(user).lower()
    for allowed in allowed_bots:
        if allowed in bot_name:
            return True

    return False


class MirrorSelfBot(discord.Client):
    def __init__(self, token, monitored_servers):
        super().__init__(enable_guild_compression=True)
        self.token = token
        self.monitored_servers = {str(server_id) for server_id in monitored_servers}
        self.session = aiohttp.ClientSession()
        self.login_attempts = 0
        self.max_attempts = MAX_LOGIN_ATTEMPTS
        # Track destination bot's user ID to prevent echo loops
        self.destination_bot_id = None

    async def on_ready(self):
        await self.fetch_guilds()
        print(f"✅ Self-bot {self.user} is running!")

        # Save user info on successful login
        save_user_info(self.token, self.user)

        # Reset login attempts on successful connection
        self.login_attempts = 0

        # Try to get destination bot's user ID to prevent echo loops
        await self.get_destination_bot_id()

    async def get_destination_bot_id(self):
        """Fetch the destination bot's user ID to prevent echo loops."""
        try:
            destination_server_id = get_dm_destination_server(self.token)
            if destination_server_id:
                guild = discord.utils.get(self.guilds, id=int(destination_server_id))
                if guild:
                    # Look for the bot that owns the webhooks
                    for member in guild.members:
                        if member.bot and "1tap" in member.name.lower():
                            self.destination_bot_id = member.id
                            logging.info(f"🤖 Found destination bot ID: {self.destination_bot_id}")
                            break
        except Exception as e:
            logging.warning(f"⚠️ Could not determine destination bot ID: {e}")

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

        # Skip messages from the destination bot to prevent echo loops
        if self.destination_bot_id and message.author.id == self.destination_bot_id:
            return

        # Handle DM messages
        if isinstance(message.channel, discord.DMChannel):
            return await self.handle_dm_message(message)

        # Handle guild messages (existing logic)
        if not message.guild:
            return  # Ignore other non-guild messages

        # Skip "posted by" bot messages with attachments (these are usually automated reposts)
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

        # Check excluded categories and channels
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

        # Handle reply information
        reply_to = None
        reply_text = None
        if message.reference and isinstance(message.reference.resolved, discord.Message):
            original_msg = message.reference.resolved
            reply_to = original_msg.author.display_name
            reply_text = original_msg.clean_content[:180]  # clip to 180 chars

        # CORRECTED FORWARDED MESSAGE DETECTION
        # Only detect forwards when a user manually forwards a message within Discord
        forwarded_from = None
        forwarded_embeds = []
        forwarded_attachments = []

        # Method 1: Discord's native message forwarding detection
        # This happens when someone right-clicks a message and selects "Forward"
        if (
                hasattr(message, 'message_reference')
                and message.message_reference
                and message.message_reference.guild_id != message.guild.id  # Different guild
                and message.reference
                and isinstance(message.reference.resolved, discord.Message)
        ):
            ref_msg = message.reference.resolved
            forwarded_from = ref_msg.author.display_name or str(ref_msg.author)
            forwarded_embeds = [self.format_embed(embed) for embed in ref_msg.embeds]
            forwarded_attachments = [attachment.url for attachment in ref_msg.attachments]
            logging.info(f"🔄 Detected cross-guild forwarded message from {forwarded_from}")

        # Method 2: Empty message that quotes another message (manual forwarding)
        elif (
                not message.content.strip()  # Empty content
                and not message.embeds  # No embeds
                and not message.attachments  # No attachments
                and message.reference  # References another message
                and isinstance(message.reference.resolved, discord.Message)
                and not message.author.bot  # User, not bot
        ):
            ref_msg = message.reference.resolved
            # Only consider it forwarded if the referenced message has content/embeds/attachments
            if ref_msg and (ref_msg.embeds or ref_msg.attachments or ref_msg.content.strip()):
                forwarded_from = ref_msg.author.display_name or str(ref_msg.author)
                forwarded_embeds = [self.format_embed(embed) for embed in ref_msg.embeds]
                forwarded_attachments = [attachment.url for attachment in ref_msg.attachments]
                logging.info(f"🔄 Detected manual forwarded message from {forwarded_from}")

        # Method 3: Detect when someone manually types "forwarded from" or similar
        elif message.content and any(
                keyword in message.content.lower() for keyword in ["forwarded from", "originally from"]):
            import re
            forward_pattern = r"(?:forwarded from|originally from)\s*[@:]?\s*([^\n\r]+)"
            match = re.search(forward_pattern, message.content, re.IGNORECASE)
            if match:
                forwarded_from = match.group(1).strip()
                logging.info(f"🔄 Detected text-indicated forwarded message from {forwarded_from}")

        # DON'T detect cross-posting or application_id as forwarding - these are different features

        # Prepare message data
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
                forwarded_embeds if forwarded_embeds else  # Use forwarded embeds if available
                [self.format_embed(embed) for embed in message.embeds]
                if message.embeds else
                [{"image": {"url": message.attachments[0].url}}] if message.attachments else []
            ),
            "forwarded_attachments": forwarded_attachments,
            "is_forwarded": bool(forwarded_from),
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

    async def handle_dm_message(self, message):
        """Handle DM messages for mirroring."""
        # Skip if DM mirroring is not enabled for this token
        if not is_dm_mirroring_enabled(self.token):
            return

        # Skip messages from self
        if message.author.id == self.user.id:
            return

        # Handle bot messages separately
        if message.author.bot:
            # Only allow specific bots
            if not is_allowed_bot(message.author):
                logging.info(f"🚫 Blocked DM from unauthorized bot: {message.author}")
                return
            else:
                logging.info(f"✅ Allowing DM from authorized bot: {message.author}")
        else:
            # For non-bot users, apply spam/friend request filters
            if not should_allow_dm(message):
                return

        destination_server_id = get_dm_destination_server(self.token)
        if not destination_server_id:
            logging.warning(
                f"⚠️ DM mirroring enabled but no destination server configured for token {self.token[:10]}...")
            return

        # Get proper display names
        author_display_name = get_user_display_name(message.author)
        self_display_name = get_user_display_name(self.user)

        logging.info(
            f"📨 Processing DM from {author_display_name} (ID: {message.author.id}) to {self_display_name} (ID: {self.user.id})")

        # Create DM message data with correct token mapping
        message_data = {
            "message_type": "dm",
            "reply_to": None,
            "reply_text": None,
            "channel_real_name": f"dm-{normalize_username_for_channel(author_display_name)}",
            "server_real_name": f"@{self_display_name} [DM]",
            "mentioned_roles": {},
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "channel_name": f"dm-{normalize_username_for_channel(author_display_name)}",
            "category_name": f"@{self_display_name} [DM]",
            "server_id": "dm",
            "server_name": f"@{self_display_name} [DM]",
            "content": message.content,
            "author_id": str(message.author.id),
            "author_name": author_display_name,
            "author_avatar": message.author.avatar.url if message.author.avatar else None,
            "timestamp": str(message.created_at),
            "attachments": [attachment.url for attachment in message.attachments],
            "forwarded_from": None,
            "embeds": (
                [self.format_embed(embed) for embed in message.embeds]
                if message.embeds else
                [{"image": {"url": message.attachments[0].url}}] if message.attachments else []
            ),
            "destination_server_id": destination_server_id,
            "dm_user_id": str(message.author.id),
            "dm_username": author_display_name,
            "self_user_id": str(self.user.id),
            "self_username": self_display_name,
            "receiving_token": self.token,  # Token of the person receiving the DM
            "sender_user_id": str(message.author.id),  # ID of the person sending the DM
            "is_bot": message.author.bot,  # Add bot flag
            "bot_name": str(message.author) if message.author.bot else None  # Bot name for reference
        }

        try:
            redis_client.lpush("message_queue", json.dumps(message_data))
            logging.info(f"✅ QUEUED DM to Redis: message_id={message.id} from {author_display_name}")
            log_message(f"📨 Pushed DM from {author_display_name} to Redis.")
        except Exception as e:
            logging.error(f"❌ ERROR: Failed to push DM to Redis: {e}")

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

    async def send_dm_to_user(self, user_id, content):
        """Send a DM to a specific user using this bot's token."""
        try:
            user = await self.fetch_user(int(user_id))
            if user:
                await user.send(content)
                logging.info(f"✅ Sent DM to {user}: {content[:50]}...")
                return True
            else:
                logging.error(f"❌ Could not find user with ID {user_id}")
                return False
        except discord.Forbidden as e:
            logging.error(f"❌ Cannot send DM to user {user_id}: {e}")
            return False
        except Exception as e:
            logging.error(f"❌ Failed to send DM to user {user_id}: {e}")
            return False

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


# Global storage for bot instances to enable DM relay functionality
bot_instances = {}


async def send_dm_via_token(token, user_id, content):
    """Send a DM using a specific token's bot instance."""
    try:
        logging.info(f"🔍 Looking for bot instance with token {token[:10]}...")
        logging.info(f"🔍 Available bot instances: {list(bot_instances.keys())[:3]}...")  # Show first 3 tokens

        if token in bot_instances:
            bot_instance = bot_instances[token]
            logging.info(f"✅ Found bot instance for token {token[:10]}...")

            success = await bot_instance.send_dm_to_user(user_id, content)
            if success:
                logging.info(f"✅ DM sent successfully via token {token[:10]}... to user {user_id}")
            else:
                logging.error(f"❌ Failed to send DM via token {token[:10]}... to user {user_id}")
            return success
        else:
            logging.error(f"❌ Bot instance not found for token {token[:10]}...")
            logging.error(f"❌ Available tokens: {[t[:10] + '...' for t in bot_instances.keys()]}")
            return False
    except Exception as e:
        logging.error(f"❌ Exception in send_dm_via_token: {e}")
        return False


async def share_bot_instances():
    """Share bot instances with bot.py for channel blocking."""
    try:
        instance_data = {}
        for token, bot_instance in bot_instances.items():
            if bot_instance.user:
                instance_data[token] = {
                    "user_id": str(bot_instance.user.id),
                    "username": str(bot_instance.user),
                    "guilds": [str(guild.id) for guild in bot_instance.guilds]
                }

        redis_client.set("bot_instances", json.dumps(instance_data))
        logging.info("✅ Shared bot instance data with bot.py")
    except Exception as e:
        logging.error(f"❌ Failed to share bot instances: {e}")


async def share_bot_instances_periodically():
    """Periodically share bot instance data."""
    await asyncio.sleep(60)  # Wait for bots to be ready
    while True:
        await share_bot_instances()
        await asyncio.sleep(30)  # Update every 30 seconds


async def start_dm_relay_service():
    """Start a web service to handle DM relay requests from bot.py."""
    from aiohttp import web

    async def handle_dm_relay(request):
        """Handle DM relay requests."""
        try:
            data = await request.json()
            action = data.get("action")

            logging.info(f"📨 Received DM relay request: {data}")

            if action == "send_dm":
                token = data.get("token")
                user_id = data.get("user_id")
                content = data.get("content", "")
                attachments = data.get("attachments", [])

                logging.info(
                    f"📤 Processing DM relay: token={token[:10]}... user_id={user_id} content='{content[:50]}...'")

                # Check if bot instance exists
                if token not in bot_instances:
                    error_msg = f"Bot instance not found for token {token[:10]}..."
                    logging.error(f"❌ {error_msg}")
                    return web.json_response({"status": "error", "message": error_msg}, status=404)

                # Send DM via the appropriate bot instance
                success = await send_dm_via_token(token, user_id, content)

                if success:
                    logging.info(f"✅ DM relay successful to user {user_id}")
                    return web.json_response({"status": "success"}, status=200)
                else:
                    error_msg = f"Failed to send DM to user {user_id}"
                    logging.error(f"❌ {error_msg}")
                    return web.json_response({"status": "error", "message": error_msg}, status=500)
            elif action == "request_sync":
                # Handle sync request from bot.py
                logging.info("📤 Received server sync request from bot.py")
                await sync_server_info_to_bot()
                return web.json_response({"status": "success", "message": "Server sync triggered"}, status=200)
            else:
                error_msg = f"Unknown action: {action}"
                logging.error(f"❌ {error_msg}")
                return web.json_response({"status": "error", "message": error_msg}, status=400)

        except Exception as e:
            error_msg = f"Error in DM relay service: {str(e)}"
            logging.error(f"❌ {error_msg}")
            return web.json_response({"status": "error", "message": error_msg}, status=500)

    app = web.Application()
    app.router.add_post("/send_dm", handle_dm_relay)
    app.router.add_post("/request_sync", handle_dm_relay)  # Add sync endpoint
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 5001)
    await site.start()
    logging.info("✅ DM relay service started on port 5001")
    print("📡 DM relay service running on http://127.0.0.1:5001")


async def process_dm_relay_queue():
    """Process DM relay requests from Redis queue."""
    while True:
        try:
            # Check for DM relay requests
            relay_data = redis_client.rpop("dm_relay_queue")
            if relay_data:
                try:
                    relay_request = json.loads(relay_data)
                    token = relay_request.get("token")
                    user_id = relay_request.get("user_id")
                    content = relay_request.get("content", "")

                    # Send the DM
                    success = await send_dm_via_token(token, user_id, content)
                    if success:
                        logging.info(f"✅ DM relay successful to user {user_id}")
                    else:
                        logging.error(f"❌ DM relay failed to user {user_id}")

                except json.JSONDecodeError:
                    logging.error(f"❌ Invalid JSON in DM relay queue: {relay_data}")
                except Exception as e:
                    logging.error(f"❌ Error processing DM relay: {e}")

            await asyncio.sleep(1)

        except Exception as e:
            logging.error(f"❌ Error in DM relay queue processor: {e}")
            await asyncio.sleep(5)


# Start listening for requests
async def main():
    global bot_instances, active_bots

    # Start config file watcher
    config_observer = start_config_watcher()

    # First, add the max_login_attempts setting if it doesn't exist
    if "max_login_attempts" not in config.get("settings", {}):
        config["settings"]["max_login_attempts"] = 3
        save_config(config)

    # Initialize dm_mappings if it doesn't exist
    if "dm_mappings" not in config:
        config["dm_mappings"] = {}
        save_config(config)

    enabled_tokens = [(t, d) for t, d in TOKENS.items() if not d.get("disabled", False) and d.get("status") != "failed"]

    if not enabled_tokens:
        print("❌ No valid tokens found. Please check your config.json")
        return

    # Start DM relay service
    asyncio.create_task(start_dm_relay_service())
    asyncio.create_task(process_dm_relay_queue())

    for index, (token, token_data) in enumerate(enabled_tokens):
        server_ids = set(token_data["servers"].keys())

        # Show user info if available
        user_info = token_data.get("user_info", {})
        if user_info:
            print(
                f"🔹 Loading bot for user {user_info.get('name', 'Unknown')} with token {token[:10]}... Monitoring servers: {server_ids}")
        else:
            print(f"🔹 Loading bot with token {token[:10]}... Monitoring servers: {server_ids}")

        # Show DM mirroring status
        dm_config = token_data.get("dm_mirroring", {})
        if dm_config.get("enabled", False):
            dest_server = dm_config.get("destination_server_id", "Not Set")
            print(f"📨 DM mirroring enabled → Destination: {dest_server}")

        bot = MirrorSelfBot(token, server_ids)
        bot_instances[token] = bot  # Store for DM relay functionality
        active_bots[token] = bot  # Store for config reloading

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
    print("📨 DM relay service active on port 5001")
    print("🔄 Server sync service active")

    asyncio.create_task(share_bot_instances_periodically())

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
            await asyncio.sleep(5)  # Check every 5 seconds
            
            # Check if config reload is needed
            global config_reload_flag
            if config_reload_flag:
                config_reload_flag = False  # Reset flag
                await reload_config_dynamically()
    finally:
        print("👋 Shutting down...")
        config_observer.stop()
        config_observer.join()
        for bot in bot_instances.values():
            await bot.close()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())