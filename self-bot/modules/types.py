"""
Type definitions and data models for the 1Tap Notify system.
Provides comprehensive type hints for better code safety and IDE support.
"""

from typing import Dict, List, Optional, Union, Any, Callable, TypeVar, Generic
from typing import Protocol, runtime_checkable
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import discord

# Type variables
T = TypeVar('T')
K = TypeVar('K')
V = TypeVar('V')

# Basic type aliases
UserId = int
ServerId = int
ChannelId = int
MessageId = int
TokenStr = str
ConfigDict = Dict[str, Any]

class MessageType(Enum):
    """Types of messages processed by the system."""
    REGULAR = "regular"
    DM = "dm"
    FORWARDED = "forwarded"
    SYSTEM = "system"
    BATCH = "batch"

class ServerStatus(Enum):
    """Status of server monitoring."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    FAILED = "failed"
    EXCLUDED = "excluded"

class BotStatus(Enum):
    """Status of bot instances."""
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"

@dataclass
class UserInfo:
    """Discord user information."""
    id: UserId
    name: str
    discriminator: str = "0"
    last_successful_login: Optional[str] = None
    avatar_url: Optional[str] = None

@dataclass
class ServerInfo:
    """Discord server information."""
    id: ServerId
    name: str
    status: ServerStatus = ServerStatus.ACTIVE
    excluded_categories: List[ChannelId] = field(default_factory=list)
    excluded_channels: List[ChannelId] = field(default_factory=list)
    message_count: int = 0
    last_message_at: Optional[datetime] = None

@dataclass
class TokenConfig:
    """Configuration for a Discord token."""
    disabled: bool = False
    status: str = "active"
    last_error: Optional[str] = None
    last_failed_attempt: Optional[str] = None
    user_info: Optional[UserInfo] = None
    servers: Dict[ServerId, ServerInfo] = field(default_factory=dict)
    dm_mirroring: Dict[str, Any] = field(default_factory=dict)

@dataclass
class MessageData:
    """Standardized message data structure."""
    message_id: MessageId
    channel_id: ChannelId
    channel_name: str
    server_id: ServerId
    server_name: str
    author_id: UserId
    author_name: str
    content: str
    timestamp: str
    message_type: MessageType = MessageType.REGULAR
    
    # Optional fields
    category_name: Optional[str] = None
    author_avatar: Optional[str] = None
    attachments: List[str] = field(default_factory=list)
    embeds: List[Dict[str, Any]] = field(default_factory=list)
    mentioned_roles: Dict[str, str] = field(default_factory=dict)
    
    # Forwarding information
    is_forwarded: bool = False
    forwarded_from: Optional[str] = None
    forwarded_attachments: List[str] = field(default_factory=list)
    
    # Reply information
    reply_to: Optional[str] = None
    reply_text: Optional[str] = None
    
    # Processing metadata
    processed_at: Optional[datetime] = None
    processing_time: Optional[float] = None
    correlation_id: Optional[str] = None

@dataclass
class DMMessageData:
    """DM-specific message data structure."""
    message_type: str = "dm"
    channel_real_name: str = ""
    server_real_name: str = ""
    destination_server_id: ServerId = 0
    dm_user_id: UserId = 0
    dm_username: str = ""
    self_user_id: UserId = 0
    self_username: str = ""
    receiving_token: TokenStr = ""
    sender_user_id: UserId = 0
    is_bot: bool = False
    bot_name: Optional[str] = None

@dataclass
class CacheStats:
    """Cache performance statistics."""
    size: int
    max_size: int
    hits: int
    misses: int
    hit_rate_percent: float
    ttl_seconds: float

@dataclass
class PerformanceStats:
    """System performance statistics."""
    uptime_seconds: float
    messages_processed: int
    messages_per_second: float
    average_processing_time_ms: float
    error_count: int
    memory_usage_mb: float
    active_connections: int

@dataclass
class ErrorInfo:
    """Error information for tracking."""
    error_type: str
    message: str
    count: int
    first_seen: str
    last_seen: str
    context: Dict[str, Any] = field(default_factory=dict)

# Protocol definitions for duck typing
@runtime_checkable
class Cacheable(Protocol):
    """Protocol for cacheable objects."""
    
    def get_cache_key(self) -> str:
        """Return unique cache key for this object."""
        ...
    
    def is_expired(self) -> bool:
        """Check if object is expired."""
        ...

@runtime_checkable
class Serializable(Protocol):
    """Protocol for serializable objects."""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert object to dictionary."""
        ...
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Serializable':
        """Create object from dictionary."""
        ...

@runtime_checkable
class MessageProcessor(Protocol):
    """Protocol for message processors."""
    
    async def process_message(self, message: MessageData) -> bool:
        """Process a message and return success status."""
        ...
    
    def can_process(self, message: MessageData) -> bool:
        """Check if processor can handle this message."""
        ...

@runtime_checkable
class ConfigValidator(Protocol):
    """Protocol for configuration validators."""
    
    def validate(self, config: ConfigDict) -> List[str]:
        """Validate configuration and return list of errors."""
        ...

# Type aliases for complex types
MessageHandler = Callable[[MessageData], bool]
AsyncMessageHandler = Callable[[MessageData], Any]  # Returns awaitable
ConfigLoader = Callable[[], ConfigDict]
ErrorHandler = Callable[[Exception, Dict[str, Any]], None]

# Generic types for collections
MessageQueue = List[MessageData]
TokenMap = Dict[TokenStr, TokenConfig]
ServerMap = Dict[ServerId, ServerInfo]
ChannelMap = Dict[ChannelId, str]

# Function signature types
FilterFunction = Callable[[MessageData], bool]
TransformFunction = Callable[[MessageData], MessageData]
ValidatorFunction = Callable[[Any], bool]

# Event callback types
OnMessageCallback = Callable[[discord.Message], None]
OnErrorCallback = Callable[[Exception], None]
OnShutdownCallback = Callable[[], None]
OnConfigChangeCallback = Callable[[ConfigDict], None]

# Cache-related types
CacheKey = Union[str, int]
CacheValue = Any
CacheTTL = Union[int, float]

# HTTP-related types
HTTPMethod = str
HTTPHeaders = Dict[str, str]
HTTPResponse = Dict[str, Any]
WebhookPayload = Dict[str, Any]

# Configuration section types
@dataclass
class BotConfig:
    """Bot-specific configuration."""
    name: str
    version: str
    debug: bool = False
    log_level: str = "INFO"
    max_retries: int = 3
    retry_delay: float = 1.0

@dataclass
class DatabaseConfig:
    """Database configuration."""
    url: str
    max_connections: int = 10
    timeout: float = 30.0
    retry_attempts: int = 3

@dataclass
class RedisConfig:
    """Redis configuration."""
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    max_connections: int = 20
    timeout: float = 5.0

@dataclass
class PerformanceConfig:
    """Performance tuning configuration."""
    batch_size: int = 50
    batch_timeout: float = 5.0
    connection_pool_size: int = 100
    rate_limit_per_second: float = 50.0
    cache_ttl_seconds: int = 3600
    memory_limit_mb: int = 512

@dataclass
class MonitoringConfig:
    """Monitoring and alerting configuration."""
    enable_metrics: bool = True
    metrics_port: int = 8080
    health_check_interval: int = 60
    error_threshold: int = 100
    alert_webhooks: List[str] = field(default_factory=list)

# Union types for configuration validation
ConfigValue = Union[str, int, float, bool, List[Any], Dict[str, Any]]
ConfigSection = Dict[str, ConfigValue]

# Type guards for runtime type checking
def is_message_data(obj: Any) -> bool:
    """Check if object is MessageData instance."""
    return isinstance(obj, MessageData)

def is_valid_user_id(user_id: Any) -> bool:
    """Check if user_id is valid."""
    return isinstance(user_id, int) and user_id > 0

def is_valid_server_id(server_id: Any) -> bool:
    """Check if server_id is valid."""
    return isinstance(server_id, int) and server_id > 0

def is_valid_token(token: Any) -> bool:
    """Check if token is valid format."""
    return isinstance(token, str) and len(token) > 20

# Custom exception types with type annotations
class ConfigurationError(Exception):
    """Configuration-related errors."""
    
    def __init__(self, message: str, config_section: Optional[str] = None):
        super().__init__(message)
        self.config_section = config_section

class MessageProcessingError(Exception):
    """Message processing errors."""
    
    def __init__(self, message: str, message_data: Optional[MessageData] = None):
        super().__init__(message)
        self.message_data = message_data

class TokenAuthenticationError(Exception):
    """Token authentication errors."""
    
    def __init__(self, message: str, token: Optional[str] = None):
        super().__init__(message)
        self.token = token[:10] + "..." if token else None

class RateLimitError(Exception):
    """Rate limiting errors."""
    
    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after