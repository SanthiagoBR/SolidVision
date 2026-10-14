"""A rotation that drops neither the record nor a backup (RFC-034 section 8.3).

The file is real and so is the second process: what is being checked is what
Windows does to a rename while someone else holds the file, which a double
could only repeat back. With the standard handler this arrangement kept 219
records of 600 and one backup of five (measured, section 2.6).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.infrastructure.logging import handlers
from app.infrastructure.logging.handlers import (
    LosslessRotatingFileHandler,
    build_rotating_file_handler,
)

LIMIT = 2000
BACKUPS = 5

windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Only Windows refuses to rename a file another process holds open",
)


class Recording(LosslessRotatingFileHandler):
    """The handler, noting every record `logging` would have dropped."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, maxBytes=LIMIT, backupCount=BACKUPS, encoding="utf-8")
        self.setFormatter(logging.Formatter("%(message)s"))
        self.dropped: list[str] = []

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        self.dropped.append(record.getMessage())


@pytest.fixture()
def log(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "job_runner.log"


@pytest.fixture()
def handler(log: Path) -> Iterator[Recording]:
    log.parent.mkdir()
    recording = Recording(log)
    try:
        yield recording
    finally:
        recording.close()


def write(handler: Recording, first: int, stop: int) -> list[str]:
    """Records of about a hundred bytes, numbered from `first` up to `stop`."""
    messages = [f"record {number:04d} " + "x" * 80 for number in range(first, stop)]
    for message in messages:
        handler.handle(logging.LogRecord("t", logging.INFO, "", 0, message, None, None))
    return messages


def old_backups(log: Path) -> dict[str, str]:
    """Five backups from before the test, each recognizable by its content."""
    backups = {f"{log.name}.{number}": f"old {number}\n" for number in range(1, 6)}
    for name, content in backups.items():
        (log.parent / name).write_text(content, encoding="utf-8")
    return backups


def on_disk(log: Path) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(log.parent.iterdir())
    }


def records_in(*texts: str) -> list[str]:
    return [line for text in texts for line in text.splitlines() if "record" in line]


HOLD = textwrap.dedent("""
    import sys, time
    handle = open(sys.argv[1], "a", encoding="utf-8")
    print("holding", flush=True)
    time.sleep(120)
    """)


@pytest.fixture()
def held_by_another_process(log: Path, handler: Recording) -> Iterator[None]:
    """A second process with the log open, the way Python's `open()` opens it:
    without asking Windows to let anyone rename the file meanwhile."""
    child = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(log)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "holding"
        yield
    finally:
        child.kill()
        child.wait(timeout=10)


# --------------------------------------------------------------------------
# One process: the rotation of RFC-005, unchanged in what it leaves
# --------------------------------------------------------------------------


def test_a_rotation_shifts_the_backups_and_keeps_every_record(
    log: Path, handler: Recording
) -> None:
    old = old_backups(log)

    written = write(handler, 0, 25)

    files = on_disk(log)
    assert handler.dropped == []
    assert sorted(files) == sorted([log.name, *old])
    assert records_in(files[f"{log.name}.1"], files[log.name]) == written
    assert files[f"{log.name}.2"] == old[f"{log.name}.1"]
    assert files[f"{log.name}.5"] == old[f"{log.name}.4"]
    assert old[f"{log.name}.5"] not in files.values(), "the sixth is what goes"


def test_the_limit_still_holds_for_a_file_nobody_else_has(
    log: Path, handler: Recording
) -> None:
    write(handler, 0, 300)

    assert all(len(text.encode()) <= LIMIT for text in on_disk(log).values())
    assert handler.dropped == []


# --------------------------------------------------------------------------
# Two processes on one file
# --------------------------------------------------------------------------


@windows_only
def test_a_rename_that_fails_loses_no_record_and_touches_no_backup(
    log: Path, handler: Recording, held_by_another_process: None
) -> None:
    old = old_backups(log)

    written = write(handler, 0, 600)

    files = on_disk(log)
    assert handler.dropped == []
    assert records_in(files[log.name]) == written, "600 of 600, in order"
    assert {name: files[name] for name in old} == old, "5 of 5, untouched"
    assert sorted(files) == sorted([log.name, *old]), "and nothing left half way"


@windows_only
def test_the_declared_cost_is_a_file_past_its_limit(
    log: Path, handler: Recording, held_by_another_process: None
) -> None:
    """It grows instead of losing lines."""
    write(handler, 0, 600)

    assert log.stat().st_size > LIMIT * 20


@windows_only
def test_the_first_rotation_after_the_other_process_exits_cuts_the_file(
    log: Path, handler: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "ROLLOVER_RETRY_SECONDS", 0.0)
    old = old_backups(log)
    child = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(log)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "holding"
        while_shared = write(handler, 0, 100)
    finally:
        child.kill()
        child.wait(timeout=10)

    # Windows lets go of a dead process's files a moment after the process
    # is gone, and how long a moment is not this test's to assume. Until it
    # does, the rename keeps failing and the records keep being written.
    after: list[str] = []
    deadline = time.monotonic() + 30
    while not records_in(on_disk(log)[f"{log.name}.1"]):
        assert time.monotonic() < deadline, "the file was never rotated"
        after += write(handler, 100 + len(after), 101 + len(after))
        time.sleep(0.05)

    files = on_disk(log)
    rotated = records_in(files[f"{log.name}.1"])
    assert handler.dropped == []
    assert rotated[:100] == while_shared, "past its limit, and whole"
    assert rotated + records_in(files[log.name]) == while_shared + after
    assert records_in(files[log.name]) == after[-1:], "the record that cut it"
    assert files[f"{log.name}.2"] == old[f"{log.name}.1"]


def test_a_rotation_that_cannot_happen_is_not_retried_on_every_record(
    log: Path, handler: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Put off by `ROLLOVER_RETRY_SECONDS`: one failed rename, not hundreds."""
    attempts: list[str] = []

    def refuse(source: str, target: str) -> None:
        attempts.append(source)
        raise PermissionError(32, "The file is in use by another process", source)

    monkeypatch.setattr(os, "rename", refuse)

    written = write(handler, 0, 300)

    assert len(attempts) == 1
    assert handler.dropped == []
    assert records_in(log.read_text(encoding="utf-8")) == written


def test_the_rotation_is_tried_again_once_the_wait_is_over(
    log: Path, handler: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusing = True
    rename = os.rename

    def sometimes(source: str, target: str) -> None:
        if refusing:
            raise PermissionError(32, "The file is in use by another process", source)
        rename(source, target)

    monkeypatch.setattr(os, "rename", sometimes)
    write(handler, 0, 100)
    assert not (log.parent / f"{log.name}.1").exists()

    refusing = False
    handler._next_attempt = time.monotonic()
    write(handler, 100, 101)

    assert (log.parent / f"{log.name}.1").exists()
    assert handler.dropped == []


# --------------------------------------------------------------------------
# A rotation interrupted between its two steps
# --------------------------------------------------------------------------


def test_a_backup_that_cannot_move_does_not_lose_the_record_either(
    log: Path, handler: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The current file left, and then a backup refused to shift: the record
    is still written, and the next rotation finishes what this one began."""
    old_backups(log)
    replace = os.replace
    refusing = True

    def sometimes(source: str, target: str) -> None:
        if refusing:
            raise PermissionError(32, "The file is in use by another process", source)
        replace(source, target)

    monkeypatch.setattr(os, "replace", sometimes)
    monkeypatch.setattr(handlers, "ROLLOVER_RETRY_SECONDS", 0.0)

    written = write(handler, 0, 22)

    assert handler.dropped == []
    assert (log.parent / f"{log.name}.rotating").exists()

    refusing = False
    written += write(handler, 22, 60)

    files = on_disk(log)
    assert handler.dropped == []
    assert f"{log.name}.rotating" not in files
    assert sorted(records_in(*files.values())) == sorted(written)
    assert all(len(text.encode()) <= LIMIT for text in files.values())


def test_what_a_dead_process_left_half_rotated_becomes_a_backup(
    log: Path, handler: Recording
) -> None:
    (log.parent / f"{log.name}.rotating").write_text("left behind\n", "utf-8")

    write(handler, 0, 25)

    files = on_disk(log)
    assert f"{log.name}.rotating" not in files
    assert files[f"{log.name}.2"] == "left behind\n"
    assert handler.dropped == []


# --------------------------------------------------------------------------
# What the project builds
# --------------------------------------------------------------------------


def test_the_projects_file_handler_is_this_one_with_rfc_005s_numbers(
    tmp_path: Path,
) -> None:
    """The format, the level, the 10 MB and the five backups do not change."""
    directory = tmp_path / "SolidVision" / "logs"

    built = build_rotating_file_handler(directory, "uvicorn.log")
    try:
        assert isinstance(built, LosslessRotatingFileHandler)
        assert built.maxBytes == 10 * 1024 * 1024
        assert built.backupCount == BACKUPS
        assert Path(built.baseFilename) == directory / "uvicorn.log"
        assert directory.is_dir(), "created on the first use, not before"
    finally:
        built.close()
