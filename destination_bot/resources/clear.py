import redis

redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# Clear all keys (optional if you want a clean slate)
# redis_client.flushall()

# Clear specific keys
redis_client.delete("webhooks")
redis_client.delete("message_queue")

print("✅ Redis cleared: webhooks and message_queue.")
