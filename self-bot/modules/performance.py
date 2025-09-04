"""
Performance optimization module with batch processing, connection pooling, and async improvements.
Provides efficient message handling and HTTP request management.
"""

import asyncio
import aiohttp
import time
import json
from typing import List, Dict, Any, Optional, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict, deque
import weakref
import threading

@dataclass
class MessageBatch:
    """Batch of messages for efficient processing."""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    destination_url: str = ""
    max_size: int = 50
    max_age_seconds: float = 5.0
    
    def add_message(self, message: Dict[str, Any]) -> bool:
        """Add message to batch. Returns True if batch is ready for processing."""
        self.messages.append(message)
        return self.is_ready()
    
    def is_ready(self) -> bool:
        """Check if batch is ready for processing."""
        age = time.time() - self.created_at
        return len(self.messages) >= self.max_size or age >= self.max_age_seconds
    
    def is_expired(self, max_age: float = 30.0) -> bool:
        """Check if batch has expired."""
        return time.time() - self.created_at > max_age

class BatchProcessor:
    """Efficient batch processing for messages."""
    
    def __init__(self, max_batch_size: int = 50, max_batch_age: float = 5.0):
        self.max_batch_size = max_batch_size
        self.max_batch_age = max_batch_age
        self.batches: Dict[str, MessageBatch] = {}
        self.lock = asyncio.Lock()
        self.processing_queue = asyncio.Queue(maxsize=1000)
        self._processor_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Statistics
        self.stats = {
            "messages_processed": 0,
            "batches_processed": 0,
            "processing_time_total": 0.0,
            "last_processed": None
        }
    
    async def start(self):
        """Start batch processor."""
        if self._running:
            return
        
        self._running = True
        self._processor_task = asyncio.create_task(self._process_batches())
        self._cleanup_task = asyncio.create_task(self._cleanup_expired_batches())
    
    async def stop(self):
        """Stop batch processor and process remaining batches."""
        self._running = False
        
        # Process any remaining batches
        async with self.lock:
            for batch_key, batch in list(self.batches.items()):
                if batch.messages:
                    await self.processing_queue.put((batch_key, batch))
            self.batches.clear()
        
        # Cancel tasks
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
    
    async def add_message(self, message: Dict[str, Any], destination_url: str):
        """Add message to appropriate batch."""
        batch_key = destination_url
        
        async with self.lock:
            if batch_key not in self.batches:
                self.batches[batch_key] = MessageBatch(
                    destination_url=destination_url,
                    max_size=self.max_batch_size,
                    max_age_seconds=self.max_batch_age
                )
            
            batch = self.batches[batch_key]
            is_ready = batch.add_message(message)
            
            if is_ready:
                # Move batch to processing queue
                await self.processing_queue.put((batch_key, batch))
                del self.batches[batch_key]
    
    async def _process_batches(self):
        """Process batches from the queue."""
        while self._running:
            try:
                # Wait for batch with timeout
                batch_key, batch = await asyncio.wait_for(
                    self.processing_queue.get(), 
                    timeout=1.0
                )
                
                start_time = time.time()
                await self._send_batch(batch)
                processing_time = time.time() - start_time
                
                # Update statistics
                self.stats["messages_processed"] += len(batch.messages)
                self.stats["batches_processed"] += 1
                self.stats["processing_time_total"] += processing_time
                self.stats["last_processed"] = datetime.now().isoformat()
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"❌ Batch processing error: {e}")
                await asyncio.sleep(1)
    
    async def _cleanup_expired_batches(self):
        """Cleanup expired batches periodically."""
        while self._running:
            try:
                current_time = time.time()
                expired_keys = []
                
                async with self.lock:
                    for batch_key, batch in self.batches.items():
                        if batch.is_expired():
                            expired_keys.append(batch_key)
                            if batch.messages:  # Process if has messages
                                await self.processing_queue.put((batch_key, batch))
                    
                    for key in expired_keys:
                        if key in self.batches:
                            del self.batches[key]
                
                await asyncio.sleep(10)  # Check every 10 seconds
                
            except Exception as e:
                print(f"❌ Batch cleanup error: {e}")
                await asyncio.sleep(30)
    
    async def _send_batch(self, batch: MessageBatch):
        """Send batch of messages to destination."""
        if not batch.messages:
            return
        
        try:
            # Use connection pool for efficient HTTP requests
            async with connection_pool.get_session() as session:
                payload = {
                    "batch": True,
                    "messages": batch.messages,
                    "batch_size": len(batch.messages),
                    "created_at": batch.created_at
                }
                
                async with session.post(
                    batch.destination_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status != 200:
                        print(f"⚠️ Batch send failed: HTTP {response.status}")
                        # Could implement retry logic here
                    else:
                        print(f"✅ Batch sent: {len(batch.messages)} messages")
                        
        except Exception as e:
            print(f"❌ Failed to send batch: {e}")
            # Could implement dead letter queue here
    
    def get_stats(self) -> Dict[str, Any]:
        """Get processing statistics."""
        avg_processing_time = 0
        if self.stats["batches_processed"] > 0:
            avg_processing_time = self.stats["processing_time_total"] / self.stats["batches_processed"]
        
        return {
            **self.stats,
            "avg_processing_time_ms": round(avg_processing_time * 1000, 2),
            "active_batches": len(self.batches),
            "queue_size": self.processing_queue.qsize()
        }

class ConnectionPool:
    """HTTP connection pool for efficient request handling."""
    
    def __init__(self, max_connections: int = 100, max_connections_per_host: int = 30):
        self.connector = aiohttp.TCPConnector(
            limit=max_connections,
            limit_per_host=max_connections_per_host,
            keepalive_timeout=300,
            enable_cleanup_closed=True
        )
        self.session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        
        # Statistics
        self.stats = {
            "requests_made": 0,
            "connection_reuses": 0,
            "connection_errors": 0,
            "response_times": deque(maxlen=1000)  # Keep last 1000 response times
        }
    
    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self.session is None or self.session.closed:
            async with self._lock:
                if self.session is None or self.session.closed:
                    timeout = aiohttp.ClientTimeout(total=60, connect=10)
                    self.session = aiohttp.ClientSession(
                        connector=self.connector,
                        timeout=timeout,
                        headers={"User-Agent": "1TapNotify-Bot/1.0"}
                    )\n        return self.session
    
    async def close(self):
        """Close connection pool."""
        if self.session and not self.session.closed:
            await self.session.close()
        if self.connector:
            await self.connector.close()
    
    async def request(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        """Make HTTP request with connection pooling."""
        start_time = time.time()
        
        try:
            session = await self.get_session()
            response = await session.request(method, url, **kwargs)
            
            # Update statistics
            self.stats["requests_made"] += 1
            response_time = time.time() - start_time
            self.stats["response_times"].append(response_time)
            
            return response
            
        except aiohttp.ClientError as e:
            self.stats["connection_errors"] += 1
            raise e
    
    def get_stats(self) -> Dict[str, Any]:
        """Get connection pool statistics."""
        response_times = list(self.stats["response_times"])
        avg_response_time = sum(response_times) / len(response_times) if response_times else 0
        
        return {
            "requests_made": self.stats["requests_made"],
            "connection_errors": self.stats["connection_errors"],
            "avg_response_time_ms": round(avg_response_time * 1000, 2),
            "active_connections": len(self.connector._conns) if hasattr(self.connector, '_conns') else 0,
            "pool_size": self.connector.limit
        }

class RateLimiter:
    """Token bucket rate limiter for API calls."""
    
    def __init__(self, rate: float = 10.0, burst: int = 20):
        self.rate = rate  # tokens per second
        self.burst = burst  # max tokens in bucket
        self.tokens = float(burst)
        self.last_update = time.time()
        self.lock = asyncio.Lock()
    
    async def acquire(self, tokens: int = 1) -> bool:
        """Acquire tokens from the bucket."""
        async with self.lock:
            now = time.time()
            
            # Add tokens based on elapsed time
            elapsed = now - self.last_update
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_update = now
            
            # Check if we have enough tokens
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            
            return False
    
    async def wait_for_tokens(self, tokens: int = 1):
        """Wait until tokens are available."""
        while not await self.acquire(tokens):
            await asyncio.sleep(0.1)

class PerformanceMonitor:
    """Monitor and track performance metrics."""
    
    def __init__(self):
        self.metrics: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self.counters: Dict[str, int] = defaultdict(int)
        self.start_time = time.time()
    
    def record_metric(self, name: str, value: float):
        """Record a performance metric."""
        self.metrics[name].append((time.time(), value))
    
    def increment_counter(self, name: str, value: int = 1):
        """Increment a counter metric."""
        self.counters[name] += value
    
    def get_metric_stats(self, name: str) -> Dict[str, float]:
        """Get statistics for a metric."""
        values = [v for _, v in self.metrics[name]]
        if not values:
            return {"count": 0, "avg": 0, "min": 0, "max": 0}
        
        return {
            "count": len(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values)
        }
    
    def get_summary(self) -> Dict[str, Any]:
        """Get comprehensive performance summary."""
        uptime = time.time() - self.start_time
        
        return {
            "uptime_seconds": uptime,
            "counters": dict(self.counters),
            "metrics": {
                name: self.get_metric_stats(name)
                for name in self.metrics.keys()
            }
        }

# Global instances
batch_processor = BatchProcessor()
connection_pool = ConnectionPool()
performance_monitor = PerformanceMonitor()
rate_limiter = RateLimiter(rate=50.0, burst=100)  # 50 requests per second, burst of 100

async def optimized_send_message(message_data: Dict[str, Any], destination_url: str):
    """Send message using batch processing and connection pooling."""
    try:
        # Add to batch processor
        await batch_processor.add_message(message_data, destination_url)
        performance_monitor.increment_counter("messages_queued")
        
    except Exception as e:
        performance_monitor.increment_counter("message_queue_errors")
        print(f"❌ Failed to queue message: {e}")
        
        # Fallback to direct sending
        await direct_send_message(message_data, destination_url)

async def direct_send_message(message_data: Dict[str, Any], destination_url: str):
    """Send message directly (fallback method)."""
    start_time = time.time()
    
    try:
        # Wait for rate limit
        await rate_limiter.wait_for_tokens()
        
        # Send using connection pool
        session = await connection_pool.get_session()
        async with session.post(destination_url, json=message_data) as response:
            if response.status == 200:
                performance_monitor.increment_counter("messages_sent_success")
            else:
                performance_monitor.increment_counter("messages_sent_failed")
                
        processing_time = time.time() - start_time
        performance_monitor.record_metric("message_send_time", processing_time)
        
    except Exception as e:
        performance_monitor.increment_counter("direct_send_errors")
        print(f"❌ Direct send failed: {e}")

# Cleanup function for graceful shutdown
async def cleanup_performance_components():
    """Clean up all performance components."""
    print("🔄 Shutting down performance components...")
    
    await batch_processor.stop()
    await connection_pool.close()
    
    print("✅ Performance components shut down successfully")