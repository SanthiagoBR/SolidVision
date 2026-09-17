"""The Windows revealer, with its process launcher replaced (RFC-030 section 5.1).

**Nothing here starts `explorer.exe`.** Each test hands the adapter a
recorder in place of `subprocess.Popen` and asserts on exactly what would
have been launched: the argument list, and the absence of a shell.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.file_revealer import WindowsFileRevealer


class RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


def reveal(path: Path) -> tuple[list[str], dict[str, Any]]:
    launcher = RecordingLauncher()
    WindowsFileRevealer(launch=launcher).reveal(ImagePath(path))
    ((args, kwargs),) = launcher.calls
    (command,) = args
    return command, kwargs


def test_select_and_the_path_are_two_separate_arguments(tmp_path: Path) -> None:
    photo = tmp_path / "DJI_0042.JPG"
    photo.write_bytes(b"x")

    command, _ = reveal(photo)

    assert command[1:] == ["/select,", os.fspath(photo)]


def test_a_path_with_spaces_reaches_explorer_in_the_form_that_selects_it(
    tmp_path: Path,
) -> None:
    """The correction to RFC-030 section 5.1, pinned at the command line.

    As one list item, `/select,<path>` is quoted whole when the path has a
    space, and Explorer then opens Documents with nothing selected -- seen
    on Windows 10 (`experiments/rfc-030-file-access/
    verify_explorer_select.log`). As two items, only the path is quoted.
    This asserts the exact string `CreateProcess` would receive, because
    that string is what Explorer parses.
    """
    photo = tmp_path / "com espaço" / "fazenda São João.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"x")

    command, _ = reveal(photo)

    command_line = subprocess.list2cmdline(command)
    assert f'/select, "{os.fspath(photo)}"' in command_line
    assert '"/select,' not in command_line


def test_no_shell_is_ever_involved(tmp_path: Path) -> None:
    """RFC-030 section 5.1: a list, and `shell=False` said out loud."""
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")

    command, kwargs = reveal(photo)

    assert isinstance(command, list)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_the_system_explorer_is_named_by_absolute_path(tmp_path: Path) -> None:
    """A bare `explorer` would be searched for in the venv and the CWD first."""
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")

    command, _ = reveal(photo)

    executable = Path(command[0])
    assert executable.is_absolute()
    assert executable.name.lower() == "explorer.exe"
    assert executable.parent == Path(os.environ["SystemRoot"])


def test_the_path_uses_native_separators(tmp_path: Path) -> None:
    """`ImagePath` stores forward slashes; Explorer's `/select,` wants backslashes."""
    photo = tmp_path / "nested" / "a.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"x")

    command, _ = reveal(Path(str(photo).replace("\\", "/")))

    assert "/" not in command[2]


@pytest.mark.parametrize(
    "name", ["fazenda São João.jpg", "foto & cia; (1).jpg", "%PATH% ^x.jpg"]
)
def test_metacharacters_and_non_ascii_names_arrive_unchanged(
    tmp_path: Path, name: str
) -> None:
    """Characters a shell would interpret mean nothing without a shell."""
    photo = tmp_path / name
    photo.write_bytes(b"x")

    command, kwargs = reveal(photo)

    assert command[2] == os.fspath(photo)
    assert kwargs["shell"] is False


def test_a_missing_file_raises_and_launches_nothing(tmp_path: Path) -> None:
    """Explorer would open a default folder instead of failing."""
    launcher = RecordingLauncher()

    with pytest.raises(FileNotFoundError):
        WindowsFileRevealer(launch=launcher).reveal(ImagePath(tmp_path / "gone.jpg"))

    assert launcher.calls == []


def test_a_directory_is_not_a_file_to_reveal(tmp_path: Path) -> None:
    launcher = RecordingLauncher()

    with pytest.raises(FileNotFoundError):
        WindowsFileRevealer(launch=launcher).reveal(ImagePath(tmp_path))

    assert launcher.calls == []
