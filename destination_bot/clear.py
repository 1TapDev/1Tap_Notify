import redis

redis_client = redis.Redis(host="localhost", port=6379, db=0)
redis_client.delete("message_queue")  # Deletes the queue
print("✅ Redis message queue cleared!")
