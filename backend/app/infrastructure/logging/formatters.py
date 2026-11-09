"""Implementações de formatter da infraestrutura de logging."""

from __future__ import annotations

import logging


class AppFormatter(logging.Formatter):
    """Formatter legível por humanos, para o console e para o arquivo."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        return (
            f"{timestamp} | {record.levelname} | {record.name} | {record.getMessage()}"
        )
