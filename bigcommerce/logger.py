"""Reusable application logger with console + rotating file output."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class AppLogger:
    """Wraps `logging.Logger` with sane defaults (console + rotating file, one-time setup per name)."""

    _configured_names = set()

    def __init__(
        self,
        name: str,
        log_dir: str = "logs",
        log_file: str = "bigcommerce.log",
        level: str = None,
        max_bytes: int = 5_000_000,
        backup_count: int = 5,
    ):
        self.logger = logging.getLogger(name)
        level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
        self.logger.setLevel(getattr(logging, level, logging.INFO))

        # Guard against duplicate handlers if AppLogger(name) is instantiated more than once.
        if name not in AppLogger._configured_names:
            self.logger.propagate = False
            formatter = logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

            Path(log_dir).mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                Path(log_dir) / log_file, maxBytes=max_bytes, backupCount=backup_count
            )
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

            AppLogger._configured_names.add(name)

    def debug(self, msg, *args, **kwargs):
        self.logger.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self.logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self.logger.error(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self.logger.critical(msg, *args, **kwargs)
