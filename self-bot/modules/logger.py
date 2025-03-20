import logging
import os
from datetime import datetime

class Logger:
    def __init__(self, debug_enabled=False, source=None):
        self.debug_enabled = debug_enabled
        self.source = source
        self.logger = logging.getLogger("Logger")
        self.logger.setLevel(logging.DEBUG if debug_enabled else logging.INFO)

        # Create logs directory if not exists
        if not os.path.exists("logs"):
            os.makedirs("logs")

        # Configure logging format
        log_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_formatter)
        self.logger.addHandler(console_handler)

        # File handler
        log_filename = datetime.now().strftime("logs/%Y-%m-%d.log")
        file_handler = logging.FileHandler(log_filename)
        file_handler.setFormatter(log_formatter)
        self.logger.addHandler(file_handler)

    def log(self, level, message, *args):
        msg = f"[{self.source}] {message}" if self.source else message
        self.logger.log(level, msg, *args)

    def debug(self, message, *args):
        if self.debug_enabled:
            self.log(logging.DEBUG, message, *args)

    def info(self, message, *args):
        self.log(logging.INFO, message, *args)

    def warning(self, message, *args):
        self.log(logging.WARNING, message, *args)

    def error(self, message, *args):
        self.log(logging.ERROR, message, *args)

    def critical(self, message, *args):
        self.log(logging.CRITICAL, message, *args)

    def success(self, message, *args):
        self.log(logging.INFO, f"SUCCESS: {message}", *args)

    def reset(self):
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
