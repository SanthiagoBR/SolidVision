"""Central logging factory for the application."""

from __future__ import annotations

import logging
from typing import Any

from app.infrastructure.config.settings import configuration_report, settings

from .handlers import build_console_handler, build_rotating_file_handler
from .program import program_name

_APP_LOGGER_NAME = "app"


def _log_filename() -> str:
    """`LOG_FILENAME` when set; otherwise one file per program (RFC-034 8.2)."""
    return settings.log_filename or f"{program_name()}.log"


def _configure_root_logger() -> logging.Logger:
    """Configure the root application logger once."""
    root_logger = logging.getLogger(_APP_LOGGER_NAME)
    if root_logger.handlers:
        return root_logger

    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root_logger.propagate = False

    root_logger.addHandler(build_console_handler())
    # Absolute already: `Settings` anchors every path (RFC-034 section 6.3).
    root_logger.addHandler(
        build_rotating_file_handler(settings.log_directory, _log_filename())
    )
    _write_configuration_report(root_logger)
    return root_logger


def _write_configuration_report(root_logger: logging.Logger) -> None:
    """Say where this process's configuration came from (RFC-034 section 5.6).

    Written here because `settings` cannot log: this module imports it. The
    warnings name every key of the file that did not do what it seems to.
    """
    for line in configuration_report.lines():
        root_logger.info(line)
    for warning in configuration_report.warnings:
        root_logger.warning(warning)


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for the calling module."""
    _configure_root_logger()
    return logging.getLogger(f"{_APP_LOGGER_NAME}.{name}")


def log_exception(logger: logging.Logger, message: str, exc_info: Any = True) -> None:
    """Log an exception with a traceback."""
    logger.exception(message, exc_info=exc_info)
