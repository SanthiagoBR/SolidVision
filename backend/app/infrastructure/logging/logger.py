"""Fábrica central de loggers da aplicação."""

from __future__ import annotations

import logging
from typing import Any

from app.infrastructure.config.settings import configuration_report, settings

from .handlers import build_console_handler, build_rotating_file_handler
from .program import program_name

_APP_LOGGER_NAME = "app"


def _log_filename() -> str:
    """`LOG_FILENAME` quando definido; senão, um arquivo por programa (RFC-034 §8.2)."""
    return settings.log_filename or f"{program_name()}.log"


def _configure_root_logger() -> logging.Logger:
    """Configura o logger raiz da aplicação, uma vez só."""
    root_logger = logging.getLogger(_APP_LOGGER_NAME)
    if root_logger.handlers:
        return root_logger

    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root_logger.propagate = False

    root_logger.addHandler(build_console_handler())
    # Já absoluto: `Settings` ancora todo caminho (RFC-034 §6.3).
    root_logger.addHandler(
        build_rotating_file_handler(settings.log_directory, _log_filename())
    )
    _write_configuration_report(root_logger)
    return root_logger


def _write_configuration_report(root_logger: logging.Logger) -> None:
    """Diz de onde veio a configuração deste processo (RFC-034 §5.6).

    Escrito aqui porque `settings` não pode fazer log: este módulo o importa. Os
    avisos nomeiam cada chave do arquivo que não fez o que parece fazer.
    """
    for line in configuration_report.lines():
        root_logger.info(line)
    for warning in configuration_report.warnings:
        root_logger.warning(warning)


def get_logger(name: str) -> logging.Logger:
    """Devolve um logger configurado para o módulo que chama."""
    _configure_root_logger()
    return logging.getLogger(f"{_APP_LOGGER_NAME}.{name}")


def log_exception(logger: logging.Logger, message: str, exc_info: Any = True) -> None:
    """Registra uma exceção no log, com traceback."""
    logger.exception(message, exc_info=exc_info)
