"""Enhanced utilities module with comprehensive helper functions and type safety."""

import re
import hashlib
import time
import json
import asyncio
from typing import Optional, Dict, Any, List, Union, Callable, TypeVar
from datetime import datetime, timezone
import discord

# Import our custom types
from .types import (
    MessageData, MessageType, UserId, ServerId, ChannelId, MessageId,
    TokenStr, ConfigDict, UserInfo, ServerInfo
)
from .logger import structured_logger, performance_monitor
from .memory_manager import memory_manager

T = TypeVar('T')

def get_command_info(bot) -> str:
    """Get formatted command information from bot."""
    return "\n".join(f"{cmd.name} - {cmd.help}" for cmd in bot.commands)

def normalize_channel_name(name: str) -> str:
    """Normalize channel name for consistent handling."""
    if not isinstance(name, str):
        return str(name)
    
    # Remove emojis and special characters, replace with hyphens
    normalized = re.sub(r'[^\w\s-]', '', name.lower())
    normalized = re.sub(r'[\s_]+', '-', normalized.strip())
    return normalized[:100]  # Discord channel name limit

def extract_server_keywords(channel_name: str) -> List[str]:
    """Extract server-identifying keywords from channel name."""
    keywords = []
    
    # Common server identifiers
    patterns = [
        r'\[(\w+)\]',  # [server]
        r'-(\w+)$',     # channel-server
        r'^(\w+)-',     # server-channel
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, channel_name.lower())
        keywords.extend(matches)
    
    return list(set(keywords))  # Remove duplicates

def clean_message_content(content: str, max_length: int = 2000) -> str:
    """Clean and truncate message content."""
    if not isinstance(content, str):
        return str(content)
    
    # Remove excessive whitespace
    cleaned = re.sub(r'\s+', ' ', content.strip())
    
    # Truncate if too long
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length-3] + "..."
    
    return cleaned

def validate_discord_id(discord_id: Union[str, int]) -> Optional[int]:
    """Validate and convert Discord ID to integer."""
    try:
        id_int = int(discord_id)
        # Discord IDs are 64-bit integers, typically 17-19 digits
        if 10**16 <= id_int <= 10**19:
            return id_int
    except (ValueError, TypeError):
        pass
    return None

def is_forwarded_message(message: discord.Message) -> bool:
    """Detect if message is forwarded using multiple heuristics."""
    content = message.content.lower() if message.content else ""
    
    # Method 1: Check for forwarding indicators
    forwarding_indicators = [
        "forwarded from",
        "originally from",
        "shared from",
        "via @"
    ]
    
    if any(indicator in content for indicator in forwarding_indicators):
        return True
    
    # Method 2: Check for cross-server references
    if message.reference and hasattr(message.reference, 'guild_id'):
        if message.reference.guild_id != message.guild.id:
            return True
    
    # Method 3: Empty message with quoted content
    if not content and message.reference:
        return True
    
    return False

def extract_forwarded_info(message: discord.Message) -> Dict[str, Optional[str]]:
    """Extract forwarding information from message."""
    info = {"forwarded_from": None, "original_content": None}
    
    if not is_forwarded_message(message):
        return info
    
    content = message.content if message.content else ""
    
    # Extract forwarded from information
    patterns = [
        r"forwarded from\s+([^\n]+)",
        r"originally from\s+([^\n]+)",
        r"via\s+@([^\s]+)"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            info["forwarded_from"] = match.group(1).strip()
            break
    
    return info

def format_user_mention(user: Union[discord.User, discord.Member]) -> str:
    """Format user mention consistently."""
    if hasattr(user, 'global_name') and user.global_name:
        return user.global_name
    elif hasattr(user, 'display_name') and user.display_name:
        return user.display_name
    else:
        return str(user).replace("#0", "")

def create_message_hash(message: discord.Message) -> str:
    """Create unique hash for message deduplication."""
    # Use content, author, and timestamp for hash
    hash_data = f"{message.id}:{message.author.id}:{message.content}:{message.created_at}"
    return hashlib.md5(hash_data.encode()).hexdigest()

def convert_discord_message(message: discord.Message) -> MessageData:
    """Convert Discord message to our standardized MessageData format."""
    # Determine message type
    msg_type = MessageType.REGULAR
    if message.guild is None:
        msg_type = MessageType.DM
    elif is_forwarded_message(message):
        msg_type = MessageType.FORWARDED
    
    # Extract forwarding info if applicable
    forwarded_info = extract_forwarded_info(message)
    
    # Process attachments
    attachments = [att.url for att in message.attachments] if message.attachments else []
    
    # Process embeds
    embeds = [embed.to_dict() for embed in message.embeds] if message.embeds else []
    
    # Process role mentions
    mentioned_roles = {}
    if hasattr(message, 'role_mentions') and message.role_mentions:
        mentioned_roles = {str(role.id): role.name for role in message.role_mentions}
    
    return MessageData(
        message_id=message.id,
        channel_id=message.channel.id,
        channel_name=message.channel.name,
        server_id=message.guild.id if message.guild else 0,
        server_name=message.guild.name if message.guild else "DM",
        author_id=message.author.id,
        author_name=format_user_mention(message.author),
        content=clean_message_content(message.content or ""),
        timestamp=message.created_at.isoformat(),
        message_type=msg_type,
        category_name=message.channel.category.name if hasattr(message.channel, 'category') and message.channel.category else None,
        author_avatar=str(message.author.avatar.url) if message.author.avatar else None,
        attachments=attachments,
        embeds=embeds,
        mentioned_roles=mentioned_roles,
        is_forwarded=msg_type == MessageType.FORWARDED,
        forwarded_from=forwarded_info.get("forwarded_from"),
        processed_at=datetime.now(timezone.utc)
    )

def is_spam_content(content: str) -> bool:
    """Detect spam content using pattern matching."""
    if not content:
        return False
    
    content_lower = content.lower()
    
    # Spam indicators
    spam_patterns = [
        r'(https?://\S+){3,}',  # Multiple links
        r'([!@#$%^&*]){5,}',    # Excessive symbols
        r'(.)\1{10,}',          # Repeated characters
        r'\b(free|win|prize|money|cash)\b.*\b(now|today|click)\b',  # Spam keywords
    ]
    
    for pattern in spam_patterns:
        if re.search(pattern, content_lower):
            return True
    
    return False

def should_exclude_channel(channel_id: ChannelId, server_config: Dict[str, Any]) -> bool:
    """Check if channel should be excluded from monitoring."""
    excluded_channels = server_config.get("excluded_channels", [])
    return channel_id in excluded_channels

def should_exclude_category(category_id: ChannelId, server_config: Dict[str, Any]) -> bool:
    """Check if category should be excluded from monitoring."""
    excluded_categories = server_config.get("excluded_categories", [])
    return category_id in excluded_categories

def get_rate_limit_delay(error: Exception) -> Optional[float]:
    """Extract rate limit delay from Discord API error."""
    if hasattr(error, 'retry_after'):
        return float(error.retry_after)
    
    # Parse from error message if available
    error_str = str(error).lower()
    if 'rate limited' in error_str or 'retry after' in error_str:
        # Try to extract number
        import re
        match = re.search(r'(\d+(?:\.\d+)?)\s*seconds?', error_str)
        if match:
            return float(match.group(1))
    
    return None

async def retry_with_backoff(func: Callable, max_retries: int = 3, base_delay: float = 1.0) -> Any:
    """Retry function with exponential backoff."""
    for attempt in range(max_retries):
        try:
            if asyncio.iscoroutinefunction(func):
                return await func()
            else:
                return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            
            # Check for rate limit
            delay = get_rate_limit_delay(e)
            if delay is None:
                delay = base_delay * (2 ** attempt)  # Exponential backoff
            
            structured_logger.warning(
                f"Retry attempt {attempt + 1}/{max_retries} failed, waiting {delay}s",
                error_type=type(e).__name__,
                error_message=str(e),
                delay=delay
            )
            
            await asyncio.sleep(delay)
    
    raise RuntimeError("Max retries exceeded")

def safe_json_loads(json_str: str, default: Any = None) -> Any:
    """Safely parse JSON string with fallback."""
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return default

def safe_json_dumps(obj: Any, default: str = "{}") -> str:
    """Safely serialize object to JSON with fallback."""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return default

def measure_execution_time(func: Callable) -> Callable:
    """Decorator to measure function execution time."""
    def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            execution_time = time.time() - start_time
            performance_monitor.record_metric(
                f"{func.__name__}_execution_time",
                execution_time
            )
            return result
        except Exception as e:
            execution_time = time.time() - start_time
            performance_monitor.record_metric(
                f"{func.__name__}_error_time",
                execution_time
            )
            raise e
    return wrapper

async def async_measure_execution_time(func: Callable) -> Callable:
    """Async decorator to measure function execution time."""
    async def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = await func(*args, **kwargs)
            execution_time = time.time() - start_time
            performance_monitor.record_metric(
                f"{func.__name__}_execution_time",
                execution_time
            )
            return result
        except Exception as e:
            execution_time = time.time() - start_time
            performance_monitor.record_metric(
                f"{func.__name__}_error_time",
                execution_time
            )
            raise e
    return wrapper

# Cached utility functions (memory manager integration optional)
def validate_server_permissions(server_id: ServerId) -> bool:
    """Validate server permissions."""
    # Implement server permission validation logic
    return True  # Placeholder

def validate_user_permissions(user_id: UserId, server_id: ServerId) -> bool:
    """Validate user permissions in server."""
    # Implement user permission validation logic
    return True  # Placeholder

def get_memory_usage() -> Dict[str, float]:
    """Get current memory usage statistics."""
    import psutil
    import os
    
    process = psutil.Process(os.getpid())
    memory_info = process.memory_info()
    
    return {
        "rss_mb": memory_info.rss / 1024 / 1024,
        "vms_mb": memory_info.vms / 1024 / 1024,
        "percent": process.memory_percent()
    }

def format_uptime(start_time: float) -> str:
    """Format uptime duration in human-readable format."""
    uptime_seconds = time.time() - start_time
    
    if uptime_seconds < 60:
        return f"{uptime_seconds:.1f}s"
    elif uptime_seconds < 3600:
        minutes = uptime_seconds // 60
        seconds = uptime_seconds % 60
        return f"{int(minutes)}m {int(seconds)}s"
    else:
        hours = uptime_seconds // 3600
        minutes = (uptime_seconds % 3600) // 60
        return f"{int(hours)}h {int(minutes)}m"

# Export commonly used functions
__all__ = [
    'get_command_info', 'normalize_channel_name', 'extract_server_keywords',
    'clean_message_content', 'validate_discord_id', 'is_forwarded_message',
    'extract_forwarded_info', 'format_user_mention', 'create_message_hash',
    'convert_discord_message', 'is_spam_content', 'should_exclude_channel',
    'should_exclude_category', 'retry_with_backoff', 'safe_json_loads',
    'safe_json_dumps', 'measure_execution_time', 'async_measure_execution_time',
    'get_memory_usage', 'format_uptime'
]
