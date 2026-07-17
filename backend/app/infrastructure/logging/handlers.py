"""Handler implementations for the logging infrastructure."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .formatters import AppFormatter


def build_console_handler() -> logging.Handler:
    """Create a console handler."""
    handler = logging.StreamHandler()
    handler.setFormatter(AppFormatter())
    return handler


def build_rotating_file_handler(
    log_directory: Path, log_filename: str
) -> logging.Handler:
    """Create a rotating file handler."""
    log_directory.mkdir(parents=True, exist_ok=True)
    file_path = log_directory / log_filename
    handler = RotatingFileHandler(
        filename=file_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(AppFormatter())
    return handler
