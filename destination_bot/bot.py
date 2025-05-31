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
import threading
import requests
import re
from discord.ext import commands
from discord.ext import tasks
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
MAX_DISCORD_FILE_SIZE = 8 * 1024 * 1024  # 8MB
# Connect to Redis
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# Global cache to track recent message_ids and prevent duplicates
recent_message_ids = set()


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
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
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


async def monitor_for_archive():
    await bot.wait_until_ready()
    guild = bot.get_guild(DESTINATION_SERVER_ID)

    if not guild:
        logging.error("❌ Destination server not found in monitor_for_archive")
        return

    while not bot.is_closed():
        try:
            for channel in guild.text_channels:
                try:
                    messages = [message async for message in channel.history(limit=50)]
                    for msg in messages:
                        content_lower = msg.content.lower()
                        if "!archive" in content_lower or "archived to forum thread" in content_lower:
                            logging.info(f"🗑️ Archive command detected in '{channel.name}' → Message: {msg.content}")

                            server_name_raw = guild.name
                            category_name_raw = channel.category.name if channel.category else "Uncategorized"

                            server_name = server_name_raw.split(" [")[0].strip()
                            category_name = category_name_raw.split(" [")[0].strip()
                            logging.info(f"📂 Archive check: Server='{server_name}' | Category='{category_name}'")

                            forum_mappings = config.get("archived_forums", {})
                            normalized_category = normalize_category(category_name)
                            normalized_server = normalize_server_tag(server_name)
                            forum_target = forum_mappings.get(f"{normalized_category} [{normalized_server}]")

                            logging.info(f"🔍 Looking for: forum_mappings[{server_name}][{category_name}]")
                            logging.info(f"📦 Full forum mappings: {forum_mappings}")

                            if not forum_target:
                                logging.warning(f"⚠️ No forum mapping for '{category_name}' in server '{server_name}'")
                            else:
                                forum_channel = discord.utils.get(guild.forum_channels, name=forum_target)
                                if forum_channel:
                                    try:
                                        base_msg = await forum_channel.send(
                                            content=f"Auto-archived from #{channel.name}")
                                        thread = await forum_channel.create_thread(name=channel.name, message=base_msg)
                                        logging.info(f"✅ Created thread '{thread.name}' in forum '{forum_target}'")
                                    except Exception as e:
                                        logging.error(f"❌ Failed to create thread in forum '{forum_target}': {e}")
                                else:
                                    logging.warning(f"⚠️ Forum channel '{forum_target}' not found in guild")

                            await channel.delete(reason="!archive command triggered")
                            logging.info(f"✅ Deleted channel '{channel.name}'")

                            break  # Stop checking this channel after archiving
                except discord.Forbidden:
                    logging.warning(f"⚠️ No permission to access '{channel.name}'")
                except Exception as e:
                    logging.error(f"❌ Exception in monitor_for_archive for '{channel.name}': {e}")
            await asyncio.sleep(10)
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
                    message = json.loads(message_data)
                    if not isinstance(message, dict):
                        raise ValueError("Invalid message format, expected dict")
                    if "message_id" not in message:
                        raise ValueError("Missing required field: message_id")

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


async def clean_mentions(content: str, destination_guild: discord.Guild, message_data: dict) -> str:
    # Replace <#channel_id> with destination channel or fallback text
    channel_mentions = re.findall(r"<#(\d+)>", content)
    for channel_id in channel_mentions:
        original_channel = bot.get_channel(int(channel_id))
        original_name = message_data.get("channel_real_name", f"channel-{channel_id}")
        server_name = message_data.get("server_real_name", "Unknown Server")

        # Find a matching channel in the destination server by name
        matching_channel = discord.utils.get(destination_guild.text_channels, name=original_name)
        if matching_channel:
            # Replace with clickable destination channel
            content = content.replace(f"<#{channel_id}>", f"<#{matching_channel.id}>")
        else:
            # Replace with fallback text
            content = content.replace(f"<#{channel_id}>", f"`{server_name} > #{original_name}`")

    # Replace <@user_id> with @username#discriminator
    user_mentions = re.findall(r"<@!?(\d+)>", content)
    for user_id in user_mentions:
        try:
            user_obj = await bot.fetch_user(int(user_id))
            tag = f"<@{user_obj.id}>"
            content = re.sub(f"<@!?{user_id}>", tag, content)
        except Exception:
            content = content.replace(f"<@{user_id}>", "@unknown")

    # Replace <@&role_id> with matching role in destination or create it
    role_mentions = re.findall(r"<@&(\d+)>", content)
    source_role_map = message_data.get("mentioned_roles", {})

    for role_id in role_mentions:
        role_name = source_role_map.get(role_id, f"AutoRole-{role_id}")
        dest_role = discord.utils.get(destination_guild.roles, name=role_name)

        if not dest_role:
            # Create based on MEMBERS
            base_role = discord.utils.get(destination_guild.roles, name="MEMBERS")
            if base_role:
                dest_role = await destination_guild.create_role(
                    name=role_name,
                    permissions=base_role.permissions,
                    color=base_role.color,
                    hoist=False,
                    mentionable=True
                )
                logging.info(f"✅ Created role '{role_name}' based on MEMBERS")
            else:
                content = content.replace(f"<@&{role_id}>", f"@{role_name}")
                continue

        content = content.replace(f"<@&{role_id}>", f"<@&{dest_role.id}>")

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
    message_id = message_data.get("message_id")

    # Auto-delete if archive command detected
    archive_trigger = message_data.get("content", "").strip().lower()
    embed_title = (message_data.get("embed_title") or "").lower()
    embed_desc = (message_data.get("embed_description") or "").lower()

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
        else:
            logging.warning(f"⚠️ Channel not found in cache for archive delete: {message_data['channel_id']}")

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

    content = await clean_mentions(
        message_data.get("content", ""),
        bot.get_guild(DESTINATION_SERVER_ID),
        message_data
    )

    forwarded_text = f"> **Forwarded from @{message_data['forwarded_from']}**" if message_data.get(
        "forwarded_from") else ""
    if forwarded_text:
        content = f"{forwarded_text}\n{content}"

    if message_data.get("reply_to") and message_data.get("reply_text"):
        content = f"> **Replying to @{message_data['reply_to']}:** {message_data['reply_text']}\n{content}"
    elif message_data.get("reply_to"):
        content = f"> **Replying to @{message_data['reply_to']}**\n{content}"

    attachments = message_data.get("attachments", [])
    embeds = message_data.get("embeds", [])
    if not embeds:
        logging.warning(f"⚠️ No embeds received from main.py → message_id={message_id}")

    cleaned_embeds = []
    for embed in embeds:
        if not isinstance(embed, dict):
            continue
        try:
            embed_copy = embed.copy()

            # Check if embed has any meaningful field
            if not any([
                embed_copy.get("title"),
                embed_copy.get("description"),
                embed_copy.get("url"),
                embed_copy.get("image"),
                embed_copy.get("thumbnail"),
                embed_copy.get("fields")
            ]):
                logging.warning(f"⚠️ Embed skipped due to missing core fields:\n{json.dumps(embed_copy, indent=2)}")
                continue

            core_fields = [
                embed_copy.get("title"),
                embed_copy.get("description"),
                embed_copy.get("url"),
                embed_copy.get("image", {}).get("url") if isinstance(embed_copy.get("image"), dict) else embed_copy.get(
                    "image"),
                embed_copy.get("thumbnail", {}).get("url") if isinstance(embed_copy.get("thumbnail"),
                                                                         dict) else embed_copy.get("thumbnail"),
                embed_copy.get("fields")
            ]
            if not any(core_fields):
                logging.warning(f"⚠️ Embed skipped due to missing core fields:\n{json.dumps(embed_copy, indent=2)}")
                continue

            # Clean malformed image field if needed
            if "image" in embed_copy:
                if isinstance(embed_copy["image"], str):
                    embed_copy["image"] = {"url": embed_copy["image"]}
                elif isinstance(embed_copy["image"], dict) and "url" not in embed_copy["image"]:
                    embed_copy.pop("image")

            # Remove empty fields
            for key in list(embed_copy.keys()):
                if embed_copy[key] is None:
                    del embed_copy[key]

            embed_copy = await resolve_embed_mentions(embed_copy, bot.get_guild(DESTINATION_SERVER_ID), message_data)
            cleaned_embeds.append(embed_copy)

        except Exception as e:
            logging.warning(f"❌ Embed processing failed: {e}")

    files = []
    # ⏭️ Skip truly empty messages (no content, no embeds, no attachments)
    if not content.strip() and not cleaned_embeds and not attachments:
        logging.info(
            f"⏭️ Skipped empty message_id={message_data.get('message_id')} from {message_data.get('author_name')}")
        return

    # Split message if over 2000 characters
    parts = [content[i:i + 2000] for i in range(0, len(content), 2000)] if content else [""]

    # Download attachments to files (if any)
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
                            logging.warning(f"⚠️ File too large, skipping: {filename}")
        except Exception as e:
            logging.warning(f"⚠️ Failed to fetch attachment: {url} → {e}")

    async with aiohttp.ClientSession() as session:
        for part in parts:
            for attempt in range(3):
                try:
                    avatar_url = message_data.get("author_avatar")

                    payload = {
                        "username": message_data.get("author_name", "Unknown"),
                        "avatar_url": avatar_url
                    }

                    if content:
                        payload["content"] = content

                    if embeds:
                        payload["embeds"] = embeds

                    if "embeds" in payload:
                        logging.info(f"📤 Embeds included in payload: {json.dumps(payload['embeds'], indent=2)}")
                    else:
                        logging.warning("⚠️ Embeds were NOT included in final payload")

                    if files:
                        logging.info(f"📤 With files: {[f['filename'] for f in files]}")

                    if not payload.get("content"):
                        payload.pop("content", None)

                    if part == parts[0] and cleaned_embeds:
                        payload["embeds"] = cleaned_embeds
                    else:
                        payload.pop("embeds", None)

                    # If message only has image attachments, treat as file upload instead of just an embed
                    if not content.strip() and not cleaned_embeds and files:
                        parts = [""]  # Force sending file even if no text or embed

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

                        async with session.post(webhook_url, data=form) as response:
                            if response.status in (200, 204):
                                logging.info(f"✅ Webhook message sent successfully to {webhook_url}")
                                break  # only break on success
                            elif response.status == 404:
                                error_text = await response.text()
                                logging.error(f"❌ Webhook 404: {error_text}")
                                if "Unknown Webhook" in error_text:
                                    logging.warning(f"⚠️ Webhook deleted for {webhook_key}. Removing from config.")
                                    WEBHOOKS.pop(webhook_key, None)
                                    redis_client.hdel("webhooks", webhook_key)
                                    bot.save_config()
                                    webhook_url = await create_channel_and_webhook(category_name, channel_name,
                                                                                   server_name)
                                    if not webhook_url:
                                        return
                                elif "Unknown Channel" in error_text:
                                    logging.warning(
                                        f"⚠️ Channel '{channel_name}' no longer exists. Removing webhook + config for {webhook_key}.")
                                    WEBHOOKS.pop(webhook_key, None)
                                    redis_client.hdel("webhooks", webhook_key)
                                    bot.save_config()
                                    return
                            elif response.status >= 500:
                                logging.warning(f"⚠️ Discord error {response.status}, retry {attempt + 1}")
                                await asyncio.sleep(2 * (attempt + 1))
                            else:
                                error = await response.text()
                                logging.error(f"❌ Webhook file upload failed ({response.status}) → {error}")
                                return

                    # Fallback: JSON post without file
                    async with session.post(webhook_url, json=payload) as response:
                        if response.status in (200, 204):
                            break
                        elif response.status == 404:
                            error_text = await response.text()
                            if "Unknown Webhook" in error_text:
                                logging.warning(f"⚠️ Webhook deleted for {webhook_key}. Removing from config.")
                                WEBHOOKS.pop(webhook_key, None)
                                redis_client.hdel("webhooks", webhook_key)
                                bot.save_config()
                                webhook_url = await create_channel_and_webhook(category_name, channel_name, server_name)
                                if not webhook_url:
                                    return
                            elif "Unknown Channel" in error_text:
                                logging.warning(
                                    f"⚠️ Channel '{channel_name}' no longer exists. Removing webhook + config for {webhook_key}.")
                                WEBHOOKS.pop(webhook_key, None)
                                redis_client.hdel("webhooks", webhook_key)
                                bot.save_config()
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


class DestinationBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.reactions = True
        intents.members = True

        super().__init__(command_prefix="!", intents=intents)
        self.webhook_cache = WEBHOOKS
        self.event(self.on_ready)

    async def on_ready(self):
        print(f"✅ Bot {self.user} is running!")
        self.webhook_cache = redis_client.hgetall("webhooks")
        await self.ensure_webhooks()
        await self.migrate_channels_to_uncategorized()
        await self.populate_category_mappings()
        self.save_config()
        print("✅ Webhook setup complete. Bot is now processing messages.")
        asyncio.create_task(process_redis_messages())
        asyncio.create_task(monitor_for_archive())
        asyncio.create_task(self.monitor_channels_continuously())
        asyncio.create_task(self.monitor_deleted_channels())
        asyncio.create_task(self.cleanup_expired_channels())

    async def migrate_channels_to_uncategorized(self):
        guild = self.get_guild(DESTINATION_SERVER_ID)
        if not guild:
            logging.error("❌ Destination server not found for migration.")
            return

        ignored_tags = config.get("ignored_category_tags", [])
        uncategorized_category = None

        for category in guild.categories:
            # Skip if category has ignored tag
            if any(tag in category.name for tag in ignored_tags):
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

    def save_config(self):
        config["webhooks"] = self.webhook_cache
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)

    async def cleanup_expired_channels(self):
        """Check and delete expired channels in Daily Schedule and Release Guides categories."""
        await self.wait_until_ready()
        guild = self.get_guild(DESTINATION_SERVER_ID)

        while not self.is_closed():
            try:
                current_time = datetime.now()
                current_year = current_time.year

                for category in guild.categories:
                    # Daily Schedule - 24 hour expiration
                    if category.name.startswith("📅 Daily Schedule"):
                        for channel in category.channels:
                            if not isinstance(channel, discord.TextChannel):
                                continue

                            # Store creation time when channel is first seen
                            creation_key = f"channel_created_{channel.id}"
                            stored_time = redis_client.get(creation_key)

                            if not stored_time:
                                # First time seeing this channel, store its creation time
                                redis_client.setex(creation_key, 86400 * 2,
                                                   current_time.isoformat())  # Store for 48 hours
                                logging.info(f"📅 Tracking new daily channel: {channel.name}")
                            else:
                                # Check if 24 hours have passed
                                created_time = datetime.fromisoformat(stored_time)
                                time_elapsed = current_time - created_time

                                if time_elapsed >= timedelta(hours=24):
                                    try:
                                        await channel.delete(reason="Daily Schedule channel expired (24 hours)")
                                        redis_client.delete(creation_key)
                                        logging.info(
                                            f"🗑️ Deleted expired daily channel: {channel.name} (age: {time_elapsed})")
                                    except Exception as e:
                                        logging.error(f"❌ Failed to delete expired channel {channel.name}: {e}")

                    # Release Guides - 7 days expiration or past date
                    elif category.name.startswith("📅 Release Guides"):
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
                                # First time seeing this channel, store its creation time
                                redis_client.setex(creation_key, 86400 * 14,
                                                   current_time.isoformat())  # Store for 14 days
                                logging.info(f"📅 Tracking new release channel: {channel.name}")
                            else:
                                # Check if 7 days have passed
                                created_time = datetime.fromisoformat(stored_time)
                                time_elapsed = current_time - created_time

                                if time_elapsed >= timedelta(days=7):
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

    async def monitor_channels_continuously(self):
        await self.wait_until_ready()
        guild = self.get_guild(DESTINATION_SERVER_ID)

        months = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"
        ]

        while not self.is_closed():
            try:
                # 1. Sort channels in their categories
                for category in guild.categories:
                    if category.name.startswith("📅 Release Guides"):
                        logging.info(f"📁 Found Release Guides category: '{category.name}'")
                        await self.sort_channels_in_category(category, by="date")
                    elif category.name.startswith("📅 Daily Schedule"):
                        logging.info(f"📁 Found Daily Schedule category: '{category.name}'")
                        await self.sort_channels_in_category(category, by="time")

                # 2. Reroute uncategorized channels
                for channel in guild.text_channels:
                    name = channel.name.lower()
                    if channel.category is not None:
                        continue  # Skip already categorized channels

                    # Delete divine+month
                    if any(month in name for month in months) and "divine" in name:
                        try:
                            await channel.delete(reason="Month + divine based channel deleted")
                            logging.info(f"🗑️ Deleted channel '{channel.name}' (divine/month match)")
                            continue
                        except Exception as e:
                            logging.error(f"❌ Failed to delete '{channel.name}': {e}")

                    # Time reroute
                    if re.search(r"\b\d{1,2}(am|pm)\b", name):
                        for cat in guild.categories:
                            if cat.name.startswith("📅 Daily Schedule") and cat.name.endswith("]"):
                                try:
                                    await channel.edit(category=cat)
                                    server_tag_match = re.search(r'\[(.*?)\]', channel.name)
                                    if server_tag_match:
                                        server_tag = server_tag_match.group(1)
                                    source_channel_id = config["source_channel_ids"].get(server_tag)
                                    if source_channel_id:
                                        redis_client.hset("channel_monitoring", str(channel.id), str(source_channel_id))
                                    logging.info(f"📅 Moved '{channel.name}' to '{cat.name}'")
                                except Exception as e:
                                    logging.error(f"❌ Could not move '{channel.name}' to Daily Schedule: {e}")
                                break

                    # Date reroute
                    elif re.search(r"\b\d{1,2}-\d{1,2}\b", name):
                        for cat in guild.categories:
                            if cat.name.startswith("📅 Release Guides") and cat.name.endswith("]"):
                                try:
                                    await channel.edit(category=cat)
                                    server_tag_match = re.search(r'\[(.*?)\]', channel.name)
                                    if server_tag_match:
                                        server_tag = server_tag_match.group(1)
                                    source_channel_id = config["source_channel_ids"].get(server_tag)
                                    if source_channel_id:
                                        redis_client.hset("channel_monitoring", str(channel.id), str(source_channel_id))
                                    logging.info(f"📅 Moved '{channel.name}' to '{cat.name}'")
                                except Exception as e:
                                    logging.error(f"❌ Could not move '{channel.name}' to Release Guides: {e}")
                                break

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
        if category.name not in ["📅 Daily Schedule [1Tap Notify]", "📁 Release Guides [1Tap Notify]"]:
            logging.info(f"⛔ Skipping category '{category.name}' — not eligible for sorting.")
            return
        logging.info(f"🔃 Sorting '{category.name}' by {by} with {len(category.channels)} channels")

        def extract_sort_key(name):
            # Remove emojis and special characters
            clean_name = re.sub(r'[^\w\s:-]', '', name.lower())
            logging.info(f"🔎 Evaluating sort key for: {name} → cleaned: {clean_name}")

            if by == "date":
                # Match patterns like 4-17, 04-17, 4/17
                match = re.search(r'\b(\d{1,2})[-/](\d{1,2})\b', clean_name)
                if match:
                    try:
                        date_val = datetime.strptime(f"{match.group(1)}-{match.group(2)}", "%m-%d")
                        logging.info(f"✅ Parsed date for '{name}': {date_val}")
                        return date_val
                    except Exception as e:
                        logging.warning(f"⚠️ Failed to parse date for '{name}': {e}")

            elif by == "time":
                # Match 4pm, 11am, 5AM, etc.
                match = re.search(r'\b(\d{1,2})(am|pm)\b', clean_name)
                if match:
                    try:
                        time_val = datetime.strptime(match.group(0), "%I%p")
                        logging.info(f"✅ Parsed time for '{name}': {time_val}")
                        return time_val
                    except Exception as e:
                        logging.warning(f"⚠️ Failed to parse time for '{name}': {e}")

            logging.info(f"🔽 No valid sort key found for: {name}, pushing to bottom")
            return datetime.max


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


async def start_web_server():
    app = web.Application()
    app.router.add_post("/process_message", process_message)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 5000)
    await site.start()


async def run_bot():
    bot.webhook_cache = redis_client.hgetall("webhooks")

    threading.Thread(target=schedule_cleanup, daemon=True).start()

    await asyncio.gather(
        bot.start(BOT_TOKEN),
        start_web_server()
    )


bot = DestinationBot()


@bot.command()
async def update(ctx, *, description):
    updates_channel_id = config.get("updates_channel_id")
    channel = bot.get_channel(updates_channel_id)
    if not channel:
        await ctx.send("❌ Updates channel not configured or not found.")
        return

    version = get_next_version()
    timestamp = datetime.now().strftime("%b %d, %Y | %H:%M:%S")

    embed = discord.Embed(
        title="🔔 New update!",
        description=description.strip(),
        color=discord.Color.blurple()
    )
    embed.set_footer(text=f"Update {version} | 1Tap Notify [{timestamp}]")

    await channel.send(embed=embed)
    await ctx.send("✅ Update posted.")


if __name__ == "__main__":
    asyncio.run(run_bot())