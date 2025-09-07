# Discord Notification System - Developer Documentation

## Project Overview

This is a Discord Self-Bot Message Mirroring and Notification System designed to monitor multiple Discord servers simultaneously and aggregate important messages into a centralized destination. The system is specifically built for sneaker/trading communities to capture time-sensitive notifications while filtering out spam and irrelevant content.

## Architecture & System Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    DISCORD NOTIFICATION SYSTEM                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Discord API   │◄──►│    main.py      │◄──►│   Redis Queue   │
│  (6 User Tokens)│    │   Self-Bot      │    │   (Messages)    │
│                 │    │   Controller    │    │                 │
│ • 1tapsneakers  │    │                 │    │ • Queue: msg    │
│ • nulll         │    │ ┌─────────────┐ │    │ • Queue: dm     │
│ • frogmon       │    │ │DM Relay     │ │    │ • Queue: relay  │
│ • sneakerbot_   │    │ │Service      │ │    │                 │
│ • tupapa1931    │    │ │Port 5001    │ │    │                 │
│ • aquilini      │    │ └─────────────┘ │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘
                               │
                               ▼
                       ┌─────────────────┐
                       │     bot.py      │
                       │  (Destination   │
                       │   Bot/Webhook   │
                       │   Processor)    │
                       │   Port 5000     │
                       └─────────────────┘
```

## File Structure & Core Components

### 1. main.py - Self-Bot Controller
**Purpose:** Multi-token Discord self-bot manager that monitors servers, processes messages, and handles DM mirroring.

**Key Classes:**
- `MirrorSelfBot(discord.Client)` - Core self-bot implementation

**Core Functions:**
- `on_message()` - Main message handler with forwarded message detection
- `handle_dm_message()` - DM-specific processing with spam filtering
- `handle_server_message()` - Server message processing with exclusion logic
- `send_to_destination()` - HTTP POST to bot.py with retry logic
- `start_dm_relay_service()` - HTTP server on port 5001 for DM relay

**Message Processing Pipeline:**
```python
# 1. Message Reception & Filtering
async def on_message(self, message):
    # Skip self-messages and destination bot messages
    # Route DMs vs server messages
    # Apply 0.5s delay for Discord attachment processing

# 2. Server Message Filtering
# Check if server is monitored
# Apply excluded categories filter
# Apply excluded channels filter
# Log acceptance/rejection

# 3. Forwarded Message Detection (3 methods)
# Method 1: Discord native forwarding (cross-guild references)
# Method 2: Empty message quoting another message
# Method 3: Text pattern matching ("forwarded from", "originally from")
```

**Message Data Structure:**
```json
{
    "reply_to": "ORIGINAL_AUTHOR",
    "reply_text": "ORIGINAL_CONTENT_SNIPPET",
    "channel_real_name": "CHANNEL_NAME",
    "server_real_name": "SERVER_NAME", 
    "mentioned_roles": {"ROLE_ID": "ROLE_NAME"},
    "message_id": "MESSAGE_ID",
    "channel_id": "CHANNEL_ID",
    "channel_name": "CHANNEL_NAME",
    "category_name": "CATEGORY_NAME",
    "server_id": "SERVER_ID",
    "server_name": "SERVER_NAME",
    "content": "MESSAGE_CONTENT",
    "author_id": "AUTHOR_ID",
    "author_name": "AUTHOR_DISPLAY_NAME",
    "author_avatar": "AVATAR_URL",
    "timestamp": "MESSAGE_TIMESTAMP",
    "attachments": ["ATTACHMENT_URLS"],
    "forwarded_from": "ORIGINAL_AUTHOR",
    "embeds": [EMBED_OBJECTS],
    "forwarded_attachments": ["FORWARDED_URLS"],
    "is_forwarded": Boolean
}
```

### 2. bot.py - Destination Processor
**Purpose:** Receives messages from main.py via Redis queue and HTTP endpoints, processes them, and delivers to Discord webhooks/channels.

**Key Features:**
- Discord bot with slash commands
- Redis queue processing
- Webhook management and cleanup
- Channel combining functionality via `/combine_channels` command
- DM relay integration

**Slash Commands:**
```python
/ping - Test bot responsiveness
/help - Show all available commands
/status - Show bot status and configuration
/block - Block a channel from being mirrored
/unblock - Unblock a channel by name
/listblocked - List all blocked channels
/dmstats - Show DM mirroring statistics
/dmfilters - Show DM filtering status
/update - Post updates to designated channel (Admin only)
/combine_channels - Combine multiple channels into one master channel
```

**Message Processing:**
- Handles regular messages, DMs, and forwarded content
- Manages duplicate detection via message ID tracking
- Processes embeds and attachments
- Implements channel blocking/unblocking
- Manages webhook cleanup for deleted channels

### 3. config.json - Configuration Management
**Structure:**
```json
{
    "tokens": {
        "TOKEN_BASE64": {
            "disabled": false,
            "dm_mirroring": {
                "enabled": true,
                "destination_server_id": "SERVER_ID"
            },
            "servers": {
                "SERVER_ID": {
                    "excluded_categories": [CATEGORY_IDS],
                    "excluded_channels": [CHANNEL_IDS]
                }
            },
            "user_info": {
                "id": "USER_ID",
                "name": "USERNAME",
                "discriminator": "0",
                "last_successful_login": "ISO_TIMESTAMP"
            },
            "status": "active/failed",
            "last_error": "ERROR_MESSAGE",
            "last_failed_attempt": "ISO_TIMESTAMP"
        }
    },
    "destination_server": "DESTINATION_SERVER_ID",
    "webhooks": {},
    "settings": {
        "message_delay": 0.75,
        "max_login_attempts": 3
    },
    "dm_mappings": {}
}
```

**Configured Users:**
- 1tapsneakers#0 (ID: 1041771944040742953) - Status: Active
- nulll#0 (ID: 175339853725040640) - Status: Active
- frogmon#0 (ID: 461486060074041375) - Status: Failed
- sneakerbot_#0 (ID: 612926994333564938) - Status: Active
- tupapa1931#0 (ID: 388953081078874113) - Status: Active
- aquilini#0 (ID: 392816175492497418) - Status: Active

## DM Mirroring System

**DM Filtering Logic:**
```python
def should_allow_dm(message):
    # Allow DMs from users in monitored servers
    # Block spam messages (keywords, links, emojis)
    # Block friend request DMs (no mutual servers)
    # Allow specific authorized bots
```

**Authorized Bot Patterns:**
- "zebra check", "divine monitor", "hidden clearance bot"
- "monitor", "ticket tool", "notification", "alert"
- "checker", "1tap", "sneaker", "cook"

**DM Message Structure:**
```json
{
    "message_type": "dm",
    "channel_real_name": "dm-{NORMALIZED_USERNAME}",
    "server_real_name": "@{SELF_USERNAME} [DM]",
    "destination_server_id": "DESTINATION_ID",
    "dm_user_id": "SENDER_ID",
    "dm_username": "SENDER_DISPLAY_NAME",
    "self_user_id": "RECEIVER_ID", 
    "self_username": "RECEIVER_DISPLAY_NAME",
    "receiving_token": "TOKEN",
    "sender_user_id": "SENDER_ID",
    "is_bot": Boolean,
    "bot_name": "BOT_NAME"
}
```

## Services & Communication

### 1. DM Relay Service (Port 5001)
```python
async def start_dm_relay_service():
    # HTTP server for handling DM relay requests
    # POST /send_dm - Send DM via specific token
    # POST /request_sync - Trigger server sync
```

### 2. Redis Queue Management
- Message queue: "message_queue"
- DM relay queue: "dm_relay_queue"  
- Bot instances data: "bot_instances"

### 3. HTTP Communication
```python
DESTINATION_BOT_URL = "http://127.0.0.1:5000/process_message"
# POST requests with message data
# Retry logic with exponential backoff
# Connection error handling
```

## Error Handling & Resilience

### 1. Connection Issues
- SSL certificate bypass for Discord API
- Automatic reconnection with exponential backoff
- Max login attempts tracking (default: 3)
- Failed token marking and exclusion

### 2. Token Management
```python
def mark_token_as_failed(token, error_message):
    # Update config.json with failure status
    # Record error message and timestamp
    # Exclude from future startup attempts
```

### 3. Graceful Degradation
- Continue operation if Redis unavailable
- Skip problematic servers/channels
- Log errors without crashing
- Session cleanup on shutdown

## Monitoring Configuration

### Monitored Server Types
Based on excluded categories and channels, the system monitors:

**Sneaker Release Servers:**
- Release notifications
- Restock alerts
- Drop announcements
- Monitor bot feeds

**Trading/Cook Groups:**
- Group buys
- Trading channels
- Success shares
- Tool notifications

**Community Servers:**
- Excludes: general chat, off-topic, social categories
- Includes: announcements, releases, important updates

### Exclusion Strategy
- **Excluded Categories:** Social, general discussion, voice channels, admin areas
- **Excluded Channels:** Specific high-noise channels, bot commands, testing areas
- **Included Content:** Time-sensitive notifications, releases, trading, alerts

## Data Flow & Processing

### 1. Message Ingestion
```
Discord Message → on_message() → Filtering → Data Structure → Redis Queue
```

### 2. Queue Processing
```
Redis Queue → bot.py → Webhook/Channel → Destination Server
```

### 3. DM Relay
```
bot.py Request → DM Relay Service → Token Selection → Discord API → DM Sent
```

### 4. Error Recovery
```
Connection Lost → Reconnection Attempt → Exponential Backoff → Resume Monitoring
```

## Current Known Issues & Fixes

### 1. SSL Certificate Errors
**Issue:** Self-signed certificate issues with Discord
**Solution:** SSL context bypass implemented in main.py

### 2. Forwarded Message Detection
**Issue:** Reply messages incorrectly identified as forwarded messages
**Solution:** Enhanced detection logic to differentiate between replies and forwards

### 3. Double Image Issue
**Issue:** Images appear twice (as attachment and embed)  
**Status:** REVERTED - Automatic attachment-to-embed conversion removed
**Solution:** Images now only appear as attachments, embeds only process Discord's native embeds

### 4. All-Staff-Pings Embed Issue  
**Issue:** ❗┃all-staff-pings-divine channel not showing embed messages properly
**Root Cause:** Duplicate image fix affected legitimate embed processing
**Status:** FIXED by reverting automatic embed generation

### 5. AttributeError Issues
**Issue:** `mutual_guilds` not available on User objects
**Solution:** Proper error handling and fallback logic

## Enhanced Module System

### New Modules (self-bot/modules/)

**performance.py** - Performance monitoring and metrics collection
```python
# Performance decorators for function monitoring
@performance_monitor
def my_function():
    pass

@async_performance_monitor  
async def async_function():
    pass

# Usage tracking
perf_logger.log_function_performance(func_name, duration, **kwargs)
perf_logger.log_message_processing(server_id, count, time)
```

**memory_manager.py** - Memory usage monitoring and cleanup
```python
class MemoryManager:
    def get_memory_usage()           # Current memory stats
    def monitor_memory()             # Background monitoring
    def cleanup_large_objects()      # Automatic cleanup
    def get_memory_report()          # Detailed report
```

**shutdown_handler.py** - Graceful shutdown management
```python
class ShutdownHandler:
    def register_cleanup_function()  # Register cleanup callbacks
    def initiate_shutdown()          # Start graceful shutdown
    def emergency_shutdown()         # Force shutdown
```

**types.py** - Type definitions and data structures
```python
# Enhanced type hints for better code organization
MessageData = TypedDict('MessageData', {...})
DMMapping = TypedDict('DMMapping', {...})
PerformanceMetrics = TypedDict('PerformanceMetrics', {...})
```

**logger.py** - Enhanced structured logging system
```python
# New structured logger with correlation tracking
structured_logger.info("Message", correlation_id="abc123", **kwargs)

# Performance logging
perf_logger = PerformanceLogger()

# Error aggregation
error_aggregator.record_error(error_type, message, context)
```

### Enhanced Features

**Configuration File Watching:**
- Real-time config.json monitoring with watchdog
- Automatic configuration reload without restart
- Server list updates propagated to all bot instances

**Structured Logging:**
- JSON-formatted logs with correlation IDs
- Unicode support with proper encoding
- Performance metrics tracking
- Error aggregation and reporting

**Memory Management:**
- Automatic memory monitoring and cleanup
- Large object detection and removal
- Memory usage reporting and alerts

**Graceful Shutdown:**
- Signal handling for clean termination
- Resource cleanup on shutdown
- Session management and proper disconnection

## Development Setup

### Dependencies
```python
discord.py       # Discord API interaction
aiohttp          # Async HTTP client
redis            # Message queue
asyncio          # Async runtime
ssl              # Certificate handling
json             # Configuration management
logging          # Error tracking
re               # Pattern matching
watchdog         # File system monitoring
psutil           # System/process utilities
typing           # Type hints
threading        # Thread management
contextlib       # Context managers
functools        # Function utilities
traceback        # Error tracing
uuid             # Unique identifiers
Pillow (PIL)     # Image processing and compression
io               # In-memory file operations
```

### Infrastructure Requirements
- Redis Server (localhost:6379)
- Python 3.8+ with asyncio support
- Network Access to Discord API
- File System for logging and configuration

### External Services
- Discord API (multiple user tokens)
- bot.py (destination processor on port 5000)
- Webhooks/Channels (final message destination)

## Recent Changes & Updates

### Version History
- **Latest Commit:** feat(compression): add intelligent image compression for large files
- **Previous Commit:** feat(channels): restrict automatic channel moving to specific categories
- **Key Updates:**
  - Intelligent image compression system with progressive optimization
  - Restricted channel management to only 2 specific categories
  - Server layout protection for all other categories
  - Enhanced slash commands for manual channel organization
  - Enhanced structured logging system with Unicode support
  - Performance monitoring and memory management modules
  - Configuration file watching with automatic reload
  - Graceful shutdown handling
  - Type definitions for better code organization
  - Reverted duplicate image fix to restore embed functionality

### File Changes Summary
- **Modified:** `self-bot/main.py` - Reverted automatic attachment-to-embed conversion
- **Modified:** `destination_bot/bot.py` - Added image compression and restricted channel management
- **New:** `self-bot/modules/performance.py` - Performance monitoring system
- **New:** `self-bot/modules/memory_manager.py` - Memory usage tracking
- **New:** `self-bot/modules/shutdown_handler.py` - Graceful shutdown management
- **New:** `self-bot/modules/types.py` - Type definitions
- **Enhanced:** `self-bot/modules/logger.py` - Structured logging with correlation tracking

### Known Working Features
- ✅ Message mirroring from monitored servers
- ✅ DM mirroring with spam filtering
- ✅ Forwarded message detection (3 methods)
- ✅ Reply handling with gray bars
- ✅ Role mention mapping
- ✅ Intelligent image compression for large files
- ✅ Restricted channel moving (Release Guides & Daily Schedule only)
- ✅ Server layout protection for all other categories
- ✅ Native Discord embed processing
- ✅ Channel combining functionality
- ✅ Configuration file hot-reloading
- ✅ Performance monitoring and logging

### Current Status
- ✅ Image compression system working and crash-proof
- ✅ Channel movement restricted to 2 specific categories only
- ✅ Server layout protection implemented and active
- ✅ All-staff-pings embed messages displaying properly

## Operational Procedures

### Startup Sequence
1. Load configuration from config.json
2. Connect to Redis server
3. Initialize SSL context for Discord connections
4. Start configuration file watcher
5. Initialize performance monitoring and structured logging
6. Start DM relay service (port 5001)
7. Launch self-bot instances for each active token
8. Stagger bot launches (5-second intervals)
9. Begin message monitoring and processing

### Shutdown Procedure
1. Graceful signal handling (SIGINT/SIGTERM)
2. Close aiohttp sessions
3. Disconnect Discord clients
4. Flush Redis queues
5. Save final configuration state

### Error Recovery
- **Token Failures:** Mark as failed, exclude from restart
- **Connection Issues:** Exponential backoff reconnection
- **SSL Problems:** Certificate bypass and retry
- **Rate Limits:** Respect Discord rate limiting

### Monitoring & Logging
- **File Logging:** Detailed logs to `logs/main_YYYY-MM-DD_HH-MM-SS.log`
- **Console Output:** Critical errors only
- **Redis Queues:** Message count monitoring
- **Connection Status:** Real-time connection health

## Image Compression System

The system includes intelligent image compression for handling large files that exceed Discord's size limits.

### Implementation
```python
def compress_image(image_data, filename, max_size=MAX_DISCORD_FILE_SIZE, quality=85):
    # Progressive quality reduction: 85% → 70% → 55% → 40% → 25% → 10%
    # Intelligent resizing: 2048px → 1024px → smaller
    # Format optimization: PNG/GIF → JPEG for better compression
    # Multi-step approach with error handling
```

### Key Features
- **Progressive Compression:** Quality reduction with size monitoring
- **Intelligent Resizing:** Maintains aspect ratio while reducing dimensions
- **Format Optimization:** Converts to JPEG for maximum compression
- **Transparency Handling:** White background for transparent images
- **Multi-format Support:** JPG, PNG, GIF, WebP, BMP
- **Error Handling:** Robust PIL processing with multiple fallbacks
- **Safety Limits:** Max 8 attempts to prevent infinite loops

### Compression Behavior
**Before:** `⚠️ File too large, skipping: image0.jpg`
**After:** `🗜️ Image compressed: image0.jpg (15MB → 2.1MB, -86.0%, quality=55)`

## Channel Management System

The system implements restricted automatic channel moving with server layout protection.

### Protected Channel Management
```python
# Only these categories allow automatic channel moving
ALLOWED_CATEGORY_IDS = {1348464705701806080, 1353704482457915433}  # Release Guides, Daily Schedule

async def monitor_allowed_categories_only():
    # Process only channels in allowed categories
    # All other categories respect server_layout.json
```

### Slash Commands
```python
/organize_channels - Manual channel organization (admin only)
/capture_layout - Capture and protect server layout
/protect - Protect specific channels from deletion
/unprotect - Remove channel protection
```

### Key Features
- **Restricted Movement:** Only Release Guides and Daily Schedule categories
- **Layout Protection:** All other categories follow server_layout.json exactly
- **Manual Control:** Admin-only commands for organization
- **Protection System:** Individual channel protection from auto-deletion

## Channel Combining Feature

The system includes advanced channel combining functionality that allows multiple source channels to be routed to a single master channel in the destination server.

### Implementation
```python
@bot.tree.command(name="combine_channels")
async def combine_channels(interaction: discord.Interaction, ...):
    # Create channel mappings in config
    # Notify main.py via Redis
    # Route future messages to master channel
```

### Key Benefits
- Reduces channel clutter in destination server
- Groups related content (e.g., all release channels → master-releases)
- Maintains source attribution
- Configurable via slash commands

## Security & Compliance

### Rate Limiting
- 0.75-second delay between message processing
- Exponential backoff on connection failures
- Request timeout handling (10 seconds)

### Privacy Protection
- User ID anonymization options
- DM content filtering
- Spam/malicious content blocking

### Token Security
- Base64 encoded token storage
- Failed token automatic disabling
- Login attempt limiting

---

This comprehensive system provides robust, scalable Discord message monitoring with intelligent filtering, DM management, reliable message delivery, and advanced channel management capabilities.