"""Central logging factory for the application."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.infrastructure.config.settings import settings

from .handlers import build_console_handler, build_rotating_file_handler

_APP_LOGGER_NAME = "app"


def _resolve_log_directory() -> Path:
    """Resolve the log directory to an absolute path using the project root."""
    configured_directory = Path(settings.log_directory)
    if configured_directory.is_absolute():
        return configured_directory

    project_root = Path(__file__).resolve().parents[4]
    return project_root / configured_directory


def _configure_root_logger() -> logging.Logger:
    """Configure the root application logger once."""
    root_logger = logging.getLogger(_APP_LOGGER_NAME)
    if root_logger.handlers:
        return root_logger

    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root_logger.propagate = False

    root_logger.addHandler(build_console_handler())
    root_logger.addHandler(
        build_rotating_file_handler(
            _resolve_log_directory(),
            settings.log_filename,
        )
    )
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for the calling module."""
    _configure_root_logger()
    return logging.getLogger(f"{_APP_LOGGER_NAME}.{name}")


def log_exception(logger: logging.Logger, message: str, exc_info: Any = True) -> None:
    """Log an exception with a traceback."""
    logger.exception(message, exc_info=exc_info)
