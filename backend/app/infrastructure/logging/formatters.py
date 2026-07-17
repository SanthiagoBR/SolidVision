"""Formatter implementations for the logging infrastructure."""

from __future__ import annotations

import logging


class AppFormatter(logging.Formatter):
    """Human-readable formatter for console and file output."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        return (
            f"{timestamp} | {record.levelname} | {record.name} | {record.getMessage()}"
        )
