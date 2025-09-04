"""
Memory management module with LRU caches, TTL support, and resource monitoring.
Provides efficient caching and memory usage optimization.
"""

import time
import threading
import weakref
from typing import Any, Dict, Optional, TypeVar, Generic, Callable
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

T = TypeVar('T')

@dataclass
class CacheEntry:
    """Cache entry with value and metadata."""
    value: Any
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    access_count: int = 0

class TTLCache(Generic[T]):
    """Thread-safe LRU cache with TTL (Time To Live) support."""
    
    def __init__(self, max_size: int = 1000, ttl_seconds: float = 3600):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        
    def get(self, key: str, default: Optional[T] = None) -> Optional[T]:
        """Get item from cache with LRU and TTL checks."""
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return default
            
            entry = self._cache[key]
            
            # Check TTL
            if time.time() - entry.created_at > self.ttl_seconds:
                del self._cache[key]
                self._misses += 1
                return default
            
            # Update access info and move to end (most recently used)
            entry.accessed_at = time.time()
            entry.access_count += 1
            self._cache.move_to_end(key)
            self._hits += 1
            return entry.value
    
    def set(self, key: str, value: T) -> None:
        """Set item in cache with LRU eviction."""
        with self._lock:
            if key in self._cache:
                # Update existing entry
                self._cache[key].value = value
                self._cache[key].created_at = time.time()
                self._cache[key].accessed_at = time.time()
                self._cache.move_to_end(key)
            else:
                # Add new entry
                if len(self._cache) >= self.max_size:
                    # Remove least recently used
                    self._cache.popitem(last=False)
                
                self._cache[key] = CacheEntry(value=value)
    
    def delete(self, key: str) -> bool:
        """Delete item from cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
    
    def cleanup_expired(self) -> int:
        """Remove expired entries and return count of removed items."""
        current_time = time.time()
        expired_keys = []
        
        with self._lock:
            for key, entry in self._cache.items():
                if current_time - entry.created_at > self.ttl_seconds:
                    expired_keys.append(key)
            
            for key in expired_keys:
                del self._cache[key]
        
        return len(expired_keys)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0
            
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_percent": round(hit_rate, 2),
                "ttl_seconds": self.ttl_seconds
            }

class MemoryManager:
    """Centralized memory management for caches and resources."""
    
    def __init__(self):
        self.caches: Dict[str, TTLCache] = {}
        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_interval = 300  # 5 minutes
        self._running = False
        self._lock = threading.Lock()
        
        # Pre-configured caches for common use cases
        self.server_names = self.get_cache("server_names", max_size=500, ttl_seconds=3600)
        self.message_ids = self.get_cache("message_ids", max_size=10000, ttl_seconds=1800)
        self.channel_info = self.get_cache("channel_info", max_size=1000, ttl_seconds=1800)
        self.webhook_cache = self.get_cache("webhook_cache", max_size=200, ttl_seconds=7200)
    
    def get_cache(self, name: str, max_size: int = 1000, ttl_seconds: float = 3600) -> TTLCache:
        """Get or create a named cache."""
        with self._lock:
            if name not in self.caches:
                self.caches[name] = TTLCache(max_size=max_size, ttl_seconds=ttl_seconds)
            return self.caches[name]
    
    def start_cleanup_thread(self):
        """Start background cleanup thread."""
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            return
        
        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_worker, daemon=True)
        self._cleanup_thread.start()
    
    def stop_cleanup_thread(self):
        """Stop background cleanup thread."""
        self._running = False
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5)
    
    def _cleanup_worker(self):
        """Background worker to cleanup expired cache entries."""
        while self._running:
            try:
                total_cleaned = 0
                with self._lock:
                    for cache_name, cache in self.caches.items():
                        cleaned = cache.cleanup_expired()
                        total_cleaned += cleaned
                
                if total_cleaned > 0:
                    print(f"🧹 Cleaned {total_cleaned} expired cache entries")
                
                # Sleep for cleanup interval
                for _ in range(self._cleanup_interval):
                    if not self._running:
                        break
                    time.sleep(1)
                    
            except Exception as e:
                print(f"❌ Cache cleanup error: {e}")
                time.sleep(60)  # Wait before retrying
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """Get comprehensive memory usage statistics."""
        stats = {
            "total_caches": len(self.caches),
            "caches": {},
            "summary": {
                "total_entries": 0,
                "total_hits": 0,
                "total_misses": 0,
                "average_hit_rate": 0
            }
        }
        
        with self._lock:
            hit_rates = []
            for name, cache in self.caches.items():
                cache_stats = cache.get_stats()
                stats["caches"][name] = cache_stats
                stats["summary"]["total_entries"] += cache_stats["size"]
                stats["summary"]["total_hits"] += cache_stats["hits"]
                stats["summary"]["total_misses"] += cache_stats["misses"]
                
                if cache_stats["hit_rate_percent"] > 0:
                    hit_rates.append(cache_stats["hit_rate_percent"])
            
            if hit_rates:
                stats["summary"]["average_hit_rate"] = round(sum(hit_rates) / len(hit_rates), 2)
        
        return stats
    
    def clear_all_caches(self):
        """Clear all caches."""
        with self._lock:
            for cache in self.caches.values():
                cache.clear()
    
    def optimize_memory(self) -> Dict[str, int]:
        """Optimize memory usage by cleaning expired entries and resizing caches."""
        results = {"expired_cleaned": 0, "caches_resized": 0}
        
        with self._lock:
            # Clean expired entries
            for cache in self.caches.values():
                results["expired_cleaned"] += cache.cleanup_expired()
            
            # Resize caches that are underutilized
            for name, cache in self.caches.items():
                stats = cache.get_stats()
                if stats["size"] < stats["max_size"] * 0.5 and stats["max_size"] > 100:
                    # Reduce cache size if less than 50% utilized
                    new_size = max(100, stats["size"] * 2)
                    cache.max_size = new_size
                    results["caches_resized"] += 1
        
        return results

class ResourceTracker:
    """Track resource usage and detect memory leaks."""
    
    def __init__(self):
        self._tracked_objects: Dict[str, weakref.WeakSet] = {}
        self._creation_counts: Dict[str, int] = {}
        self._lock = threading.Lock()
    
    def track_object(self, obj: Any, category: str = "default"):
        """Track an object for memory leak detection."""
        with self._lock:
            if category not in self._tracked_objects:
                self._tracked_objects[category] = weakref.WeakSet()
                self._creation_counts[category] = 0
            
            self._tracked_objects[category].add(obj)
            self._creation_counts[category] += 1
    
    def get_object_counts(self) -> Dict[str, Dict[str, int]]:
        """Get counts of tracked objects."""
        with self._lock:
            return {
                category: {
                    "alive": len(weak_set),
                    "created": self._creation_counts.get(category, 0),
                    "garbage_collected": self._creation_counts.get(category, 0) - len(weak_set)
                }
                for category, weak_set in self._tracked_objects.items()
            }

# Global memory manager instance
memory_manager = MemoryManager()
resource_tracker = ResourceTracker()

# Decorator for automatic caching
def cached(cache_name: str = "default", ttl_seconds: float = 3600, max_size: int = 1000):
    """Decorator for automatic function result caching."""
    def decorator(func: Callable) -> Callable:
        cache = memory_manager.get_cache(f"func_{cache_name}_{func.__name__}", max_size, ttl_seconds)
        
        def wrapper(*args, **kwargs):
            # Create cache key from function name and arguments
            key = f"{func.__name__}:{hash((args, tuple(sorted(kwargs.items()))))}"
            
            # Try to get from cache
            result = cache.get(key)
            if result is not None:
                return result
            
            # Call function and cache result
            result = func(*args, **kwargs)
            cache.set(key, result)
            return result
        
        return wrapper
    return decorator

# Context manager for resource tracking
class tracked_resource:
    """Context manager for tracking resource lifecycle."""
    
    def __init__(self, resource_type: str):
        self.resource_type = resource_type
        self.resource = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.resource is not None:
            resource_tracker.track_object(self.resource, self.resource_type)