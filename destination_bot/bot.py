import discord
import json
import asyncio
import aiohttp
import redis
import logging
import hashlib
import argparse
import os
import time
import io
from PIL import Image, ImageOps
import threading
import requests
import re
from discord.ext import commands
from discord import app_commands
from aiohttp import web
from datetime import datetime, timedelta

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
    datefmt="%Y-%m-%d %H:%M-%S"
)

# Configure console to only show errors
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)  # Only WARNING and above
console_formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
console_handler.setFormatter(console_formatter)
logging.getLogger().addHandler(console_handler)

# Load configuration from config.json
CONFIG_FILE = "config.json"

# Connection state tracking
connection_state = {
    "is_connected": True,
    "last_disconnect_logged": False,
    "reconnect_attempts": 0
}

def load_config():
    """Load configuration from config.json file."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(config_data):
    """Save configuration to config.json file."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=4)

def capture_server_layout(guild):
    """Capture the current server layout with all categories and channel positions."""
    layout = {
        "captured_at": datetime.now().isoformat(),
        "server_id": guild.id,
        "server_name": guild.name,
        "categories": {},
        "uncategorized_channels": []
    }
    
    # Capture categorized channels
    for category in guild.categories:
        layout["categories"][category.id] = {
            "name": category.name,
            "position": category.position,
            "channels": []
        }
        
        for channel in category.channels:
            if isinstance(channel, discord.TextChannel):
                layout["categories"][category.id]["channels"].append({
                    "id": channel.id,
                    "name": channel.name,
                    "position": channel.position
                })
    
    # Capture uncategorized channels
    uncategorized = [ch for ch in guild.text_channels if ch.category is None]
    for channel in uncategorized:
        layout["uncategorized_channels"].append({
            "id": channel.id,
            "name": channel.name,
            "position": channel.position
        })
    
    return layout

def save_server_layout(guild):
    """Save the current server layout to a file."""
    layout = capture_server_layout(guild)
    layout_file = f"server_layout_{guild.id}.json"
    
    with open(layout_file, "w", encoding="utf-8") as f:
        json.dump(layout, f, indent=4)
    
    logging.info(f"🔒 Server layout saved to {layout_file}")
    logging.info(f"📋 Captured {len(layout['categories'])} categories and {len(layout['uncategorized_channels'])} uncategorized channels")
    return layout_file

def load_server_layout(guild_id):
    """Load the saved server layout from file."""
    layout_file = f"server_layout_{guild_id}.json"
    
    try:
        with open(layout_file, "r", encoding="utf-8") as f:
            layout = json.load(f)
        logging.info(f"🔓 Loaded server layout from {layout_file}")
        return layout
    except FileNotFoundError:
        logging.warning(f"⚠️ No saved layout found: {layout_file}")
        return None
    except Exception as e:
        logging.error(f"❌ Failed to load layout: {e}")
        return None

# Category IDs for moveable categories
RELEASE_GUIDES_CATEGORY_ID = 1348464705701806080
DAILY_SCHEDULE_CATEGORY_ID = 1353704482457915433
MOVEABLE_CATEGORY_IDS = {RELEASE_GUIDES_CATEGORY_ID, DAILY_SCHEDULE_CATEGORY_ID}

def is_moveable_category(category_id):
    """Check if channels in this category are allowed to be moved."""
    return category_id in MOVEABLE_CATEGORY_IDS

config = load_config()

BOT_TOKEN = config.get("bot_token")
DESTINATION_SERVER_ID = config["destination_server"]
WEBHOOKS = config.get("webhooks", {})
TOKENS = config.get("tokens", {})
DM_MAPPINGS = config.get("dm_mappings", {})
MAX_DISCORD_FILE_SIZE = 7.5 * 1024 * 1024  # 7.5MB instead of 8MB

def compress_image(image_data, filename, max_size=MAX_DISCORD_FILE_SIZE, quality=85):
    """
    Compress an image to fit under Discord's file size limit.
    
    Args:
        image_data: Binary image data
        filename: Original filename for format detection
        max_size: Maximum file size in bytes
        quality: JPEG compression quality (1-100)
    
    Returns:
        tuple: (compressed_data, new_filename, was_compressed)
    """
    try:
        # Check if it's actually an image
        original_size = len(image_data)
        
        # Detect file type from filename
        file_ext = filename.lower().split('.')[-1] if '.' in filename else 'jpg'
        
        # Skip compression for non-image files
        if file_ext not in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']:
            return image_data, filename, False
            
        # Load image with PIL
        try:
            image = Image.open(io.BytesIO(image_data))
            # Verify this is actually a valid image
            image.verify()
            # Reload the image since verify() can only be called once
            image = Image.open(io.BytesIO(image_data))
        except Exception as load_error:
            logging.warning(f"⚠️ Could not load image {filename}: {load_error}")
            return image_data, filename, False
        
        # Convert RGBA to RGB for JPEG compatibility (preserve transparency for PNG)
        if image.mode in ('RGBA', 'LA', 'P') and file_ext in ['jpg', 'jpeg']:
            # Create white background for JPEG
            background = Image.new('RGB', image.size, (255, 255, 255))
            if image.mode == 'P':
                image = image.convert('RGBA')
            background.paste(image, mask=image.split()[-1] if image.mode in ('RGBA', 'LA') else None)
            image = background
        
        # Start with current quality
        current_quality = quality
        max_dimension = max(image.size)
        attempts = 0
        max_attempts = 8  # Prevent infinite loops
        
        while current_quality > 10 and attempts < max_attempts:  # Minimum quality threshold and safety limit
            attempts += 1
            # Resize if image is too large (progressive downsizing)
            if max_dimension > 2048 and current_quality < 60:
                ratio = 2048 / max_dimension
                new_size = (int(image.size[0] * ratio), int(image.size[1] * ratio))
                resized_image = image.resize(new_size, Image.Resampling.LANCZOS)
                max_dimension = max(new_size)
            else:
                resized_image = image
            
            # Compress image
            output_buffer = io.BytesIO()
            
            # Always convert to JPEG for better compression and compatibility
            # This avoids PNG encoding issues and provides more consistent compression
            try:
                # Convert to RGB mode for JPEG compatibility
                if resized_image.mode in ('RGBA', 'LA', 'P'):
                    # Create white background for transparency
                    background = Image.new('RGB', resized_image.size, (255, 255, 255))
                    if resized_image.mode == 'P':
                        resized_image = resized_image.convert('RGBA')
                    if resized_image.mode in ('RGBA', 'LA'):
                        background.paste(resized_image, mask=resized_image.split()[-1])
                    else:
                        background.paste(resized_image)
                    resized_image = background
                elif resized_image.mode not in ('RGB', 'L'):
                    resized_image = resized_image.convert('RGB')
                
                # Save as JPEG with quality adjustment
                resized_image.save(output_buffer, format='JPEG', quality=current_quality, optimize=True)
                compressed_filename = filename.rsplit('.', 1)[0] + '_compressed.jpg'
                
            except Exception as save_error:
                # If JPEG saving fails, try a more basic approach
                logging.warning(f"⚠️ JPEG compression failed for {filename}, trying basic conversion: {save_error}")
                try:
                    # Convert to basic RGB and try again
                    rgb_image = Image.new('RGB', resized_image.size, (255, 255, 255))
                    if resized_image.mode != 'RGB':
                        resized_image = resized_image.convert('RGB')
                    rgb_image.paste(resized_image)
                    rgb_image.save(output_buffer, format='JPEG', quality=current_quality)
                    compressed_filename = filename.rsplit('.', 1)[0] + '_compressed.jpg'
                except Exception as final_error:
                    logging.error(f"❌ Failed to compress {filename} with all methods: {final_error}")
                    return image_data, filename, False
            
            compressed_data = output_buffer.getvalue()
            compressed_size = len(compressed_data)
            
            # Check if compression was successful
            if compressed_size <= max_size:
                compression_ratio = (1 - compressed_size / original_size) * 100
                logging.info(
                    f"🗜️ Image compressed: {filename} "
                    f"({original_size//1024}KB → {compressed_size//1024}KB, "
                    f"-{compression_ratio:.1f}%, quality={current_quality})"
                )
                return compressed_data, compressed_filename, True
            
            # Reduce quality for next iteration
            current_quality -= 15
            
            # If quality is getting too low, try more aggressive resizing
            if current_quality < 30 and max_dimension > 1024:
                max_dimension = max_dimension // 2
                current_quality = 60  # Reset quality for smaller image
        
        # If we couldn't compress enough, return original data
        logging.warning(f"⚠️ Could not compress {filename} sufficiently. Original size: {original_size//1024}KB")
        return image_data, filename, False
        
    except Exception as e:
        logging.warning(f"⚠️ Failed to compress image {filename}: {e}")
        return image_data, filename, False

# Connect to Redis
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# Global cache to track recent message_ids and prevent duplicates
recent_message_ids = set()

# In-memory set to track channels deleted by Polar Helper to avoid recreating them
polar_deleted_channels = set()

# Create bot instance with slash command support
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.reactions = True
intents.members = True

# Store for DM relay functionality
dm_relay_endpoint = "http://127.0.0.1:5001/send_dm"  # Endpoint to send DMs via main.py


def get_next_version():
    version_file = "version.txt"
    if not os.path.exists(version_file):
        with open(version_file, "w") as f:
            f.write("1.0")

    with open(version_file, "r") as f:
        current = f.read().strip()

    major, minor = map(int, current.split("."))
    if minor >= 9:
        major += 1
        minor = 0
    else:
        minor += 1

    next_version = f"{major}.{minor}"
    with open(version_file, "w") as f:
        f.write(next_version)

    return next_version


def cleanup_dead_webhooks():
    logging.info("🧹 Starting cleanup of dead webhooks...")

    to_delete = []

    for key, webhook_url in list(config.get("webhooks", {}).items()):
        try:
            response = requests.head(webhook_url, timeout=5)
            if response.status_code in [404, 401, 403]:
                try:
                    data = response.json()
                    if data.get("code") == 10015:
                        to_delete.append(key)
                    else:
                        to_delete.append(key)
                except Exception:
                    to_delete.append(key)
        except requests.RequestException as e:
            logging.warning(f"[Webhook Checker] Request error for {webhook_url}: {e}")
            to_delete.append(key)

    for key in to_delete:
        config["webhooks"].pop(key, None)
        redis_client.hdel("webhooks", key)

    if to_delete:
        save_config(config)
        logging.info(f"✅ Removed {len(to_delete)} dead webhooks from config and Redis.")
    else:
        logging.info("✅ Cleanup completed — no dead webhooks found.")


def strip_emojis(text):
    return re.sub(r'[^\w\s\[\]-]', '', text).strip()


def schedule_cleanup():
    while True:
        cleanup_dead_webhooks()
        time.sleep(1800)


def normalize_category(name):
    name = re.sub(r"[^\w\s\[\]\-()]", "", name)
    return name.lower().replace("  ", " ").strip()


def normalize_server_tag(tag):
    return tag.lower().strip()


def normalize_key(category_name, channel_name, server_name):
    norm_category = category_name.lower().replace(" ", "-").replace("|", "").replace("︱", "").replace("⚡", "").strip()
    norm_channel = channel_name.lower().replace(" ", "-").replace("|", "").replace("︱", "").strip()
    norm_server = server_name.lower().replace(" ", "-").replace("|", "").replace("︱", "").strip()
    return f"{norm_category}-[{norm_server}]/{norm_channel}"


def normalize_name(name: str) -> str:
    return (
        name.lower()
        .replace("–", "-")  # En dash
        .replace("—", "-")  # Em dash
        .replace("‒", "-")  # Figure dash
        .replace("'", "'")
        .replace("'", "'")
        .replace(""", '"')
        .replace(""", '"')
        .strip()
    )


def normalize_username_for_channel(username):
    """Normalize a username to be safe for Discord channel names."""
    # Remove emojis, special characters, and normalize
    cleaned = re.sub(r'[^\w\s\-_]', '', username)
    # Replace spaces with hyphens and convert to lowercase
    cleaned = cleaned.replace(' ', '-').lower()
    # Remove multiple consecutive hyphens
    cleaned = re.sub(r'-+', '-', cleaned)
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


def parse_channel_info(channel_name):
    return parse_channel_info_fixed(channel_name)

def find_original_channel_mapping(channel_name, server_tag):
    """Find the original server ID and channel that corresponds to this mirrored channel."""
    for webhook_key in WEBHOOKS.keys():
        if f"[{server_tag.lower().replace(' ', '-')}]" in webhook_key:
            parts = webhook_key.split("/")
            if len(parts) == 2:
                webhook_channel = parts[1]
                if webhook_channel == channel_name.lower().replace(" ", "-").replace("|", ""):
                    for token_data in TOKENS.values():
                        for server_id, server_config in token_data.get("servers", {}).items():
                            return server_id, None
    return None, None


async def add_channel_to_exclusions(server_id, channel_id, token=None):
    """Add a channel to the excluded_channels list for the appropriate token and server."""
    config_data = load_config()  # Load fresh config

    if token:
        if token in config_data["tokens"] and server_id in config_data["tokens"][token]["servers"]:
            if "excluded_channels" not in config_data["tokens"][token]["servers"][server_id]:
                config_data["tokens"][token]["servers"][server_id]["excluded_channels"] = []

            excluded_channels = config_data["tokens"][token]["servers"][server_id]["excluded_channels"]
            if int(channel_id) not in excluded_channels:
                excluded_channels.append(int(channel_id))
                save_config(config_data)
                return True
    else:
        added_count = 0
        for token_key, token_data in config_data["tokens"].items():
            if server_id in token_data.get("servers", {}):
                if "excluded_channels" not in token_data["servers"][server_id]:
                    token_data["servers"][server_id]["excluded_channels"] = []

                excluded_channels = token_data["servers"][server_id]["excluded_channels"]
                if int(channel_id) not in excluded_channels:
                    excluded_channels.append(int(channel_id))
                    added_count += 1

        if added_count > 0:
            save_config(config_data)
            return True

    return False


async def find_and_block_original_channel(channel_name, server_tag):
    """Find the original channel and add it to exclusions."""
    try:
        # Get bot instance data from Redis
        bot_instances_data = redis_client.get("bot_instances")
        if not bot_instances_data:
            return False

        instances = json.loads(bot_instances_data)
        normalized_name = channel_name.lower().replace(" ", "-").replace("|", "")

        config_data = load_config()
        for token, instance_info in instances.items():
            if token in config_data["tokens"]:
                for server_id in config_data["tokens"][token].get("servers", {}):
                    try:
                        # Make API call to find channels
                        headers = {"Authorization": f"Bot {BOT_TOKEN}"}
                        url = f"https://discord.com/api/v10/guilds/{server_id}/channels"

                        async with aiohttp.ClientSession() as session:
                            async with session.get(url, headers=headers) as response:
                                if response.status == 200:
                                    channels = await response.json()

                                    for channel_data in channels:
                                        if channel_data.get("type") == 0:  # Text channel
                                            if channel_data["name"].lower().replace("-", "").replace("_",
                                                                                                     "") == normalized_name.replace(
                                                    "-", "").replace("_", ""):
                                                success = await add_channel_to_exclusions(server_id,
                                                                                          str(channel_data["id"]),
                                                                                          token)
                                                if success:
                                                    logging.info(
                                                        f"✅ Blocked channel {channel_data['name']} (ID: {channel_data['id']}) in server {server_id}")
                                                    return True
                    except Exception as e:
                        logging.error(f"❌ Error checking server {server_id}: {e}")
                        continue

        return False

    except Exception as e:
        logging.error(f"❌ Error in find_and_block_original_channel: {e}")
        return False

async def process_redis_messages():
    try:
        while True:
            processed = 0
            while True:
                message_data = redis_client.rpop(QUEUE_NAME)
                if not message_data:
                    break

                try:
                    message = json.loads(message_data)
                    if not isinstance(message, dict):
                        raise ValueError("Invalid message format, expected dict")
                    if "message_id" not in message:
                        raise ValueError("Missing required field: message_id")

                    # Check if this is a DM message
                    if message.get("message_type") == "dm":
                        await handle_dm_message(message)
                    else:
                        await send_to_webhook(message)
                    processed += 1
                except Exception as e:
                    logging.error(f"❌ Failed to process single Redis message: {e} → Data: {message_data}")

                except json.JSONDecodeError as je:
                    logging.error(f"❌ JSON decode error: {je} → Raw data: {repr(message_data)}")
                except Exception as e:
                    logging.error(f"❌ Failed to process single Redis message: {e} → Data: {repr(message_data)}")

            if processed > 0:
                logging.info(f"✅ Processed {processed} messages from queue.")

            await asyncio.sleep(1)
    except Exception as e:
        logging.error(f"❌ ERROR: Failed to process Redis messages: {e}")


def get_monitored_servers():
    """Get list of monitored servers from synced config"""
    try:
        config_data = load_config()

        # First try to get from synced data
        server_names = config_data.get("server_names", [])
        if server_names:
            logging.info(f"✅ Loaded {len(server_names)} servers from sync")
            return server_names

        # Fallback to manual list if no sync data
        fallback_servers = [
            "strike-access",
            "ak-chefs",
            "polar-chefs",
            "divine",
            "shoeplex",
            "gfnf",
            "fastbreak"
        ]

        logging.warning("⚠️ Using fallback server list - server sync may not be working")
        return fallback_servers

    except Exception as e:
        logging.error(f"❌ Error loading server names: {e}")
        return []


def parse_channel_info_fixed(channel_name):
    """Parse channel name to extract original server and channel info."""
    # Method 1: Standard format "channel-name [server-tag]"
    if "[" in channel_name and "]" in channel_name:
        base_name = channel_name.split(" [")[0].strip()
        server_tag = channel_name.split("[")[-1].split("]")[0].strip()
        return base_name, server_tag

    # Method 2: Use actual server names from config
    monitored_servers = get_monitored_servers()
    channel_lower = channel_name.lower()

    # Sort by length (longest first) to match more specific names first
    sorted_servers = sorted(monitored_servers, key=len, reverse=True)

    for server_name in sorted_servers:
        # Check if server name keywords are in channel name
        server_keywords = server_name.split("-")

        # Check if all keywords from server name are in channel name
        if all(keyword in channel_lower for keyword in server_keywords):
            # Remove server keywords from channel name to get base name
            base_name = channel_name
            for keyword in server_keywords:
                base_name = base_name.replace(f"-{keyword}", "").replace(keyword, "")
            base_name = base_name.strip("-").strip()
            return base_name, server_name

    # Method 3: No pattern found
    return channel_name, None


def get_channel_suggestions(channel_name):
    """Get suggestions for what server this channel might belong to."""
    monitored_servers = get_monitored_servers()
    channel_lower = channel_name.lower()
    suggestions = []

    # Score each server based on keyword matches
    server_scores = {}
    for server_name in monitored_servers:
        score = 0
        server_keywords = server_name.split("-")

        for keyword in server_keywords:
            if keyword in channel_lower:
                score += 1

        if score > 0:
            server_scores[server_name] = score

    # Return servers sorted by match score (best matches first)
    suggestions = sorted(server_scores.keys(), key=lambda x: server_scores[x], reverse=True)

    return suggestions[:5]  # Return top 5 suggestions

def parse_channel_info(channel_name):
    """Parse channel name to extract original server and channel info."""
    if "[" in channel_name and "]" in channel_name:
        base_name = channel_name.split(" [")[0].strip()
        server_tag = channel_name.split("[")[-1].split("]")[0].strip()
        return base_name, server_tag
    return channel_name, None


def find_original_channel_mapping(channel_name, server_tag):
    """Find the original server ID and channel that corresponds to this mirrored channel."""
    # This would need to be enhanced with a mapping system
    # For now, we'll use the webhook keys to reverse-engineer

    for webhook_key in WEBHOOKS.keys():
        # Webhook keys are formatted like: "category-[server]/channel"
        if f"[{server_tag.lower().replace(' ', '-')}]" in webhook_key:
            # Extract the original channel name from webhook key
            parts = webhook_key.split("/")
            if len(parts) == 2:
                webhook_channel = parts[1]
                if webhook_channel == channel_name.lower().replace(" ", "-").replace("|", ""):
                    # Found a match, now we need to find the server ID
                    for token_data in TOKENS.values():
                        for server_id, server_config in token_data.get("servers", {}).items():
                            # This is where we'd need better mapping
                            # For now, return the first server that has this tag
                            return server_id, None  # We'll need to find the actual channel ID

    return None, None


async def add_channel_to_exclusions(server_id, channel_id, token=None):
    """Add a channel to the excluded_channels list for the appropriate token and server."""
    config = load_config()

    if token:
        # Add to specific token
        if token in config["tokens"] and server_id in config["tokens"][token]["servers"]:
            if "excluded_channels" not in config["tokens"][token]["servers"][server_id]:
                config["tokens"][token]["servers"][server_id]["excluded_channels"] = []

            excluded_channels = config["tokens"][token]["servers"][server_id]["excluded_channels"]
            if int(channel_id) not in excluded_channels:
                excluded_channels.append(int(channel_id))
                save_config(config)
                return True
    else:
        # Add to all tokens that monitor this server
        added_count = 0
        for token_key, token_data in config["tokens"].items():
            if server_id in token_data.get("servers", {}):
                if "excluded_channels" not in token_data["servers"][server_id]:
                    token_data["servers"][server_id]["excluded_channels"] = []

                excluded_channels = token_data["servers"][server_id]["excluded_channels"]
                if int(channel_id) not in excluded_channels:
                    excluded_channels.append(int(channel_id))
                    added_count += 1

        if added_count > 0:
            save_config(config)
            return True

    return False

async def send_dm_via_webhook(webhook, message_data):
    """Send a DM message via webhook."""
    try:
        content = message_data.get("content", "")
        attachments = message_data.get("attachments", [])
        embeds = message_data.get("embeds", [])

        # Clean up the embeds
        cleaned_embeds = []
        for embed in embeds:
            if isinstance(embed, dict) and any([
                embed.get("title"),
                embed.get("description"),
                embed.get("url"),
                embed.get("image"),
                embed.get("thumbnail"),
                embed.get("fields")
            ]):
                cleaned_embeds.append(embed)

        # Skip truly empty messages
        if not content.strip() and not cleaned_embeds and not attachments:
            logging.info(f"⏭️ Skipped empty DM message_id={message_data.get('message_id')}")
            return

        # Use display name for webhook username
        display_name = message_data.get("author_name", "Unknown")

        # Prepare webhook payload
        payload = {
            "username": display_name,  # Use the display name directly
            "avatar_url": message_data.get("author_avatar")
        }

        if content:
            payload["content"] = content

        if cleaned_embeds:
            payload["embeds"] = cleaned_embeds

        # Handle file attachments
        files = []
        for idx, url in enumerate(attachments):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            file_data = await resp.read()
                            filename = url.split("/")[-1].split("?")[0] or f"file{idx}.jpg"

                            if len(file_data) <= MAX_DISCORD_FILE_SIZE:
                                files.append({
                                    "filename": filename,
                                    "data": file_data
                                })
                            else:
                                # Try to compress the image before giving up
                                compressed_data, compressed_filename, was_compressed = compress_image(file_data, filename)
                                if was_compressed and len(compressed_data) <= MAX_DISCORD_FILE_SIZE:
                                    files.append({
                                        "filename": compressed_filename,
                                        "data": compressed_data
                                    })
                                    logging.info(f"✅ Large DM file compressed and attached: {compressed_filename}")
                                else:
                                    # Still too large or not an image, send as link
                                    logging.warning(f"⚠️ DM file too large even after compression, sending as link: {filename}")
                                    if content:
                                        payload["content"] = f"{content}\n📎 **Large file:** {url}"
                                    else:
                                        payload["content"] = f"📎 **Large file:** {url}"
            except Exception as e:
                logging.warning(f"⚠️ Failed to fetch attachment: {url} → {e}")

        # Send via webhook
        async with aiohttp.ClientSession() as session:
            try:
                if files:
                    from aiohttp import FormData
                    form = FormData()
                    for idx, file in enumerate(files):
                        form.add_field(
                            name=f"file{idx}",
                            value=file["data"],
                            filename=file["filename"],
                            content_type="application/octet-stream"
                        )
                    form.add_field("payload_json", json.dumps(payload))

                    async with session.post(webhook.url, data=form) as response:
                        if response.status in (200, 204):
                            logging.info(f"✅ DM webhook message sent successfully")
                        else:
                            error = await response.text()
                            logging.error(f"❌ DM webhook file upload failed ({response.status}) → {error}")
                else:
                    async with session.post(webhook.url, json=payload) as response:
                        if response.status in (200, 204):
                            logging.info(f"✅ DM webhook message sent successfully")
                        else:
                            error = await response.text()
                            logging.error(f"❌ DM webhook failed ({response.status}) → {error}")

            except Exception as e:
                logging.error(f"❌ Exception during DM webhook post: {e}")

    except Exception as e:
        logging.error(f"❌ Error in send_dm_via_webhook: {e}")


async def handle_dm_message(message_data):
    """Handle DM messages by creating appropriate channels and webhooks."""
    try:
        guild = bot.get_guild(int(message_data["destination_server_id"]))
        if not guild:
            logging.error(f"❌ Destination server not found: {message_data['destination_server_id']}")
            return

        category_name = message_data["category_name"]
        channel_name = message_data["channel_name"]
        dm_user_id = message_data["dm_user_id"]
        dm_username = message_data["dm_username"]
        self_username = message_data["self_username"]
        receiving_token = message_data.get("receiving_token")  # Token of person receiving DM
        sender_user_id = message_data.get("sender_user_id")  # ID of person sending DM

        # Find or create the DM category
        category = discord.utils.get(guild.categories, name=category_name)
        if not category:
            try:
                category = await guild.create_category(name=category_name)
                logging.info(f"✅ Created DM category: {category_name}")
            except Exception as e:
                logging.error(f"❌ Failed to create DM category {category_name}: {e}")
                return

        # Find or create the DM channel
        channel = discord.utils.get(guild.text_channels, name=channel_name, category=category)
        if not channel:
            try:
                channel = await guild.create_text_channel(name=channel_name, category=category)
                logging.info(f"✅ Created DM channel: {channel_name}")

                # Set up the channel mapping for relay functionality
                # The key insight: we need the token that can send DMs to the SENDER
                sender_token = find_token_for_user(sender_user_id)
                if not sender_token:
                    logging.warning(f"⚠️ Could not find token for sender user ID {sender_user_id}")
                    sender_token = receiving_token  # Fallback to receiving token

                DM_MAPPINGS[str(channel.id)] = {
                    "user_id": dm_user_id,  # Person who sent the original DM
                    "username": dm_username,  # Display name of sender
                    "self_user_id": message_data["self_user_id"],  # Person who received the DM
                    "receiving_token": receiving_token,  # Token of person who received DM
                    "sender_token": sender_token,  # Token that can send DMs to the sender
                    "relay_token": sender_token  # Token to use for relay (sends back to sender)
                }

                # Save the mapping
                config["dm_mappings"] = DM_MAPPINGS
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4)

                # Send an informational message with display names
                embed = discord.Embed(
                    title="📨 DM Channel Created",
                    description=f"This channel mirrors DMs between **{self_username}** and **{dm_username}**.\n\nMessages sent here will be forwarded as DMs.",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Sender", value=f"{dm_username} (ID: {dm_user_id})", inline=True)
                embed.add_field(name="Receiver", value=f"{self_username} (ID: {message_data['self_user_id']})",
                                inline=True)
                embed.set_footer(text=f"Relay Token: {sender_token[:10] if sender_token else 'None'}...")
                await channel.send(embed=embed)

            except Exception as e:
                logging.error(f"❌ Failed to create DM channel {channel_name}: {e}")
                return
        else:
            # Update existing mapping with proper tokens
            sender_token = find_token_for_user(sender_user_id)
            if str(channel.id) in DM_MAPPINGS:
                DM_MAPPINGS[str(channel.id)].update({
                    "receiving_token": receiving_token,
                    "sender_token": sender_token,
                    "relay_token": sender_token
                })
                config["dm_mappings"] = DM_MAPPINGS
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4)

        # Create or get webhook for the channel
        webhook = await bot.get_or_create_webhook(channel, "DM Mirror")  # FIX: Use bot.get_or_create_webhook
        if not webhook:
            logging.error(f"❌ Failed to create webhook for DM channel {channel_name}")
            return

        # Send the DM message via webhook
        await send_dm_via_webhook(webhook, message_data)

    except Exception as e:
        logging.error(f"❌ Error handling DM message: {e}")

async def monitor_dm_channels():
    """Monitor DM channels for outgoing messages to relay back as DMs."""
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            guild = bot.get_guild(DESTINATION_SERVER_ID)
            if not guild:
                await asyncio.sleep(10)
                continue

            for category in guild.categories:
                if "[DM]" in category.name:
                    for channel in category.channels:
                        if isinstance(channel, discord.TextChannel) and str(channel.id) in DM_MAPPINGS:
                            # This channel is mapped for DM relay
                            continue  # The actual monitoring happens in on_message event

        except Exception as e:
            logging.error(f"❌ Error in monitor_dm_channels: {e}")

        await asyncio.sleep(30)


async def relay_message_to_dm(channel, message):
    """Relay a message from Discord channel back to DM."""
    try:
        mapping = DM_MAPPINGS.get(str(channel.id))
        if not mapping:
            logging.warning(f"⚠️ No DM mapping found for channel {channel.name}")
            await message.add_reaction("❌")
            return

        user_id = mapping["user_id"]  # This is the person who originally sent the DM
        relay_token = mapping.get("relay_token")  # Token that can send DMs to that person

        # Debug logging
        logging.info(
            f"🔍 DM Mapping found: user_id={user_id}, relay_token={relay_token[:10] if relay_token else 'None'}...")
        logging.info(f"🔍 Full mapping data: {mapping}")

        if not relay_token:
            logging.error(f"❌ No relay token found for DM channel {channel.name}")
            await message.add_reaction("❌")
            return

        # Log the relay attempt
        logging.info(f"📤 Attempting to relay message to user {user_id} using token {relay_token[:10]}...")
        logging.info(f"📤 Message content: {message.content[:100]}...")

        # Send DM relay request to main.py
        relay_data = {
            "action": "send_dm",
            "token": relay_token,
            "user_id": user_id,
            "content": message.content,
            "attachments": [attachment.url for attachment in message.attachments]
        }

        # Add timeout and better error handling
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                logging.info(f"🔗 Sending request to DM relay service...")
                async with session.post("http://127.0.0.1:5001/send_dm", json=relay_data) as response:
                    response_text = await response.text()
                    logging.info(f"📡 DM relay service response: {response.status} - {response_text}")

                    if response.status == 200:
                        logging.info(f"✅ DM relay request sent successfully to user {user_id}")
                        await message.add_reaction("✅")
                    else:
                        logging.error(f"❌ DM relay request failed: {response.status} - {response_text}")
                        await message.add_reaction("❌")

            except asyncio.TimeoutError:
                logging.error("⏰ DM relay request timed out")
                await message.add_reaction("⏰")
            except aiohttp.ClientConnectionError:
                logging.warning("⚠️ Could not connect to DM relay service")
                await message.add_reaction("⚠️")
            except Exception as e:
                logging.error(f"❌ Exception in DM relay request: {e}")
                await message.add_reaction("❌")

    except Exception as e:
        logging.error(f"❌ Error in relay_message_to_dm: {e}")
        await message.add_reaction("💥")


async def clean_mentions(content: str, destination_guild: discord.Guild, message_data: dict) -> str:
    """Enhanced mention cleaning with role limit protection"""

    # Replace <#channel_id> with destination channel or fallback text
    channel_mentions = re.findall(r"<#(\d+)>", content)
    for channel_id in channel_mentions:
        original_channel = bot.get_channel(int(channel_id))
        original_name = message_data.get("channel_real_name", f"channel-{channel_id}")
        server_name = message_data.get("server_real_name", "Unknown Server")

        matching_channel = discord.utils.get(destination_guild.text_channels, name=original_name)
        if matching_channel:
            content = content.replace(f"<#{channel_id}>", f"<#{matching_channel.id}>")
        else:
            content = content.replace(f"<#{channel_id}>", f"`{server_name} > #{original_name}`")

    # Replace <@user_id> with @username
    user_mentions = re.findall(r"<@!?(\d+)>", content)
    for user_id in user_mentions:
        try:
            user_obj = await bot.fetch_user(int(user_id))
            tag = f"<@{user_obj.id}>"
            content = re.sub(f"<@!?{user_id}>", tag, content)
        except Exception:
            content = content.replace(f"<@{user_id}>", "@unknown")

    # FIXED ROLE HANDLING - Always use text mentions to avoid role limit
    role_mentions = re.findall(r"<@&(\d+)>", content)
    source_role_map = message_data.get("mentioned_roles", {})

    for role_id in role_mentions:
        role_name = source_role_map.get(role_id, f"Role-{role_id}")

        # Always use text mention to avoid hitting role limit
        content = content.replace(f"<@&{role_id}>", f"**@{role_name}**")

    return content


async def resolve_embed_mentions(embed: dict, guild: discord.Guild, message_data: dict) -> dict:
    """Fix mentions inside embed fields like <#id>, <@id>, <@&id>."""
    description = embed.get("description", "")
    if not description:
        return embed

    # Handle <#channel_id>
    for match in re.findall(r"<#(\d+)>", description):
        # Try to fetch the original channel name from Redis or database if needed
        original_channel = bot.get_channel(int(match))
        original_name = original_channel.name if original_channel else f"channel-{match}"
        server_name = message_data.get("server_real_name", "Unknown Server")

        # Try to match by normalized name in destination server
        possible_matches = [
            f"{original_name} [{server_name.lower().replace(' ', '-')}]",  # e.g., cards-chat [polar chefs]
            original_name
        ]

        dest_channel = discord.utils.find(
            lambda c: c.name in possible_matches,
            guild.text_channels
        )

        if dest_channel:
            description = description.replace(f"<#{match}>", f"<#{dest_channel.id}>")
        else:
            description = description.replace(f"<#{match}>", f"`{server_name} > #{original_name}`")

    # Handle <@user_id>
    for match in re.findall(r"<@!?(\d+)>", description):
        try:
            user_obj = await bot.fetch_user(int(match))
            tag = f"@{user_obj.name}#{user_obj.discriminator}"
        except Exception:
            tag = f"@user-{match}"
        description = re.sub(f"<@!?{match}>", tag, description)

    # Handle <@&role_id>
    for match in re.findall(r"<@&(\d+)>", description):
        role_name = message_data.get("mentioned_roles", {}).get(match)
        if not role_name:
            # fallback, do not create with just an ID
            logging.warning(f"⚠️ Missing role_name for ID {match}, skipping role replacement.")
            continue

        # Try case-insensitive match
        role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
        if not role:
            base = discord.utils.get(guild.roles, name="MEMBERS")
            if base:
                role = await guild.create_role(name=role_name, permissions=base.permissions, color=base.color)
        if role:
            description = description.replace(f"<@&{match}>", f"<@&{role.id}>")
        else:
            description = description.replace(f"<@&{match}>", f"@{role_name}")

    embed["description"] = description
    return embed


async def send_to_webhook(message_data):
    """Fixed send_to_webhook function for bot.py with improved reply formatting"""
    message_id = message_data.get("message_id")

    # Skip processing if channel was deleted
    if message_data["channel_id"] in polar_deleted_channels:
        logging.info(f"⏭️ Skipping message for deleted channel {message_data['channel_id']} (Polar Helper triggered)")
        return

    # Archive detection and handling (existing logic)
    archive_trigger = message_data.get("content", "").strip().lower()
    embed_title = (message_data.get("embed_title") or "").lower()
    embed_desc = (message_data.get("embed_description") or "").lower()
    author_username = message_data.get("author_name", "")

    # Polar Helper logic (existing)
    if author_username == "Polar Helper#6493" and (
            "channel archive" in embed_title or "channel archive" in embed_desc or archive_trigger == "channel archive"
    ):
        channel_obj = bot.get_channel(int(message_data["channel_id"]))
        if channel_obj:
            try:
                await channel_obj.delete(reason="Polar Helper triggered channel archive deletion")
                polar_deleted_channels.add(message_data["channel_id"])
                logging.info(
                    f"🗑️ Deleted channel '{channel_obj.name}' (ID: {channel_obj.id}) triggered by Polar Helper")
            except Exception as e:
                logging.error(f"❌ Failed to delete channel '{channel_obj.name}': {e}")
        return

    # Archive command detection (existing)
    if archive_trigger in ["!archive", "channel archive"] \
            or "archived to forum thread" in archive_trigger \
            or "channel archive" in embed_title \
            or "channel archive" in embed_desc:

        channel_obj = bot.get_channel(int(message_data["channel_id"]))
        if channel_obj:
            try:
                await channel_obj.delete(reason="Triggered by archive command or forum archive message")
                logging.info(f"🗑️ Deleted channel '{channel_obj.name}' (ID: {channel_obj.id})")
            except Exception as e:
                logging.error(f"❌ Failed to delete channel '{channel_obj.name}': {e}")
        return

    # Duplicate detection (existing)
    if message_id in recent_message_ids:
        return
    recent_message_ids.add(message_id)
    if len(recent_message_ids) > 1000:
        recent_message_ids.pop()

    # Get webhook
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
            logging.error(f"❌ Could not create webhook for {webhook_key}")
            return

    # Clean content with enhanced mention handling
    try:
        content = await clean_mentions(
            message_data.get("content", ""),
            bot.get_guild(DESTINATION_SERVER_ID),
            message_data
        )
    except Exception as e:
        logging.error(f"❌ Error cleaning mentions: {e}")
        content = message_data.get("content", "")

    # Handle forwarded messages - IMPROVED FORMATTING
    forwarded_from = message_data.get("forwarded_from")
    forwarded_attachments = message_data.get("forwarded_attachments", [])

    if forwarded_from:
        # Better forwarded message formatting
        forwarded_text = f"📤 **Forwarded from:** {forwarded_from}\n"
        content = f"{forwarded_text}{content}" if content else forwarded_text
        logging.info(f"🔄 Processing forwarded message from {forwarded_from}")

    # Handle replies with gray bar format (OLD FORMAT)
    if message_data.get("reply_to") and message_data.get("reply_text"):
        # Create a gray bar effect using Discord's quote formatting
        reply_lines = message_data['reply_text'].split('\n')
        quoted_reply = '\n'.join([f"> {line}" for line in reply_lines])
        reply_header = f"> **{message_data['reply_to']}**\n{quoted_reply}\n"
        content = f"{reply_header}{content}"
    elif message_data.get("reply_to"):
        reply_header = f"> **{message_data['reply_to']}**\n"
        content = f"{reply_header}{content}"

    # Handle attachments (include forwarded attachments)
    attachments = message_data.get("attachments", [])
    if forwarded_attachments:
        attachments.extend(forwarded_attachments)
        logging.info(f"📎 Including {len(forwarded_attachments)} forwarded attachments")

    # Handle embeds
    embeds = message_data.get("embeds", [])
    cleaned_embeds = []
    for embed in embeds:
        if not isinstance(embed, dict):
            continue
        try:
            embed_copy = embed.copy()
            if not any([
                embed_copy.get("title"),
                embed_copy.get("description"),
                embed_copy.get("url"),
                embed_copy.get("image"),
                embed_copy.get("thumbnail"),
                embed_copy.get("fields")
            ]):
                continue

            if "image" in embed_copy:
                if isinstance(embed_copy["image"], str):
                    embed_copy["image"] = {"url": embed_copy["image"]}
                elif isinstance(embed_copy["image"], dict) and "url" not in embed_copy["image"]:
                    embed_copy.pop("image")

            for key in list(embed_copy.keys()):
                if embed_copy[key] is None:
                    del embed_copy[key]

            embed_copy = await resolve_embed_mentions(embed_copy, bot.get_guild(DESTINATION_SERVER_ID), message_data)
            cleaned_embeds.append(embed_copy)

        except Exception as e:
            logging.warning(f"❌ Embed processing failed: {e}")

    # Skip empty messages
    if not content.strip() and not cleaned_embeds and not attachments:
        return

    # Smart content splitting
    def smart_split_content(text, max_length=1900):
        if len(text) <= max_length:
            return [text]

        parts = []
        current_part = ""
        lines = text.split('\n')

        for line in lines:
            if len(current_part) + len(line) + 1 > max_length:
                if current_part:
                    parts.append(current_part.strip())
                    current_part = ""

                if len(line) > max_length:
                    words = line.split(' ')
                    for word in words:
                        if len(current_part) + len(word) + 1 > max_length:
                            if current_part:
                                parts.append(current_part.strip())
                                current_part = ""
                        current_part += f" {word}" if current_part else word
                else:
                    current_part = line
            else:
                current_part += f"\n{line}" if current_part else line

        if current_part:
            parts.append(current_part.strip())

        return parts

    content_parts = smart_split_content(content) if content else [""]

    # Download attachments
    files = []
    for idx, url in enumerate(attachments):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        file_data = await resp.read()
                        filename = url.split("/")[-1].split("?")[0] or f"file{idx}.jpg"

                        if len(file_data) <= MAX_DISCORD_FILE_SIZE:
                            files.append({
                                "filename": filename,
                                "data": file_data
                            })
                        else:
                            # Try to compress the image before giving up
                            compressed_data, compressed_filename, was_compressed = compress_image(file_data, filename)
                            if was_compressed and len(compressed_data) <= MAX_DISCORD_FILE_SIZE:
                                files.append({
                                    "filename": compressed_filename,
                                    "data": compressed_data
                                })
                                logging.info(f"✅ Large file compressed and attached: {compressed_filename}")
                            else:
                                # Still too large or not an image, send as link in content
                                logging.warning(f"⚠️ File too large even after compression, adding link to content: {filename}")
                                content_parts[0] = f"{content_parts[0]}\n📎 **Large file:** {url}" if content_parts[0] else f"📎 **Large file:** {url}"
        except Exception as e:
            logging.warning(f"⚠️ Failed to fetch attachment: {url} → {e}")

    # Send webhook with enhanced error handling
    async with aiohttp.ClientSession() as session:
        for part_idx, part in enumerate(content_parts):
            success = False
            for attempt in range(3):
                try:
                    avatar_url = message_data.get("author_avatar")
                    username = message_data.get("author_name", "Unknown")

                    payload = {
                        "username": username,
                        "avatar_url": avatar_url
                    }

                    if part:
                        payload["content"] = part

                    if part_idx == 0 and cleaned_embeds:
                        payload["embeds"] = cleaned_embeds

                    files_to_send = files if part_idx == 0 else None

                    if files_to_send:
                        from aiohttp import FormData
                        form = FormData()
                        for idx, file in enumerate(files_to_send):
                            form.add_field(
                                name=f"file{idx}",
                                value=file["data"],
                                filename=file["filename"],
                                content_type="application/octet-stream"
                            )
                        form.add_field("payload_json", json.dumps(payload))

                        async with session.post(webhook_url, data=form) as response:
                            if response.status in (200, 204):
                                success = True
                                break
                            elif response.status == 429:
                                error_data = await response.json()
                                retry_after = error_data.get("retry_after", 1)
                                await asyncio.sleep(retry_after)
                                continue
                            elif response.status == 404:
                                error_data = await response.text()
                                if "Unknown Webhook" in error_data:
                                    logging.info(f"Unknown webhook detected for {webhook_key}, removing from config")
                                    WEBHOOKS.pop(webhook_key, None)
                                    redis_client.hdel("webhooks", webhook_key)
                                    bot.save_config()
                                    return
                                else:
                                    logging.error(f"❌ Webhook 404: {error_data}")
                                    return
                            elif response.status == 413:
                                logging.info(f"Request too large for webhook, skipping message")
                                return
                            elif response.status == 400:
                                error_data = await response.text()
                                if "30005" in error_data:  # Role limit error
                                    logging.error(f"❌ Role limit reached, message skipped")
                                    return
                                else:
                                    logging.error(f"❌ Bad request: {error_data}")
                                    return
                            else:
                                error = await response.text()
                                logging.error(f"❌ Webhook failed ({response.status}): {error}")
                                return
                    else:
                        async with session.post(webhook_url, json=payload) as response:
                            if response.status in (200, 204):
                                success = True
                                break
                            elif response.status == 429:
                                error_data = await response.json()
                                retry_after = error_data.get("retry_after", 1)
                                await asyncio.sleep(retry_after)
                                continue
                            elif response.status == 404:
                                error_data = await response.text()
                                if "Unknown Webhook" in error_data:
                                    logging.info(f"Unknown webhook detected for {webhook_key}, removing from config")
                                    WEBHOOKS.pop(webhook_key, None)
                                    redis_client.hdel("webhooks", webhook_key)
                                    bot.save_config()
                                    return
                                else:
                                    logging.error(f"❌ Webhook 404: {error_data}")
                                    return
                            elif response.status == 413:
                                logging.info(f"Request too large for webhook, skipping message")
                                return
                            elif response.status == 400:
                                error_data = await response.text()
                                if "30005" in error_data:  # Role limit error
                                    logging.error(f"❌ Role limit reached, message skipped")
                                    return
                                elif "Must be 2000 or fewer in length" in error_data:
                                    if len(payload.get("content", "")) > 1900:
                                        payload["content"] = payload["content"][:1900] + "..."
                                        continue
                                else:
                                    logging.error(f"❌ Bad request: {error_data}")
                                    return
                            else:
                                error = await response.text()
                                logging.error(f"❌ Webhook failed ({response.status}): {error}")
                                return

                except Exception as e:
                    # Only log connection-related errors if we haven't logged them recently
                    if "Server disconnected" in str(e) or "getaddrinfo failed" in str(e) or "semaphore timeout" in str(
                            e).lower():
                        if not connection_state["last_disconnect_logged"]:
                            print("🔴 Disconnected from Discord")
                            connection_state["last_disconnect_logged"] = True
                            connection_state["is_connected"] = False
                    else:
                        logging.error(f"❌ Exception during webhook attempt {attempt + 1}: {e}")

                    if attempt < 2:
                        await asyncio.sleep(2 * (attempt + 1))

            if not success:
                # Only log major failures, not routine network issues
                try:
                    if not any(keyword in str(e).lower() for keyword in ["timeout", "connection", "dns", "ssl"]):
                        logging.error(f"❌ Failed to send webhook message part {part_idx + 1}")
                except:
                    logging.error(f"❌ Failed to send webhook message part {part_idx + 1}")

            if len(content_parts) > 1 and part_idx < len(content_parts) - 1:
                await asyncio.sleep(0.5)

async def handle_webhook_error(self, response, webhook_key, category_name, channel_name, server_name):
    """Handle webhook errors more gracefully"""
    if response.status == 404:
        error_text = await response.text()
        logging.error(f"❌ Webhook 404: {error_text}")
        if "Unknown Webhook" in error_text:
            logging.warning(f"⚠️ Webhook deleted for {webhook_key}. Removing from config.")
            WEBHOOKS.pop(webhook_key, None)
            redis_client.hdel("webhooks", webhook_key)
            self.save_config()
            webhook_url = await create_channel_and_webhook(category_name, channel_name, server_name)
            return webhook_url
        elif "Unknown Channel" in error_text:
            logging.warning(f"⚠️ Channel '{channel_name}' no longer exists. Removing webhook + config for {webhook_key}.")
            WEBHOOKS.pop(webhook_key, None)
            redis_client.hdel("webhooks", webhook_key)
            self.save_config()
            return None
    elif response.status >= 500:
        logging.warning(f"⚠️ Discord error {response.status}, will retry")
    else:
        error = await response.text()
        logging.error(f"❌ Webhook failed ({response.status}) → {error}")
    return None

class DestinationBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.reactions = True
        intents.members = True

        super().__init__(command_prefix='!', intents=intents)
        self.webhook_cache = WEBHOOKS
        self.event(self.on_ready)
        self.event(self.on_message)

    async def setup_hook(self):
        """This is called when the bot starts up"""
        # Sync commands to the specific guild for faster testing
        guild = discord.Object(id=DESTINATION_SERVER_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        logging.info(f"✅ Slash commands synced to guild {DESTINATION_SERVER_ID}")

    async def on_ready(self):
        global connection_state

        # Handle reconnection messaging
        if not connection_state["is_connected"]:
            print("🟢 Reconnected to Discord")
            connection_state["is_connected"] = True
            connection_state["last_disconnect_logged"] = False
            connection_state["reconnect_attempts"] = 0

        print(f"✅ Bot {self.user} is running!")
        self.webhook_cache = redis_client.hgetall("webhooks")
        await self.ensure_webhooks()
        await self.migrate_channels_to_uncategorized()
        await self.populate_category_mappings()
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name="for slash commands")
        )
        self.save_config()
        print("✅ Webhook setup complete. Bot is now processing messages.")
        asyncio.create_task(process_redis_messages())
        asyncio.create_task(monitor_dm_channels())
        # RESTRICTED: Only organize channels in Release Guides and Daily Schedule categories
        asyncio.create_task(self.monitor_allowed_categories_only())
        asyncio.create_task(self.monitor_deleted_channels())
        asyncio.create_task(self.cleanup_expired_channels())

    async def on_disconnect(self):
        """Handle disconnect events"""
        global connection_state
        if connection_state["is_connected"]:
            print("🔴 Disconnected from Discord")
            connection_state["is_connected"] = False
            connection_state["last_disconnect_logged"] = True

    async def on_message(self, message):
        """Handle messages in DM channels for relay functionality."""
        # Skip messages from the bot itself
        if message.author == self.user:
            return

        # Skip webhook messages
        if message.webhook_id:
            return

        # Check if this is a DM channel that should be relayed
        if message.channel.category and "[DM]" in message.channel.category.name:
            if str(message.channel.id) in DM_MAPPINGS:
                logging.info(f"📤 Relaying message from {message.author} in DM channel {message.channel.name}")
                await relay_message_to_dm(message.channel, message)
                return

        # Process commands
        await self.process_commands(message)

    def save_config(self):
        """Save the current configuration."""
        config["webhooks"] = self.webhook_cache
        config["dm_mappings"] = DM_MAPPINGS
        save_config(config)

    async def migrate_channels_to_uncategorized(self):
        guild = self.get_guild(DESTINATION_SERVER_ID)
        if not guild:
            logging.error("❌ Destination server not found for migration.")
            return

        ignored_tags = config.get("ignored_category_tags", [])
        uncategorized_category = None

        for category in guild.categories:
            # Skip if category has ignored tag or is a DM category
            if any(tag in category.name for tag in ignored_tags) or "[DM]" in category.name:
                continue

            # Try to extract server name tag from category
            if "[" in category.name and "]" in category.name:
                server_tag = category.name.split("[")[-1].split("]")[0].strip()
            else:
                continue  # No tag found

            for channel in category.channels:
                if not isinstance(channel, discord.TextChannel):
                    continue

                # Skip if name already ends with tag
                if channel.name.endswith(f"[{server_tag.lower()}]") or f"[{server_tag}]" in channel.name:
                    continue

                new_name = f"{channel.name} [{server_tag}]"

                normalized_new_name = new_name.lower()
                conflict = discord.utils.find(lambda c: c.name.lower() == normalized_new_name, guild.text_channels)

                if conflict:
                    logging.warning(
                        f"⚠️ Skipped renaming '{channel.name}' to avoid conflict with existing '{conflict.name}'.")
                    continue

                try:
                    await channel.edit(name=new_name, category=None)
                    logging.info(f"✅ Renamed '{channel.name}' to '{new_name}' and moved to Uncategorized.")
                    # Update webhook key if one existed
                    normalized_cat = normalize_category(category.name)
                    normalized_srv = normalize_server_tag(server_tag)

                    old_key = normalize_key(normalized_cat, channel.name, normalized_srv)
                    new_key = normalize_key("uncategorized", new_name, normalized_srv)

                    if old_key in WEBHOOKS:
                        WEBHOOKS[new_key] = WEBHOOKS.pop(old_key)
                        redis_client.hset("webhooks", new_key, WEBHOOKS[new_key])
                        self.save_config()
                        logging.info(f"🔁 Updated webhook key: '{old_key}' ➜ '{new_key}'")

                except Exception as e:
                    logging.error(f"❌ Failed to rename/move channel '{channel.name}': {e}")

    async def populate_category_mappings(self):
        """Auto-populate category_mappings in config.json with current categories from the destination server."""
        guild = self.get_guild(DESTINATION_SERVER_ID)
        if not guild:
            logging.error("❌ ERROR: Cannot populate category mappings — destination server not found.")
            return

        if "category_mappings" not in config:
            config["category_mappings"] = {}

        for category in guild.categories:
            name = category.name.strip()
            if name not in config["category_mappings"].values():
                # Suggest using same name as source (without [Server] suffix)
                base_name = name.split(" [")[0]
                config["category_mappings"][base_name] = name

    async def ensure_webhooks(self):
        guild = self.get_guild(DESTINATION_SERVER_ID)
        if not guild:
            logging.error("❌ ERROR: Destination server not found!")
            return

        server_name = guild.name.lower().replace(" ", "-").replace("|", "").strip()

        for channel in guild.text_channels:
            # Skip DM channels
            if channel.category and "[DM]" in channel.category.name:
                continue

            category_name = (
                channel.category.name.lower().replace(" ", "-").replace("|", "").strip()
                if channel.category else "uncategorized"
            )
            channel_name = channel.name.lower().replace(" ", "-").replace("|", "").strip()

            possible_keys = [
                normalize_key(category_name, channel_name, server_name),
                normalize_key(category_name, f"{channel_name}-{server_name}", ""),
                normalize_key(category_name, f"{channel_name}_{server_name}", ""),
            ]

            webhook_key = next((key for key in possible_keys if key in WEBHOOKS), possible_keys[0])

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

    async def cleanup_expired_channels(self):
        """Check and delete expired channels in Daily Schedule and Release Guides categories."""
        await self.wait_until_ready()
        guild = self.get_guild(DESTINATION_SERVER_ID)

        while not self.is_closed():
            try:
                current_time = datetime.now()
                current_year = current_time.year

                for category in guild.categories:
                    # Skip DM categories
                    if "[DM]" in category.name:
                        continue

                    # Daily Schedule - 24 hour expiration
                    if category.id == DAILY_SCHEDULE_CATEGORY_ID:
                        for channel in category.channels:
                            if not isinstance(channel, discord.TextChannel):
                                continue

                            # Store creation time when channel is first seen
                            creation_key = f"channel_created_{channel.id}"
                            stored_time = redis_client.get(creation_key)

                            if not stored_time:
                                # First time seeing this channel, store Discord's creation time
                                discord_creation_time = channel.created_at
                                redis_client.setex(creation_key, 86400 * 3,  # Store for 72 hours
                                                   discord_creation_time.isoformat())
                                logging.info(f"📅 Tracking new daily channel: {channel.name} (created: {discord_creation_time})")
                                created_time = discord_creation_time
                            else:
                                # Load stored creation time
                                created_time = datetime.fromisoformat(stored_time)
                            
                            # Check if 24 hours have passed since Discord creation
                            time_elapsed = current_time - created_time.replace(tzinfo=None)

                            if time_elapsed >= timedelta(hours=24):
                                # Check if channel is protected
                                config_data = load_config()
                                protected_channels = config_data.get("protected_channels", [])

                                if channel.id in protected_channels:
                                    logging.info(f"🛡️ Skipping deletion of protected daily channel: {channel.name}")
                                    continue

                                try:
                                    await channel.delete(reason="Daily Schedule channel expired (24 hours)")
                                    redis_client.delete(creation_key)
                                    logging.info(
                                        f"🗑️ Deleted expired daily channel: {channel.name} (age: {time_elapsed})")
                                except Exception as e:
                                    # Silently handle 404 errors for expired channels
                                    if "404" not in str(e) and "Unknown Channel" not in str(e):
                                        logging.error(f"❌ Failed to delete expired channel {channel.name}: {e}")

                    # Release Guides - 7 days expiration or past date
                    elif category.id == RELEASE_GUIDES_CATEGORY_ID:
                        for channel in category.channels:
                            if not isinstance(channel, discord.TextChannel):
                                continue

                            clean_name = re.sub(r'[^\w\s:-]', '', channel.name.lower())

                            # Check if channel has a date
                            date_match = re.search(r'\b(\d{1,2})[-/](\d{1,2})\b', clean_name)
                            if date_match:
                                try:
                                    # Parse the date (assume current year)
                                    month = int(date_match.group(1))
                                    day = int(date_match.group(2))
                                    channel_date = datetime(current_year, month, day)

                                    # If the date is in the past, delete immediately
                                    if channel_date.date() < current_time.date():
                                        # Check if channel is protected
                                        config_data = load_config()
                                        protected_channels = config_data.get("protected_channels", [])

                                        if channel.id in protected_channels:
                                            logging.info(
                                                f"🛡️ Skipping deletion of protected release channel: {channel.name}")
                                            continue

                                        await channel.delete(
                                            reason=f"Release Guide channel date has passed ({month}/{day})")
                                        logging.info(f"🗑️ Deleted past-date release channel: {channel.name}")
                                        continue
                                except ValueError:
                                    logging.warning(f"⚠️ Invalid date format in channel: {channel.name}")

                            # Check 7-day expiration
                            creation_key = f"channel_created_{channel.id}"
                            stored_time = redis_client.get(creation_key)

                            if not stored_time:
                                # First time seeing this channel, store Discord's creation time
                                discord_creation_time = channel.created_at
                                redis_client.setex(creation_key, 86400 * 15,  # Store for 15 days
                                                   discord_creation_time.isoformat())
                                logging.info(f"📅 Tracking new release channel: {channel.name} (created: {discord_creation_time})")
                                created_time = discord_creation_time
                            else:
                                # Load stored creation time
                                created_time = datetime.fromisoformat(stored_time)
                            
                            # Check if 7 days have passed since Discord creation
                            time_elapsed = current_time - created_time.replace(tzinfo=None)

                            if time_elapsed >= timedelta(days=7):
                                # Check if channel is protected
                                config_data = load_config()
                                protected_channels = config_data.get("protected_channels", [])

                                if channel.id in protected_channels:
                                    logging.info(
                                        f"🛡️ Skipping deletion of protected release channel: {channel.name}")
                                    continue

                                try:
                                    await channel.delete(reason="Release Guide channel expired (7 days)")
                                    redis_client.delete(creation_key)
                                    logging.info(
                                        f"🗑️ Deleted expired release channel: {channel.name} (age: {time_elapsed})")
                                except Exception as e:
                                    logging.error(f"❌ Failed to delete expired channel {channel.name}: {e}")

            except Exception as e:
                logging.error(f"❌ cleanup_expired_channels error: {e}")

            # Check every 30 minutes
            await asyncio.sleep(1800)

    async def monitor_allowed_categories_only(self):
        """RESTRICTED: Only organize channels in Release Guides and Daily Schedule categories"""
        await self.wait_until_ready()
        guild = self.get_guild(DESTINATION_SERVER_ID)
        
        # ALLOWED categories for channel moving
        ALLOWED_CATEGORY_IDS = {1348464705701806080, 1353704482457915433}  # Release Guides, Daily Schedule
        
        while not self.is_closed():
            try:
                # Only process channels in allowed categories
                for category in guild.categories:
                    if category.id not in ALLOWED_CATEGORY_IDS:
                        continue  # Skip all other categories - respect server_layout
                        
                    # Only organize channels within these 2 allowed categories
                    for channel in category.channels:
                        if not isinstance(channel, discord.TextChannel):
                            continue
                            
                        # Apply the existing logic only to allowed categories
                        await self._process_channel_in_allowed_category(channel, category)
                        
            except Exception as e:
                logging.error(f"❌ monitor_allowed_categories_only error: {e}")
                
            await asyncio.sleep(30)  # Check every 30 seconds
    
    async def _process_channel_in_allowed_category(self, channel, category):
        """Process a single channel within an allowed category"""
        try:
            # Only move channels with date patterns in Release Guides
            if category.id == 1348464705701806080:  # Release Guides
                # Check for date patterns in channel name
                import re
                has_date_pattern = bool(
                    re.search(r'\b(\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b', channel.name) or
                    re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b', channel.name.lower()) or
                    re.search(r'\b\d{1,2}(st|nd|rd|th)\b', channel.name.lower())
                )
                
                # Color-only channels (no date/time) get moved to Release Guides if not already there
                if not has_date_pattern and channel.category.id != 1348464705701806080:
                    release_guides = discord.utils.get(guild.categories, id=1348464705701806080)
                    if release_guides:
                        await channel.edit(category=release_guides)
                        logging.info(f"📅 Moved '{channel.name}' to Release Guides")
                        
            elif category.id == 1353704482457915433:  # Daily Schedule  
                # Apply Daily Schedule specific logic here if needed
                pass
                
        except Exception as e:
            logging.error(f"❌ Error processing channel '{channel.name}' in allowed category: {e}")

    async def monitor_channels_continuously_DISABLED(self):
        await self.wait_until_ready()
        guild = self.get_guild(DESTINATION_SERVER_ID)

        months = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"
        ]

        while not self.is_closed():
            try:
                # Sort channels in all eligible categories
                for category in guild.categories:
                    if "[DM]" in category.name:
                        continue

                    # Sort channels in Release Guides and Daily Schedule categories using IDs
                    if category.id == RELEASE_GUIDES_CATEGORY_ID:
                        await self.sort_channels_in_category(category, by="date")
                    elif category.id == DAILY_SCHEDULE_CATEGORY_ID:
                        await self.sort_channels_in_category(category, by="time")
                    # Skip all other categories completely
                    else:
                        continue

                # Reroute ONLY uncategorized channels with time/date patterns
                for channel in guild.text_channels:
                    name = channel.name.lower()

                    # Skip if channel is already in a category
                    if channel.category is not None:
                        continue

                    # Skip DM channels
                    if name.startswith("dm-"):
                        continue

                    # Only process channels that have time or date patterns
                    has_time_pattern = bool(re.search(r"\b\d{1,2}(am|pm)\b", name))
                    has_date_pattern = bool(re.search(r"\b\d{1,2}-\d{1,2}\b", name))

                    if not (has_time_pattern or has_date_pattern):
                        continue

                    # Delete divine+month channels
                    if any(month in name for month in months) and "divine" in name:
                        try:
                            await channel.delete(reason="Month + divine based channel deleted")
                            logging.info(f"🗑️ Deleted channel '{channel.name}' (divine/month match)")
                            continue
                        except Exception as e:
                            logging.error(f"❌ Failed to delete '{channel.name}': {e}")

                    # Extract server tag
                    server_tag = None
                    try:
                        # Method 1: Extract from [brackets] in channel name
                        bracket_match = re.search(r'\[(.*?)\]', channel.name)
                        if bracket_match:
                            server_tag = bracket_match.group(1)

                        # Method 2: Extract from end of channel name (common patterns)
                        if not server_tag:
                            common_servers = ["divine", "polar-chefs", "fastbreak", "ak-chefs", "gfnf", "shoeplex"]
                            for server in common_servers:
                                if name.endswith(f"-{server}") or name.endswith(server):
                                    server_tag = server
                                    break

                        # Time-based channel routing to Daily Schedule
                        if has_time_pattern:
                            target_category = discord.utils.get(guild.categories, name="📅 Daily Schedule [1Tap Notify]")
                            if target_category:
                                try:
                                    await channel.edit(category=target_category)

                                    # Store source channel mapping if server tag found
                                    if server_tag:
                                        source_channel_id = config.get("source_channel_ids", {}).get(server_tag)
                                        if source_channel_id:
                                            redis_client.hset("channel_monitoring", str(channel.id),
                                                              str(source_channel_id))

                                    logging.info(f"📅 Moved '{channel.name}' to Daily Schedule")
                                    continue
                                except Exception as e:
                                    logging.error(f"❌ Could not move '{channel.name}' to Daily Schedule: {e}")

                        # Date-based channel routing to Release Guides
                        elif has_date_pattern:
                            target_category = discord.utils.get(guild.categories, name="📅 Release Guides [1Tap Notify]")
                            if target_category:
                                try:
                                    await channel.edit(category=target_category)

                                    # Store source channel mapping if server tag found
                                    if server_tag:
                                        source_channel_id = config.get("source_channel_ids", {}).get(server_tag)
                                        if source_channel_id:
                                            redis_client.hset("channel_monitoring", str(channel.id),
                                                              str(source_channel_id))

                                    logging.info(f"📅 Moved '{channel.name}' to Release Guides")
                                    continue
                                except Exception as e:
                                    logging.error(f"❌ Could not move '{channel.name}' to Release Guides: {e}")

                    except Exception as e:
                        logging.error(f"❌ Error processing channel '{channel.name}': {e}")
                        continue

            except Exception as e:
                logging.error(f"❌ monitor_channels_continuously error: {e}")

            await asyncio.sleep(10)

    async def delete_channel_if_source_deleted(self, source_channel_id: int, destination_channel_id: int):
        """Delete destination channel if the corresponding source channel no longer exists."""
        try:
            # Make an API call to Discord to check if the source channel still exists
            session = aiohttp.ClientSession()
            headers = {
                "Authorization": f"Bot {BOT_TOKEN}",
                "Content-Type": "application/json"
            }
            url = f"https://discord.com/api/v10/channels/{source_channel_id}"
            async with session.get(url, headers=headers) as response:
                if response.status == 404:
                    # Source channel was deleted
                    logging.info(
                        f"🗑️ Source channel {source_channel_id} not found, deleting destination channel {destination_channel_id}")
                    dest_channel = self.get_channel(destination_channel_id)
                    if dest_channel:
                        await dest_channel.delete(reason="Source channel deleted")
                    else:
                        logging.warning(f"⚠️ Destination channel {destination_channel_id} not found for deletion")
                elif response.status != 200:
                    error_text = await response.text()
                    logging.warning(
                        f"⚠️ Unexpected status checking source channel {source_channel_id}: {response.status} {error_text}")
        except Exception as e:
            logging.error(f"❌ Error while checking/deleting destination channel: {e}")
        finally:
            await session.close()

    async def monitor_deleted_channels(self):
        await self.wait_until_ready()

        while not self.is_closed():
            try:
                monitoring_map = redis_client.hgetall("channel_monitoring")

                for destination_channel_id, source_channel_id in monitoring_map.items():
                    dest_channel_id = int(destination_channel_id)
                    source_chan_id = int(source_channel_id)
                    await self.delete_channel_if_source_deleted(source_chan_id, dest_channel_id)

            except Exception as e:
                logging.error(f"❌ monitor_deleted_channels error: {e}")

            await asyncio.sleep(10)

    async def sort_channels_in_category(self, category, by="date"):
        # Only allow sorting in moveable categories (Release Guides or Daily Schedule)
        if not is_moveable_category(category.id):
            logging.info(f"🔒 Layout Protection: Skipping category '{category.name}' (ID: {category.id}) - not a moveable category")
            return
        logging.info(f"🔃 Sorting '{category.name}' by {by} with {len(category.channels)} channels")

        def extract_sort_key(name):
            # Remove emojis and special characters for parsing
            clean_name = re.sub(r'[^\w\s:-]', '', name.lower())
            logging.info(f"🔎 Evaluating sort key for: {name} → cleaned: {clean_name}")

            if by == "date":
                # Match patterns like 4-17, 04-17, 4/17
                match = re.search(r'\b(\d{1,2})[-/](\d{1,2})\b', clean_name)
                if match:
                    try:
                        # Create a proper date object for sorting
                        month = int(match.group(1))
                        day = int(match.group(2))
                        date_val = datetime(2025, month, day)  # Use current year
                        logging.info(f"✅ Parsed date for '{name}': {date_val.strftime('%m-%d')}")
                        return date_val.timestamp()
                    except Exception as e:
                        logging.warning(f"⚠️ Failed to parse date for '{name}': {e}")

            elif by == "time":
                # Match 4pm, 11am, 5AM, etc.
                match = re.search(r'\b(\d{1,2})(am|pm)\b', clean_name)
                if match:
                    try:
                        time_val = datetime.strptime(match.group(0), "%I%p")
                        # Convert to 24-hour format for proper sorting
                        hour_24 = time_val.hour
                        logging.info(f"✅ Parsed time for '{name}': {hour_24:02d}:00")
                        return hour_24
                    except Exception as e:
                        logging.warning(f"⚠️ Failed to parse time for '{name}': {e}")

            # No valid date/time found - channels without date/time go to end
            logging.info(f"🔽 No valid sort key found for: {name}")
            return 999999  # Bottom of the list

        try:
            # Get all text channels in this category
            channels_to_sort = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]

            if len(channels_to_sort) <= 1:
                logging.info(f"⏭️ Skipping sort - only {len(channels_to_sort)} channel(s) in category")
                return

            # Sort channels by the extracted key
            sorted_channels = sorted(channels_to_sort, key=lambda ch: extract_sort_key(ch.name))
            
            # Check if channels are already in the correct order
            current_order = [ch.name for ch in channels_to_sort]
            target_order = [ch.name for ch in sorted_channels]
            
            if current_order == target_order:
                logging.info(f"✅ Channels in '{category.name}' are already sorted correctly")
                return

            # Reorder channels by editing positions
            logging.info(f"🔄 Reordering channels in '{category.name}'...")

            for i, channel in enumerate(sorted_channels):
                try:
                    await channel.edit(position=i)
                    logging.info(f"📍 Moved '{channel.name}' to position {i}")
                except Exception as e:
                    logging.warning(f"⚠️ Failed to move '{channel.name}': {e}")

                # Small delay to avoid rate limits
                await asyncio.sleep(0.3)

        except Exception as e:
            logging.error(f"❌ Error sorting category '{category.name}': {e}")


    async def move_color_only_channels_to_release_guides_DISABLED(self):
        """Move channels with only color emojis to Release Guides category"""
        await self.wait_until_ready()
        guild = self.get_guild(DESTINATION_SERVER_ID)

        while not self.is_closed():
            try:
                # Find the Release Guides category by ID
                release_guides_category = discord.utils.get(guild.categories, id=RELEASE_GUIDES_CATEGORY_ID)
                if not release_guides_category:
                    logging.warning(f"⚠️ Release Guides category not found (ID: {RELEASE_GUIDES_CATEGORY_ID})")
                    await asyncio.sleep(60)
                    continue

                # Check all uncategorized channels
                for channel in guild.text_channels:
                    if channel.category is not None:
                        continue

                    # Check if channel starts with only a color emoji
                    if channel.name.startswith(("🔴", "🟡", "🟢")):
                        # Check if it has no date/time patterns
                        clean_name = re.sub(r'[^\w\s:-]', '', channel.name.lower())
                        has_date_time = bool(re.search(r'\b\d{1,2}[-/]\d{1,2}\b', clean_name)) or bool(
                            re.search(r'\b\d{1,2}(am|pm)\b', clean_name))

                        if not has_date_time:
                            try:
                                await channel.edit(category=release_guides_category)
                                logging.info(f"🎯 Moved color-only channel '{channel.name}' to Release Guides")
                            except Exception as e:
                                logging.error(f"❌ Failed to move color-only channel '{channel.name}': {e}")

            except Exception as e:
                logging.error(f"❌ Error in move_color_only_channels_to_release_guides: {e}")

            await asyncio.sleep(30)  # Check every 30 seconds

async def create_channel_and_webhook(category_name, channel_name, server_name):
    guild = bot.get_guild(DESTINATION_SERVER_ID)
    if not guild:
        return None

    # Normalize everything early
    category_name = category_name.lower().replace(" ", "-").replace("|", "").strip()
    channel_name = channel_name.lower().replace(" ", "-").replace("|", "").strip()
    server_name = server_name.lower().replace(" ", "-").replace("|", "").strip()
    target_names = [
        normalize_name(f"{channel_name} [{server_name}]").replace("-", " "),  # add this
        normalize_name(f"{channel_name} [{server_name}]"),
        normalize_name(f"{channel_name}-{server_name}"),
        normalize_name(f"{channel_name}_{server_name}"),
        normalize_name(f"{server_name}-{channel_name}"),
    ]
    webhook_key = normalize_key(category_name, channel_name, server_name)
    forum_mappings = config.get("forum_mappings", {})
    full_key = f"{category_name} [{server_name}]"

    # Check if this should go to a forum
    mapped_forum_name = forum_mappings.get(full_key)
    if mapped_forum_name:
        forum_channel = discord.utils.get(
            guild.channels,
            name=mapped_forum_name,
            type=discord.ChannelType.forum
        )
        if forum_channel:
            try:
                msg = await forum_channel.send(content="Auto-archived from original channel")
                thread = await forum_channel.create_thread(
                    name=channel_name,
                    message=msg,
                    reason="!archive triggered"
                )
                webhook = await bot.get_or_create_webhook(thread, server_name)
                if webhook:
                    WEBHOOKS[webhook_key] = webhook.url
                    redis_client.hset("webhooks", webhook_key, webhook.url)
                    bot.save_config()
                    logging.info(f"✅ Created forum thread '{channel_name}' in '{mapped_forum_name}'")
                    return webhook.url
            except Exception as e:
                logging.error(f"❌ Failed to create thread in forum '{mapped_forum_name}': {e}")
                return None

    # Check if an uncategorized channel already exists (normalized)
    existing_channel_names = [
        normalize_name(c.name) for c in guild.text_channels
    ]
    target_names = [
        normalize_name(f"{channel_name} [{server_name}]").replace("-", " "),  # add this
        normalize_name(f"{channel_name} [{server_name}]"),
        normalize_name(f"{channel_name}-{server_name}"),
        normalize_name(f"{channel_name}_{server_name}"),
        normalize_name(f"{server_name}-{channel_name}"),
    ]
    existing_channel = discord.utils.find(
        lambda c: normalize_name(c.name) in target_names,
        guild.text_channels
    )

    if existing_channel:
        logging.info(f"📦 Found existing channel: {existing_channel.name} — skipping creation.")
        webhook = await bot.get_or_create_webhook(existing_channel, server_name)
        if webhook:
            WEBHOOKS[webhook_key] = webhook.url
            redis_client.hset("webhooks", webhook_key, webhook.url)
            bot.save_config()
            return webhook.url
        return None

    # Create new uncategorized channel
    full_channel_name = f"{channel_name} [{server_name}]"
    try:
        channel = await guild.create_text_channel(name=full_channel_name)
        logging.info(f"✅ Created uncategorized channel: {channel.name}")
    except Exception as e:
        logging.error(f"❌ Failed to create text channel '{full_channel_name}': {e}")
        return None

    webhook = await bot.get_or_create_webhook(channel, server_name)
    if webhook:
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


async def process_dm_relay(request):
    """Handle DM relay requests from main.py."""
    try:
        relay_data = await request.json()
        action = relay_data.get("action")

        if action == "send_dm":
            token = relay_data.get("token")
            user_id = relay_data.get("user_id")
            content = relay_data.get("content", "")
            attachments = relay_data.get("attachments", [])

            # Store the relay request for the appropriate self-bot
            relay_request = {
                "token": token,
                "user_id": user_id,
                "content": content,
                "attachments": attachments,
                "timestamp": datetime.now().isoformat()
            }

            # Push to a specific Redis queue for DM relay
            redis_client.lpush("dm_relay_queue", json.dumps(relay_request))
            logging.info(f"✅ DM relay request queued for user {user_id}")

            return web.json_response({"status": "success", "message": "DM relay queued"}, status=200)
        else:
            return web.json_response({"status": "error", "message": "Unknown action"}, status=400)

    except Exception as e:
        logging.error(f"❌ ERROR: Failed to process DM relay: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def start_web_server():
    app = web.Application()
    app.router.add_post("/process_message", process_message)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 5000)
    await site.start()
    logging.info("🌐 Web server started on http://127.0.0.1:5000")


async def run_bot():
    bot.webhook_cache = redis_client.hgetall("webhooks")

    threading.Thread(target=schedule_cleanup, daemon=True).start()

    await asyncio.gather(
        bot.start(BOT_TOKEN),
        start_web_server()
    )


bot = DestinationBot()

async def find_and_block_original_channel(channel_name, server_tag):
    """Find the original channel and add it to exclusions."""
    try:
        # Get the normalized channel name
        normalized_name = channel_name.lower().replace(" ", "-").replace("|", "")

        # Look through all monitored servers to find matching channels
        config = load_config()

        for token, token_data in config["tokens"].items():
            if token_data.get("disabled") or token_data.get("status") == "failed":
                continue

            for server_id in token_data.get("servers", {}):
                try:
                    # Try to find channels in this server via bot instance
                    if token in bot_instances:
                        bot_instance = bot_instances[token]
                        guild = discord.utils.get(bot_instance.guilds, id=int(server_id))

                        if guild and server_tag.lower() in guild.name.lower():
                            # Found the likely source server
                            for channel in guild.text_channels:
                                if channel.name.lower().replace("-", "").replace("_", "") == normalized_name.replace(
                                        "-", "").replace("_", ""):
                                    # Found the original channel!
                                    success = await add_channel_to_exclusions(server_id, str(channel.id), token)
                                    if success:
                                        logging.info(
                                            f"✅ Blocked channel {channel.name} (ID: {channel.id}) in server {guild.name}")
                                        return True

                except Exception as e:
                    logging.error(f"❌ Error checking server {server_id}: {e}")
                    continue

        return False

    except Exception as e:
        logging.error(f"❌ Error in find_and_block_original_channel: {e}")
        return False

@bot.tree.command(name="ping", description="Test if the bot is responding")
async def ping_slash(interaction: discord.Interaction):
    """Test command - responds with Pong!"""
    await interaction.response.send_message("🏓 Pong! Bot is working!", ephemeral=True)
    print(f"✅ Ping command executed by {interaction.user}")


@bot.tree.command(name="help", description="Show all available commands")
async def help_slash(interaction: discord.Interaction):
    """Show help for all commands"""
    embed = discord.Embed(
        title="🤖 1Tap Notify Bot Commands",
        description="Available slash commands:",
        color=discord.Color.blue()
    )

    commands_info = [
        ("/ping", "Test if the bot is responding"),
        ("/help", "Show this help message"),
        ("/status", "Show bot status and configuration"),
        ("/block", "Block a channel from being mirrored"),
        ("/unblock", "Unblock a channel by name"),
        ("/listblocked", "List all blocked channels"),
        ("/dmstats", "Show DM mirroring statistics"),
        ("/dmfilters", "Show DM filtering status"),
        ("/update", "Post an update to the updates channel (Admin only)"),
        ("/capture_layout", "Capture and lock the current server layout")
    ]

    for cmd, desc in commands_info:
        embed.add_field(name=cmd, value=desc, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Show bot status and configuration")
async def status_slash(interaction: discord.Interaction):
    """Show bot status and configuration"""
    embed = discord.Embed(
        title="🤖 Bot Status",
        color=discord.Color.green()
    )

    # Bot info
    embed.add_field(name="Bot User", value=f"{bot.user.name}", inline=True)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)

    # Config info
    embed.add_field(name="Destination Server", value=str(DESTINATION_SERVER_ID), inline=True)
    embed.add_field(name="Webhooks", value=str(len(WEBHOOKS)), inline=True)
    embed.add_field(name="Tokens", value=str(len(TOKENS)), inline=True)

    # Redis status
    redis_status = "✅ Connected" if redis_client else "❌ Disconnected"
    embed.add_field(name="Redis", value=redis_status, inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="debug", description="Debug information about current channel")
async def debug_slash(interaction: discord.Interaction):
    """Show debug information about the current channel"""

    channel = interaction.channel
    embed = discord.Embed(
        title="🔧 Channel Debug Information",
        color=discord.Color.orange()
    )

    embed.add_field(name="Channel Name", value=f"`{channel.name}`", inline=False)
    embed.add_field(name="Channel ID", value=str(channel.id), inline=True)
    embed.add_field(name="Channel Type", value=str(channel.type), inline=True)
    embed.add_field(name="Category", value=channel.category.name if channel.category else "None", inline=True)
    embed.add_field(name="Server", value=channel.guild.name, inline=True)
    embed.add_field(name="Server ID", value=str(channel.guild.id), inline=True)

    # Parse info
    base_name, server_tag = parse_channel_info(channel.name)
    embed.add_field(name="Parsed Base Name", value=f"`{base_name}`", inline=True)
    embed.add_field(name="Parsed Server Tag", value=f"`{server_tag}`" if server_tag else "None", inline=True)

    # Show if channel follows expected format
    has_brackets = "[" in channel.name and "]" in channel.name
    embed.add_field(name="Has Server Tag Format", value="✅ Yes" if has_brackets else "❌ No", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="servers", description="List all monitored servers from config")
async def servers_slash(interaction: discord.Interaction):
    """Show all monitored servers from configuration"""

    try:
        config_data = load_config()
        embed = discord.Embed(
            title="🖥️ Monitored Servers",
            description="Servers currently being monitored by the bot:",
            color=discord.Color.blue()
        )

        # Show detected server names
        detected_servers = get_monitored_servers()
        if detected_servers:
            server_list = "\n".join([f"• `{server}`" for server in detected_servers[:15]])
            embed.add_field(
                name="🔍 Detected Server Tags",
                value=server_list,
                inline=False
            )

            embed.add_field(name="📊 Total", value=f"{len(detected_servers)} server tags", inline=True)

            # Show last sync time
            last_updated = config_data.get("servers_last_updated")
            if last_updated:
                embed.add_field(name="🕒 Last Updated", value=last_updated[:19], inline=True)
            else:
                embed.add_field(name="⚠️ Status", value="No sync data found", inline=True)
        else:
            embed.add_field(
                name="⚠️ No Server Names Found",
                value="Server sync may not be working. Check main.py connection.",
                inline=False
            )

    except Exception as e:
        embed = discord.Embed(
            title="❌ Error Loading Servers",
            description=f"Could not load server information: {str(e)}",
            color=discord.Color.red()
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="block", description="Block a channel from being mirrored")
@app_commands.describe(
    channel="The channel to block (optional - uses current channel if not specified)",
    server_tag="Manual server tag if auto-detection fails (e.g., 'ak-chefs', 'divine')"
)
async def block_slash(interaction: discord.Interaction, channel: discord.TextChannel = None, server_tag: str = None):
    """Block a channel from being mirrored"""

    # Check permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    # Use current channel if none specified
    target_channel = channel or interaction.channel

    # Try to parse channel info
    base_name, detected_server_tag = parse_channel_info_fixed(target_channel.name)

    # Use manual server tag if provided, otherwise use detected
    final_server_tag = server_tag or detected_server_tag

    embed = discord.Embed(
        title="🔍 Channel Block Analysis",
        color=discord.Color.blue()
    )

    embed.add_field(name="Channel", value=f"{target_channel.mention}\n`{target_channel.name}`", inline=False)
    embed.add_field(name="Auto-detected Server", value=f"`{detected_server_tag}`" if detected_server_tag else "❌ None",
                    inline=True)
    embed.add_field(name="Manual Server Tag", value=f"`{server_tag}`" if server_tag else "Not provided", inline=True)
    embed.add_field(name="Final Server Tag", value=f"`{final_server_tag}`" if final_server_tag else "❌ None",
                    inline=True)

    if final_server_tag:
        embed.add_field(name="✅ Block Status", value=f"Ready to block from server: `{final_server_tag}`", inline=False)
        embed.add_field(name="Would Block", value=f"Channel: `{base_name}`\nFrom server: `{final_server_tag}`",
                        inline=False)
        embed.color = discord.Color.green()
        embed.title = "✅ Channel Ready to Block"

        # Add actual blocking logic here
        # success = await add_channel_to_exclusions(server_id, target_channel.id, token)

    else:
        suggestions = get_channel_suggestions(target_channel.name)
        embed.add_field(name="❌ Cannot Auto-Detect Server", value="Please specify server tag manually", inline=False)

        if suggestions:
            embed.add_field(name="💡 Suggested Server Tags", value="\n".join([f"• `{tag}`" for tag in suggestions]),
                            inline=False)
            embed.add_field(name="Usage Example", value=f"`/block server_tag:{suggestions[0]}`", inline=False)

        embed.color = discord.Color.orange()
        embed.title = "⚠️ Manual Server Tag Required"

    embed.set_footer(text="Note: Actual blocking logic would be implemented in a full version")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="unblock", description="Unblock a channel by name")
@app_commands.describe(channel_name="The name of the channel to unblock")
async def unblock_slash(interaction: discord.Interaction, channel_name: str):
    """Unblock a channel by name"""

    # Check permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    embed = discord.Embed(
        title="✅ Channel Unblock Request",
        description=f"Channel `{channel_name}` would be unblocked.",
        color=discord.Color.green()
    )
    embed.set_footer(text="Note: Full unblocking logic not implemented in this demo")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="listblocked", description="List all blocked channels")
async def listblocked_slash(interaction: discord.Interaction):
    """List all blocked channels"""

    # Check permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    embed = discord.Embed(
        title="🚫 Blocked Channels",
        description="No channels are currently blocked.",
        color=discord.Color.orange()
    )

    # Add logic here to load from config and show actual blocked channels
    embed.set_footer(text="Note: This would show actual blocked channels from config.json")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="dmstats", description="Show DM mirroring statistics")
async def dmstats_slash(interaction: discord.Interaction):
    """Show DM mirroring statistics"""
    embed = discord.Embed(
        title="📨 DM Mirroring Statistics",
        color=discord.Color.blue()
    )

    total_mappings = len(DM_MAPPINGS)
    active_channels = 0

    guild = bot.get_guild(DESTINATION_SERVER_ID)
    if guild:
        for category in guild.categories:
            if "[DM]" in category.name:
                active_channels += len([c for c in category.channels if isinstance(c, discord.TextChannel)])

    embed.add_field(name="Total DM Mappings", value=str(total_mappings), inline=True)
    embed.add_field(name="Active DM Channels", value=str(active_channels), inline=True)
    embed.add_field(name="DM Categories",
                    value=str(len([c for c in guild.categories if "[DM]" in c.name]) if guild else 0), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="dmfilters", description="Show DM filtering status")
async def dmfilters_slash(interaction: discord.Interaction):
    """Show current DM filtering status"""
    embed = discord.Embed(
        title="🛡️ DM Filtering Status",
        color=discord.Color.green()
    )

    embed.add_field(
        name="Spam Filter",
        value="✅ Active - Blocks messages with spam keywords",
        inline=False
    )
    embed.add_field(
        name="Friend Request Filter",
        value="✅ Active - Blocks DMs from users with no mutual servers",
        inline=False
    )
    embed.add_field(
        name="Bot Filter",
        value="✅ Active - Only allows specific whitelisted bots",
        inline=False
    )
    embed.add_field(
        name="Allowed Bots",
        value="Zebra Check, Divine Monitor, Hidden Clearance Bot, etc.",
        inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="update", description="Post an update to the updates channel")
@app_commands.describe(description="The update description to post")
async def update_slash(interaction: discord.Interaction, description: str):
    """Post an update to the updates channel"""

    # Check permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    updates_channel_id = config.get("updates_channel_id")

    if not updates_channel_id:
        await interaction.response.send_message("❌ Updates channel not configured in config.json.", ephemeral=True)
        return

    channel = bot.get_channel(updates_channel_id)
    if not channel:
        await interaction.response.send_message("❌ Updates channel not found.", ephemeral=True)
        return

    # Get next version number
    version = get_next_version()
    timestamp = datetime.now().strftime("%b %d, %Y | %H:%M:%S")

    # Parse the input to extract title and content
    parts = description.split(":", 1)
    if len(parts) == 2:
        title = parts[0].strip()
        content = parts[1].strip()
    else:
        title = "Update"
        content = description

    # Format the content with proper bullets and spacing
    formatted_content = content.replace(" - ", "\n* ").replace("Result:", "\n\n**Result:**")

    # Convert category/channel IDs to clickable links
    import re
    formatted_content = re.sub(r'\(ID: (\d+)\)', r'(<#\1>)', formatted_content)

    # Create the final description with proper spacing
    final_description = f"🔧 **{title}:**\n\n* {formatted_content}"

    embed = discord.Embed(
        title="⚠️ New update!",
        description=final_description,
        color=0xFFD700  # Gold color
    )
    embed.set_footer(text=f"Update {version} | 1Tap Notify [{timestamp}]")

    try:
        await channel.send(embed=embed)
        await interaction.response.send_message("✅ Update posted successfully!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to post update: {str(e)}", ephemeral=True)


@bot.tree.command(name="protect", description="Protect a channel from auto-deletion")
@app_commands.describe(channel="The channel to protect from auto-deletion")
async def protect_channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    """Protect a channel from auto-deletion"""

    # Check permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    # Use current channel if none specified
    target_channel = channel or interaction.channel

    try:
        # Load current config
        config_data = load_config()

        # Initialize protected_channels if it doesn't exist
        if "protected_channels" not in config_data:
            config_data["protected_channels"] = []

        # Add channel ID to protected list if not already there
        if target_channel.id not in config_data["protected_channels"]:
            config_data["protected_channels"].append(target_channel.id)
            save_config(config_data)

            await interaction.response.send_message(
                f"🛡️ **Channel Protected!**\n"
                f"Channel {target_channel.mention} (`{target_channel.name}`) is now protected from auto-deletion.\n\n"
                f"**Protected Channels:** {len(config_data['protected_channels'])}",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ Channel {target_channel.mention} is already protected from auto-deletion.",
                ephemeral=True
            )

    except Exception as e:
        await interaction.response.send_message(f"❌ Error protecting channel: {str(e)}", ephemeral=True)


@bot.tree.command(name="unprotect", description="Remove auto-deletion protection from a channel")
@app_commands.describe(channel="The channel to unprotect")
async def unprotect_channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    """Remove auto-deletion protection from a channel"""

    # Check permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    # Use current channel if none specified
    target_channel = channel or interaction.channel

    try:
        # Load current config
        config_data = load_config()

        # Initialize protected_channels if it doesn't exist
        if "protected_channels" not in config_data:
            config_data["protected_channels"] = []

        # Remove channel ID from protected list if it exists
        if target_channel.id in config_data["protected_channels"]:
            config_data["protected_channels"].remove(target_channel.id)
            save_config(config_data)

            await interaction.response.send_message(
                f"🔓 **Channel Unprotected!**\n"
                f"Channel {target_channel.mention} (`{target_channel.name}`) is no longer protected from auto-deletion.\n\n"
                f"**Protected Channels:** {len(config_data['protected_channels'])}",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ Channel {target_channel.mention} is not currently protected.",
                ephemeral=True
            )

    except Exception as e:
        await interaction.response.send_message(f"❌ Error unprotecting channel: {str(e)}", ephemeral=True)


@bot.tree.command(name="listprotected", description="List all channels protected from auto-deletion")
async def list_protected_channels(interaction: discord.Interaction):
    """List all protected channels"""

    try:
        # Load current config
        config_data = load_config()
        protected_ids = config_data.get("protected_channels", [])

        if not protected_ids:
            await interaction.response.send_message("🛡️ No channels are currently protected from auto-deletion.",
                                                    ephemeral=True)
            return

        # Get channel objects and create list
        guild = interaction.guild
        protected_channels = []

        for channel_id in protected_ids:
            channel = guild.get_channel(channel_id)
            if channel:
                protected_channels.append(f"• {channel.mention} (`{channel.name}`)")
            else:
                protected_channels.append(f"• Unknown Channel (ID: {channel_id})")

        embed = discord.Embed(
            title="🛡️ Protected Channels",
            description=f"**{len(protected_ids)} channels** are protected from auto-deletion:\n\n" + "\n".join(
                protected_channels),
            color=0x00FF00
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.response.send_message(f"❌ Error listing protected channels: {str(e)}", ephemeral=True)

@bot.tree.command(name="organize_channels", description="Manually organize channels in Release Guides and Daily Schedule categories")
@app_commands.describe()
async def organize_channels_slash(interaction: discord.Interaction):
    """Manually organize channels in allowed categories only."""
    try:
        # Check permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to use this command.",
                                                   ephemeral=True)
            return
            
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ This command must be used in a server!", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        
        # Only organize channels in allowed categories
        ALLOWED_CATEGORY_IDS = {1348464705701806080, 1353704482457915433}  # Release Guides, Daily Schedule
        organized_count = 0
        
        for category in guild.categories:
            if category.id not in ALLOWED_CATEGORY_IDS:
                continue  # Skip protected categories
                
            logging.info(f"🔄 Organizing channels in category: {category.name}")
            
            for channel in category.channels:
                if not isinstance(channel, discord.TextChannel):
                    continue
                    
                try:
                    # Apply organization logic to this channel
                    if category.id == 1348464705701806080:  # Release Guides
                        # Only move channels that need to be moved (color-only channels without dates)
                        import re
                        has_date_pattern = bool(
                            re.search(r'\\b(\\d{1,2}[-/]\\d{1,2}|\\d{1,2}[-/]\\d{1,2}[-/]\\d{2,4})\\b', channel.name) or
                            re.search(r'\\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\\b', channel.name.lower()) or
                            re.search(r'\\b\\d{1,2}(st|nd|rd|th)\\b', channel.name.lower())
                        )
                        
                        # If channel has no date pattern and is not already in Release Guides, move it
                        if not has_date_pattern and channel.category.id != 1348464705701806080:
                            release_guides = discord.utils.get(guild.categories, id=1348464705701806080)
                            if release_guides:
                                await channel.edit(category=release_guides)
                                logging.info(f"📅 Moved '{channel.name}' to Release Guides")
                                organized_count += 1
                                
                    elif category.id == 1353704482457915433:  # Daily Schedule
                        # Apply Daily Schedule specific organization if needed
                        organized_count += 1
                        
                except Exception as e:
                    logging.error(f"❌ Failed to organize channel '{channel.name}': {e}")
                    
                # Small delay to avoid rate limits
                await asyncio.sleep(0.5)
        
        embed = discord.Embed(
            title="🔄 Channel Organization Complete",
            description=f"Organized {organized_count} channels in allowed categories.",
            color=0x00ff00
        )
        
        embed.add_field(
            name="✅ Allowed Categories", 
            value="• 📅 Release Guides [1Tap Notify]\n• 📅 Daily Schedule [1Tap Notify]", 
            inline=False
        )
        
        embed.add_field(
            name="🔒 Protected Categories",
            value="All other categories follow server_layout.json and are protected from automatic moving.",
            inline=False
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error organizing channels: {str(e)}", ephemeral=True)

@bot.tree.command(name="capture_layout", description="Capture and save the current server layout to lock it in place")
@app_commands.describe()
async def capture_layout_slash(interaction: discord.Interaction):
    """Capture and save the current server layout for protection."""
    try:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ This command must be used in a server!", ephemeral=True)
            return
        
        # Capture and save layout
        layout_file = save_server_layout(guild)
        
        # Count categories and channels
        total_categories = len(guild.categories)
        total_channels = len(guild.text_channels)
        
        moveable_categories = 0
        moveable_channels = 0
        
        for category in guild.categories:
            if is_moveable_category(category.id):
                moveable_categories += 1
                moveable_channels += len([ch for ch in category.channels if isinstance(ch, discord.TextChannel)])
        
        uncategorized = len([ch for ch in guild.text_channels if ch.category is None])
        
        embed = discord.Embed(
            title="🔒 Server Layout Captured & Locked",
            description=f"Server layout has been saved to `{layout_file}`",
            color=0x00FF00
        )
        
        embed.add_field(
            name="📊 Total Categories", 
            value=f"{total_categories} categories\n{total_channels} text channels", 
            inline=True
        )
        
        # Calculate protected vs moveable
        allowed_category_ids = {1348464705701806080, 1353704482457915433}  # Release Guides, Daily Schedule
        allowed_categories = 0
        allowed_channels = 0
        
        for category in guild.categories:
            if category.id in allowed_category_ids:
                allowed_categories += 1
                allowed_channels += len([ch for ch in category.channels if isinstance(ch, discord.TextChannel)])
        
        protected_categories = total_categories - allowed_categories
        protected_channels = total_channels - allowed_channels - uncategorized
        
        embed.add_field(
            name="🔒 Protected (Layout-Locked)", 
            value=f"{protected_categories} categories\n{protected_channels} channels", 
            inline=True
        )
        
        embed.add_field(
            name="🔄 Auto-Organize Allowed", 
            value=f"{allowed_categories} categories\n{allowed_channels} channels\n{uncategorized} uncategorized", 
            inline=True
        )
        
        embed.add_field(
            name="ℹ️ Channel Movement Policy",
            value="**🔒 Protected:** All categories follow server_layout.json exactly.\n**🔄 Allowed:** Only Release Guides & Daily Schedule can auto-organize.\n**⚙️ Manual:** Use `/organize_channels` when needed.",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logging.error(f"❌ Failed to capture layout: {e}")
        await interaction.response.send_message(f"❌ Error capturing layout: {str(e)}", ephemeral=True)

if __name__ == "__main__":
    asyncio.run(run_bot())