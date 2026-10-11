"""Reading `postmaster.pid` (RFC-033 sections 8.2 and 9).

The file below is a real one, written by PostgreSQL 16.15 on Windows and
copied here with only the path shortened: forward slashes in line 2, an
empty line 5 where a Unix socket directory would be, an empty line 7 where
other platforms write a shared memory key, and a state padded to eight
characters in line 8.
"""

from __future__ import annotations

from pathlib import Path

from app.infrastructure.database.embedded.postmaster_pid import (
    STATUS_READY,
    STATUS_STARTING,
    STATUS_STOPPING,
    read_postmaster_pid,
)

READY_FILE = (
    "14072\n"
    "C:/Users/someone/AppData/Local/SolidVision/postgres/16/data\n"
    "1791732090\n"
    "59582\n"
    "\n"
    "127.0.0.1\n"
    "\n"
    "ready   \n"
)


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "postmaster.pid"
    path.write_bytes(content.encode("ascii"))
    return path


def test_a_ready_server_gives_pid_directory_port_and_state(tmp_path: Path) -> None:
    found = read_postmaster_pid(write(tmp_path, READY_FILE))

    assert found is not None
    assert found.pid == 14072
    assert found.port == 59582
    assert found.status == STATUS_READY
    assert found.data_directory.endswith("/postgres/16/data")


def test_the_states_the_server_writes_are_read_without_their_padding(
    tmp_path: Path,
) -> None:
    for written, expected in (
        ("starting", STATUS_STARTING),
        ("stopping", STATUS_STOPPING),
        ("ready   ", STATUS_READY),
    ):
        content = READY_FILE.replace("ready   ", written)

        found = read_postmaster_pid(write(tmp_path, content))

        assert found is not None and found.status == expected


def test_windows_line_endings_are_not_part_of_any_field(tmp_path: Path) -> None:
    found = read_postmaster_pid(write(tmp_path, READY_FILE.replace("\n", "\r\n")))

    assert found is not None
    assert found.port == 59582
    assert found.status == STATUS_READY


def test_a_file_still_being_written_has_a_pid_and_no_state(tmp_path: Path) -> None:
    """The server writes the file in stages; the first read can come early."""
    first_lines = "".join(READY_FILE.splitlines(keepends=True)[:3])

    found = read_postmaster_pid(write(tmp_path, first_lines))

    assert found is not None
    assert found.pid == 14072
    assert found.port is None
    assert found.status is None


def test_no_file_is_no_server(tmp_path: Path) -> None:
    assert read_postmaster_pid(tmp_path / "postmaster.pid") is None


def test_an_empty_or_garbled_file_has_no_pid_to_act_on(tmp_path: Path) -> None:
    assert read_postmaster_pid(write(tmp_path, "")) is None
    assert read_postmaster_pid(write(tmp_path, "not a pid\n")) is None


def test_line_two_is_compared_as_a_path_not_as_a_string(tmp_path: Path) -> None:
    """Section 8.4. The server writes forward slashes and its own case."""
    data = tmp_path / "Postgres" / "16" / "data"
    data.mkdir(parents=True)
    recorded = str(data).replace("\\", "/").lower()
    found = read_postmaster_pid(
        write(tmp_path, READY_FILE.replace(READY_FILE.splitlines()[1], recorded))
    )

    assert found is not None
    assert found.points_to(data)
    assert not found.points_to(tmp_path / "Postgres" / "17" / "data")


def test_a_file_with_no_second_line_points_nowhere(tmp_path: Path) -> None:
    found = read_postmaster_pid(write(tmp_path, "28612\n"))

    assert found is not None
    assert not found.points_to(tmp_path)
