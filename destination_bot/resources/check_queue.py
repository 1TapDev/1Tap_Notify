import redis
import json

# Connect to Memurai Redis instance
r = redis.Redis(host='localhost', port=6379, db=0)

# Get length of the queue
length = r.llen("message_queue")
print(f"📦 Total messages in queue: {length}\n")

# Show last 10 messages (from newest to oldest)
messages = r.lrange("message_queue", -10, -1)

for i, msg in enumerate(messages[::-1], start=1):
    try:
        data = json.loads(msg)
        author = data.get("author_name", "unknown")
        content = data.get("content", "[no content]")
        timestamp = data.get("timestamp")
        print(f"#{i} | {timestamp} | {author}: {content[:80]}")
    except Exception as e:
        print(f"#{i} | ❌ Failed to parse message: {e}")
