"""
Graceful shutdown handler for proper cleanup of resources and connections.
Ensures data integrity and clean termination of all components.
"""

import signal
import asyncio
import time
import threading
from typing import List, Callable, Optional, Any
from datetime import datetime
import weakref

class ShutdownHandler:
    """Handles graceful shutdown of the application."""
    
    def __init__(self):
        self.shutdown_callbacks: List[Callable] = []
        self.async_shutdown_callbacks: List[Callable] = []
        self.is_shutting_down = False
        self.shutdown_timeout = 30.0  # 30 seconds timeout
        self._shutdown_event = threading.Event()
        self._cleanup_tasks: List[asyncio.Task] = []
        
        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        # Windows specific
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        signal_name = signal.Signals(signum).name
        print(f"\\n🛑 Received {signal_name} signal - initiating graceful shutdown...")
        
        if not self.is_shutting_down:
            self.is_shutting_down = True
            self._shutdown_event.set()
            
            # If we're in an asyncio context, schedule shutdown
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.shutdown_async())
            except RuntimeError:
                # Not in async context, run sync shutdown
                self.shutdown_sync()
    
    def register_shutdown_callback(self, callback: Callable):
        """Register a synchronous shutdown callback."""
        self.shutdown_callbacks.append(callback)
    
    def register_async_shutdown_callback(self, callback: Callable):
        """Register an asynchronous shutdown callback."""
        self.async_shutdown_callbacks.append(callback)
    
    def shutdown_sync(self):
        """Perform synchronous shutdown procedures."""
        print("🔄 Starting synchronous shutdown procedures...")
        
        start_time = time.time()
        
        for callback in self.shutdown_callbacks:
            try:
                print(f"🧹 Executing shutdown callback: {callback.__name__}")
                callback()
            except Exception as e:
                print(f"❌ Shutdown callback {callback.__name__} failed: {e}")
        
        elapsed = time.time() - start_time
        print(f"✅ Synchronous shutdown completed in {elapsed:.2f}s")
    
    async def shutdown_async(self):
        """Perform asynchronous shutdown procedures."""
        print("🔄 Starting asynchronous shutdown procedures...")
        
        start_time = time.time()
        
        # Execute async callbacks with timeout
        tasks = []
        for callback in self.async_shutdown_callbacks:
            try:
                print(f"🧹 Scheduling async shutdown callback: {callback.__name__}")
                task = asyncio.create_task(callback())
                tasks.append(task)
                self._cleanup_tasks.append(task)
            except Exception as e:
                print(f"❌ Failed to schedule shutdown callback {callback.__name__}: {e}")
        
        if tasks:
            try:
                # Wait for all tasks with timeout
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self.shutdown_timeout
                )
            except asyncio.TimeoutError:
                print(f"⚠️ Some shutdown tasks exceeded {self.shutdown_timeout}s timeout")
                # Cancel remaining tasks
                for task in tasks:
                    if not task.done():
                        task.cancel()
        
        elapsed = time.time() - start_time
        print(f"✅ Asynchronous shutdown completed in {elapsed:.2f}s")
    
    def wait_for_shutdown(self):
        """Block until shutdown is initiated."""
        self._shutdown_event.wait()
    
    async def wait_for_shutdown_async(self):
        """Async wait for shutdown."""
        while not self.is_shutting_down:
            await asyncio.sleep(0.1)

class ResourceManager:
    """Manages application resources and ensures proper cleanup."""
    
    def __init__(self):
        self.resources: List[weakref.ref] = []
        self.cleanup_functions: List[tuple] = []  # (resource_id, cleanup_func)
        self._lock = threading.Lock()
    
    def register_resource(self, resource: Any, cleanup_func: Optional[Callable] = None):
        """Register a resource for cleanup."""
        with self._lock:
            resource_ref = weakref.ref(resource)
            self.resources.append(resource_ref)
            
            if cleanup_func:
                resource_id = id(resource)
                self.cleanup_functions.append((resource_id, cleanup_func))
    
    def cleanup_all_resources(self):
        """Clean up all registered resources."""
        print("🧹 Cleaning up all registered resources...")
        
        with self._lock:
            # Clean up resources with custom cleanup functions
            for resource_id, cleanup_func in self.cleanup_functions:
                try:
                    cleanup_func()
                    print(f"✅ Cleaned up resource {resource_id}")
                except Exception as e:
                    print(f"❌ Failed to cleanup resource {resource_id}: {e}")
            
            # Clear resource tracking
            self.resources.clear()
            self.cleanup_functions.clear()
        
        print("✅ Resource cleanup completed")
    
    def get_resource_count(self) -> int:
        """Get count of active resources."""
        with self._lock:
            # Filter out garbage collected resources
            active_resources = [ref for ref in self.resources if ref() is not None]
            self.resources = active_resources
            return len(active_resources)

class DataSaver:
    """Saves critical data during shutdown."""
    
    def __init__(self):
        self.data_to_save: List[tuple] = []  # (filename, data, format)
        self._lock = threading.Lock()
    
    def register_data(self, filename: str, data_getter: Callable, data_format: str = 'json'):
        """Register data to be saved during shutdown."""
        with self._lock:
            self.data_to_save.append((filename, data_getter, data_format))
    
    def save_all_data(self):
        """Save all registered data."""
        print("💾 Saving critical data...")
        
        with self._lock:
            for filename, data_getter, data_format in self.data_to_save:
                try:
                    data = data_getter()
                    
                    if data_format == 'json':
                        import json
                        with open(filename, 'w', encoding='utf-8') as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                    elif data_format == 'text':
                        with open(filename, 'w', encoding='utf-8') as f:
                            f.write(str(data))
                    else:
                        print(f"⚠️ Unknown data format: {data_format}")
                        continue
                    
                    print(f"✅ Saved data to {filename}")
                    
                except Exception as e:
                    print(f"❌ Failed to save data to {filename}: {e}")
        
        print("✅ Data saving completed")

class HealthChecker:
    """Monitor application health during shutdown."""
    
    def __init__(self):
        self.health_checks: List[tuple] = []  # (name, check_func)
        self.is_healthy = True
    
    def register_health_check(self, name: str, check_func: Callable[[], bool]):
        """Register a health check function."""
        self.health_checks.append((name, check_func))
    
    def run_health_checks(self) -> bool:
        """Run all health checks and return overall health status."""
        print("🔍 Running pre-shutdown health checks...")
        
        overall_healthy = True
        
        for name, check_func in self.health_checks:
            try:
                is_healthy = check_func()
                status = "✅ HEALTHY" if is_healthy else "❌ UNHEALTHY"
                print(f"  {name}: {status}")
                
                if not is_healthy:
                    overall_healthy = False
                    
            except Exception as e:
                print(f"  {name}: ❌ CHECK FAILED - {e}")
                overall_healthy = False
        
        self.is_healthy = overall_healthy
        
        if overall_healthy:
            print("✅ All health checks passed")
        else:
            print("⚠️ Some health checks failed - proceeding with caution")
        
        return overall_healthy

# Global instances
shutdown_handler = ShutdownHandler()
resource_manager = ResourceManager()
data_saver = DataSaver()
health_checker = HealthChecker()

# Utility functions for easy integration
def register_shutdown_callback(callback: Callable):
    """Register a shutdown callback (synchronous)."""
    shutdown_handler.register_shutdown_callback(callback)

def register_async_shutdown_callback(callback: Callable):
    """Register an async shutdown callback."""
    shutdown_handler.register_async_shutdown_callback(callback)

def register_resource_cleanup(resource: Any, cleanup_func: Optional[Callable] = None):
    """Register a resource for cleanup."""
    resource_manager.register_resource(resource, cleanup_func)

def register_critical_data(filename: str, data_getter: Callable, data_format: str = 'json'):
    """Register critical data to be saved during shutdown."""
    data_saver.register_data(filename, data_getter, data_format)

def register_health_check(name: str, check_func: Callable[[], bool]):
    """Register a health check."""
    health_checker.register_health_check(name, check_func)

# Main shutdown orchestrator
async def graceful_shutdown():
    """Orchestrate complete graceful shutdown."""
    print("\\n🛑 Starting graceful shutdown sequence...")
    start_time = time.time()
    
    try:
        # 1. Run health checks
        health_checker.run_health_checks()
        
        # 2. Execute shutdown callbacks
        await shutdown_handler.shutdown_async()
        
        # 3. Save critical data
        data_saver.save_all_data()
        
        # 4. Clean up resources
        resource_manager.cleanup_all_resources()
        
        # 5. Final cleanup tasks
        print("🧹 Performing final cleanup...")
        
        # Cancel any remaining tasks
        current_task = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task != current_task]
        
        if tasks:
            print(f"🔄 Cancelling {len(tasks)} remaining tasks...")
            for task in tasks:
                task.cancel()
            
            # Wait for tasks to finish cancellation
            await asyncio.gather(*tasks, return_exceptions=True)
        
        elapsed = time.time() - start_time
        print(f"\\n✅ Graceful shutdown completed successfully in {elapsed:.2f}s")
        
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"\\n❌ Shutdown completed with errors in {elapsed:.2f}s: {e}")
        
    finally:
        print("👋 Application terminated")

# Context manager for resource tracking
class managed_resource:
    """Context manager for automatic resource registration and cleanup."""
    
    def __init__(self, resource: Any, cleanup_func: Optional[Callable] = None):
        self.resource = resource
        self.cleanup_func = cleanup_func
    
    def __enter__(self):
        register_resource_cleanup(self.resource, self.cleanup_func)
        return self.resource
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Resource will be cleaned up automatically during shutdown
        pass