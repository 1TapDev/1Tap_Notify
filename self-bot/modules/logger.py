import logging
import os
import json
import uuid
import traceback
import time
from datetime import datetime
from typing import Optional, Dict, Any
from contextlib import contextmanager
from threading import local
from functools import wraps

class Logger:
    """Enhanced structured logger with support for contextual data."""
    
    def __init__(self, debug_enabled=False, source=None):
        self.debug_enabled = debug_enabled
        self.source = source
        self.logger = logging.getLogger("StructuredLogger")
        self.logger.setLevel(logging.DEBUG if debug_enabled else logging.INFO)

        # Create logs directory if not exists
        if not os.path.exists("logs"):
            os.makedirs("logs")

        # Configure structured logging format
        log_formatter = StructuredFormatter()

        # Console handler (only errors to console) with Unicode support
        import sys
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.ERROR)
        console_handler.setFormatter(log_formatter)
        
        # Set UTF-8 encoding for console if possible
        try:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8')
        except:
            pass  # Fallback for older Python versions
            
        self.logger.addHandler(console_handler)

        # File handler (all levels to file) with UTF-8 encoding
        log_filename = datetime.now().strftime("logs/structured_%Y-%m-%d.log")
        file_handler = logging.FileHandler(log_filename, encoding='utf-8')
        file_handler.setFormatter(log_formatter)
        self.logger.addHandler(file_handler)

    def _log_structured(self, level, message, **kwargs):
        """Log with structured data support."""
        try:
            # Add correlation context if available
            correlation_id = getattr(_local, 'correlation_id', None)
            if correlation_id:
                kwargs['correlation_id'] = correlation_id
                
            # Add source if specified
            if self.source:
                kwargs['source'] = self.source
                
            # Create structured log entry
            extra = {'structured_data': kwargs}
            self.logger.log(level, message, extra=extra)
            
        except (UnicodeEncodeError, UnicodeDecodeError) as e:
            # Fallback: log without problematic characters
            safe_message = repr(message)  # This will escape Unicode properly
            extra = {'structured_data': {**kwargs, 'encoding_error': str(e)}}
            self.logger.log(level, f"[ENCODING ERROR] {safe_message}", extra=extra)

    def debug(self, message, **kwargs):
        if self.debug_enabled:
            self._log_structured(logging.DEBUG, message, **kwargs)

    def info(self, message, **kwargs):
        self._log_structured(logging.INFO, message, **kwargs)

    def warning(self, message, **kwargs):
        self._log_structured(logging.WARNING, message, **kwargs)

    def error(self, message, **kwargs):
        self._log_structured(logging.ERROR, message, **kwargs)

    def critical(self, message, **kwargs):
        self._log_structured(logging.CRITICAL, message, **kwargs)

    def success(self, message, **kwargs):
        self._log_structured(logging.INFO, f"SUCCESS: {message}", **kwargs)

    def reset(self):
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            
    def correlation_context(self):
        """Context manager for correlation IDs."""
        return CorrelationContext()

# Enhanced logging components
_local = local()

class CorrelationContext:
    """Context manager for correlation tracking."""
    
    def __init__(self):
        import uuid
        self.correlation_id = str(uuid.uuid4())[:8]
        
    def __enter__(self):
        _local.correlation_id = self.correlation_id
        return self.correlation_id
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        _local.correlation_id = None

class StructuredFormatter(logging.Formatter):
    """Custom formatter for structured JSON logs."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }
        
        # Add correlation ID if available
        correlation_id = getattr(_local, 'correlation_id', None)
        if correlation_id:
            log_entry["correlation_id"] = correlation_id
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info)
            }
        
        # Add custom fields from extra
        if hasattr(record, 'extra_fields'):
            log_entry.update(record.extra_fields)
        elif hasattr(record, 'structured_data'):
            log_entry.update(record.structured_data)
        
        return json.dumps(log_entry, ensure_ascii=False)

class PerformanceLogger:
    """Logger for performance metrics and monitoring."""
    
    def __init__(self):
        self.logger = logging.getLogger("performance")
        self._setup_performance_logger()
        
    def _setup_performance_logger(self):
        """Setup dedicated performance logger."""
        if not self.logger.handlers:
            perf_handler = logging.FileHandler(
                f'logs/performance_{datetime.now().strftime("%Y-%m-%d")}.log'
            )
            perf_handler.setFormatter(StructuredFormatter())
            self.logger.addHandler(perf_handler)
            self.logger.setLevel(logging.INFO)
    
    def log_function_performance(self, func_name: str, duration: float, **kwargs):
        """Log function performance metrics."""
        extra = {
            'extra_fields': {
                'function': func_name,
                'duration_ms': round(duration * 1000, 2),
                'metric_type': 'performance',
                **kwargs
            }
        }
        self.logger.info(f"Performance: {func_name} took {duration:.3f}s", extra=extra)
    
    def log_message_processing(self, server_id: str, message_count: int, 
                              processing_time: float):
        """Log message processing performance."""
        extra = {
            'extra_fields': {
                'server_id': server_id,
                'message_count': message_count,
                'processing_time_ms': round(processing_time * 1000, 2),
                'messages_per_second': round(message_count / processing_time, 2),
                'metric_type': 'message_processing'
            }
        }
        self.logger.info(
            f"Processed {message_count} messages in {processing_time:.3f}s", 
            extra=extra
        )

class ErrorAggregator:
    """Aggregate and track errors for monitoring."""
    
    def __init__(self, max_errors: int = 1000):
        self.errors: Dict[str, Dict[str, Any]] = {}
        self.max_errors = max_errors
        self.logger = logging.getLogger("error_aggregator")
    
    def record_error(self, error_type: str, error_message: str, 
                    context: Optional[Dict[str, Any]] = None):
        """Record an error for aggregation."""
        error_key = f"{error_type}:{error_message[:100]}"  # Limit key length
        
        if error_key in self.errors:
            self.errors[error_key]["count"] += 1
            self.errors[error_key]["last_seen"] = datetime.utcnow().isoformat()
        else:
            self.errors[error_key] = {
                "type": error_type,
                "message": error_message,
                "count": 1,
                "first_seen": datetime.utcnow().isoformat(),
                "last_seen": datetime.utcnow().isoformat(),
                "context": context or {}
            }
        
        # Prevent memory bloat
        if len(self.errors) > self.max_errors:
            oldest_key = min(self.errors.keys(), 
                           key=lambda k: self.errors[k]["first_seen"])
            del self.errors[oldest_key]
    
    def get_error_summary(self) -> Dict[str, Any]:
        """Get summary of recorded errors."""
        return {
            "total_unique_errors": len(self.errors),
            "total_error_count": sum(e["count"] for e in self.errors.values()),
            "most_frequent": sorted(
                [(k, v) for k, v in self.errors.items()], 
                key=lambda x: x[1]["count"], 
                reverse=True
            )[:10]
        }

def performance_monitor(func):
    """Decorator to monitor function performance."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            duration = time.time() - start_time
            perf_logger.log_function_performance(
                func.__name__, 
                duration,
                success=True
            )
            return result
        except Exception as e:
            duration = time.time() - start_time
            error_aggregator.record_error(
                type(e).__name__,
                str(e),
                {"function": func.__name__, "duration": duration}
            )
            perf_logger.log_function_performance(
                func.__name__, 
                duration,
                success=False,
                error_type=type(e).__name__
            )
            raise
    return wrapper

def async_performance_monitor(func):
    """Decorator to monitor async function performance."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = await func(*args, **kwargs)
            duration = time.time() - start_time
            perf_logger.log_function_performance(
                func.__name__, 
                duration,
                success=True
            )
            return result
        except Exception as e:
            duration = time.time() - start_time
            error_aggregator.record_error(
                type(e).__name__,
                str(e),
                {"function": func.__name__, "duration": duration}
            )
            perf_logger.log_function_performance(
                func.__name__, 
                duration,
                success=False,
                error_type=type(e).__name__
            )
            raise
    return wrapper

# Global instances
structured_logger = Logger()
perf_logger = PerformanceLogger()
error_aggregator = ErrorAggregator()
performance_monitor = perf_logger  # Alias for compatibility

# Export commonly used functions
__all__ = [
    'structured_logger', 'perf_logger', 'error_aggregator', 'performance_monitor',
    'async_performance_monitor', 'Logger', 'PerformanceLogger', 'ErrorAggregator'
]
