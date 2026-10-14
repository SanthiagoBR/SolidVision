"""What the logger takes from the settings (RFC-034 sections 5.6 and 8)."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from app.infrastructure.config.layout import Environment
from app.infrastructure.config.settings import (
    ConfigurationReport,
    DatabaseSource,
    settings,
)
from app.infrastructure.logging import logger as logger_module
from app.infrastructure.logging.handlers import LosslessRotatingFileHandler


class Collecting(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_without_log_filename_the_file_is_named_after_the_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "log_filename", None)
    monkeypatch.setattr(logger_module, "program_name", lambda: "job_runner")

    assert logger_module._log_filename() == "job_runner.log"


def test_log_filename_still_gives_every_process_the_one_fixed_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "log_filename", "application.log")
    monkeypatch.setattr(logger_module, "program_name", lambda: "job_runner")

    assert logger_module._log_filename() == "application.log"


def test_the_log_directory_the_logger_uses_is_the_settings_and_absolute() -> None:
    """The logger no longer anchors anything: `Settings` did (section 6.3)."""
    logger_module.get_logger(__name__)
    (file_handler,) = [
        handler
        for handler in logging.getLogger("app").handlers
        if isinstance(handler, LosslessRotatingFileHandler)
    ]

    assert settings.log_directory.is_absolute()
    assert Path(file_handler.baseFilename).parent == settings.log_directory


def test_the_logger_has_no_anchor_of_its_own() -> None:
    source = Path(logger_module.__file__).read_text(encoding="utf-8")
    names = {
        node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)
    }

    assert "__file__" not in names
    assert "_resolve_log_directory" not in source


def test_the_report_and_then_its_warnings_are_written_to_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = ConfigurationReport(
        environment=Environment.INSTALLED,
        config_file=tmp_path / "solidvision.env",
        config_file_exists=True,
        data_directory=tmp_path,
        directories=(("log_directory", tmp_path / "logs"),),
        database_source=DatabaseSource.EMBEDDED,
        warnings=("solidvision.env: THUMBNAIL_SIZE is not a setting",),
    )
    monkeypatch.setattr(logger_module, "configuration_report", report)
    collecting = Collecting()
    target = logging.Logger("report-test")
    target.addHandler(collecting)

    logger_module._write_configuration_report(target)

    written = [(record.levelname, record.getMessage()) for record in collecting.records]
    assert written == [
        *(("INFO", line) for line in report.lines()),
        ("WARNING", "solidvision.env: THUMBNAIL_SIZE is not a setting"),
    ]
    assert any("mode installed" in message for _, message in written)
    assert any(str(report.config_file) in message for _, message in written)
