"""The configuration file, written the way someone writes it in Notepad.

RFC-034 sections 2.4, 5.3 and 5.5. Whoever edits `solidvision.env` is support
or the user: no test, no IDE. Each case below was read in the pilot as the
libraries behave *today*; these tests pin that behaviour, so an update of
`python-dotenv` or `pydantic-settings` that changes one of them breaks a test
instead of a user's configuration.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.infrastructure.config import settings as settings_module
from app.infrastructure.config.layout import Layout
from app.infrastructure.config.settings import (
    ConfigurationError,
    LoadedSettings,
    load_settings,
)

BACKEND = Path(__file__).resolve().parents[3]
PASSWORD = "p4ssw0rd-that-must-not-leak"

windows_paths = pytest.mark.skipif(
    os.name != "nt", reason="The paths written here are Windows paths"
)


def write(where: Layout, text: str, encoding: str = "utf-8") -> None:
    where.config_file.write_text(text, encoding=encoding)


def thumbnails(where: Layout, line: str) -> Path:
    write(where, line + "\n")
    return load_settings(where).settings.thumbnail_directory


# --------------------------------------------------------------------------
# Encoding (section 5.3): UTF-8, with or without a BOM
# --------------------------------------------------------------------------

TWO_LINES = "LOG_LEVEL=DEBUG\nBATCH_SIZE=4\n"


@pytest.mark.parametrize("mode", ["checkout", "installation"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_utf_8_is_read_with_or_without_a_bom(
    mode: str, encoding: str, request: pytest.FixtureRequest
) -> None:
    """The BOM was the bad case because it was silent: read as plain `utf-8`
    it became part of the first key, and `LOG_LEVEL` stayed `INFO`."""
    where: Layout = request.getfixturevalue(mode)
    write(where, TWO_LINES, encoding)

    loaded, report = load_settings(where)

    assert (loaded.log_level, loaded.batch_size) == ("DEBUG", 4)
    assert report.warnings == (), "the BOM must not turn the first key unknown"


@windows_paths
def test_a_path_outside_ascii_survives(installation: Layout) -> None:
    line = r"THUMBNAIL_DIRECTORY=C:\Users\João\Łódź 東京\thumbs"

    assert thumbnails(installation, line) == Path(r"C:\Users\João\Łódź 東京\thumbs")


@pytest.mark.parametrize("encoding", ["utf-16", "cp1252"])
def test_a_file_that_is_not_utf_8_refuses_to_load_naming_the_file(
    installation: Layout, encoding: str
) -> None:
    """Notepad's "Unicode" and "ANSI". The second only fails with a character
    outside ASCII, which a user's own name is enough to supply."""
    write(installation, "LOG_LEVEL=DEBUG\nTHUMBNAIL_DIRECTORY=C:\\João\n", encoding)

    with pytest.raises(ConfigurationError) as refusal:
        load_settings(installation)

    assert str(installation.config_file) in str(refusal.value)
    assert "UTF-8" in str(refusal.value)


# --------------------------------------------------------------------------
# Quotes and backslashes (section 2.4), as section 6.3 answers them
# --------------------------------------------------------------------------


@windows_paths
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (r"THUMBNAIL_DIRECTORY=D:\fotos\raw\thumbs", r"D:\fotos\raw\thumbs"),
        (r"THUMBNAIL_DIRECTORY='D:\fotos\raw\thumbs'", r"D:\fotos\raw\thumbs"),
        (r"THUMBNAIL_DIRECTORY=D:\Meus thumbs\novos", r"D:\Meus thumbs\novos"),
        (r"THUMBNAIL_DIRECTORY=D:\thumbs # o disco grande", r"D:\thumbs"),
        (r'THUMBNAIL_DIRECTORY="D:/fotos/raw/thumbs"', r"D:\fotos\raw\thumbs"),
    ],
)
def test_a_windows_path_is_read_as_written(
    installation: Layout, line: str, expected: str
) -> None:
    """Without quotes, or in single quotes. A space asks for no quotes, and a
    `#` after a space starts a comment."""
    assert thumbnails(installation, line) == Path(expected)


@windows_paths
@pytest.mark.parametrize(
    "line",
    [
        r'THUMBNAIL_DIRECTORY="D:\fotos\raw\thumbs"',
        r'THUMBNAIL_DIRECTORY="D:\Meus thumbs\novos"',
        r'LOG_DIRECTORY="D:\registros\novos"',
    ],
)
def test_double_quotes_around_a_windows_path_refuse_to_load(
    installation: Layout, line: str
) -> None:
    """The mistake becomes an error instead of a wrong folder: before, the
    first of these arrived as `D:`, a form feed, `otos`, a carriage return,
    `aw`, a tab and `humbs`."""
    write(installation, line + "\n")

    with pytest.raises(ConfigurationError, match="without quotes") as refusal:
        load_settings(installation)

    key = line.split("=")[0]
    assert f"{installation.config_file}: {key}: " in str(refusal.value)


@windows_paths
def test_a_percent_variable_refuses_to_load_with_the_form_that_works(
    installation: Layout,
) -> None:
    write(installation, "THUMBNAIL_DIRECTORY=%LOCALAPPDATA%\\thumbs\n")

    with pytest.raises(ConfigurationError) as refusal:
        load_settings(installation)

    assert "THUMBNAIL_DIRECTORY" in str(refusal.value)
    assert "${LOCALAPPDATA}" in str(refusal.value)


@windows_paths
def test_the_dollar_form_is_expanded_from_the_environment(
    installation: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one reading of the environment that stays, installed, besides
    `LOCALAPPDATA` itself: only when whoever wrote the file asked (5.2)."""
    monkeypatch.setenv("SV_TEST_DISK", r"E:\grande")

    found = thumbnails(installation, "THUMBNAIL_DIRECTORY=${SV_TEST_DISK}\\thumbs")

    assert found == Path(r"E:\grande\thumbs")


@windows_paths
@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("THUMBNAIL_DIRECTORY=D:thumbs", "names a drive and no root"),
        ("THUMBNAIL_DIRECTORY=\\thumbs", "names no drive"),
    ],
)
def test_the_two_halfway_forms_refuse_to_load(
    installation: Layout, line: str, problem: str
) -> None:
    write(installation, line + "\n")

    with pytest.raises(ConfigurationError, match=problem):
        load_settings(installation)


def test_a_relative_path_lands_beside_the_file(installation: Layout) -> None:
    found = thumbnails(installation, "THUMBNAIL_DIRECTORY=thumbs")

    assert found == installation.data_directory / "thumbs"


# --------------------------------------------------------------------------
# An invalid value refuses the start (section 5.5)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["checkout", "installation"])
def test_a_value_that_does_not_validate_names_the_file_and_the_key(
    mode: str, request: pytest.FixtureRequest
) -> None:
    where: Layout = request.getfixturevalue(mode)
    write(where, "BATCH_SIZE=abc\n")

    with pytest.raises(ConfigurationError) as refusal:
        load_settings(where)

    message = str(refusal.value)
    assert message.startswith(f"{where.config_file}: BATCH_SIZE: ")
    assert "\n" not in message, "one line"


def test_starting_with_the_default_in_its_place_is_not_what_happens(
    installation: Layout,
) -> None:
    """Whoever mistyped the path to another disk would get the thumbnails
    exactly where they said they did not want them."""
    write(installation, 'THUMBNAIL_DIRECTORY="E:\\thumbs"\n')

    with pytest.raises(ConfigurationError):
        load_settings(installation)


def test_two_bad_values_are_both_named_in_the_one_line(installation: Layout) -> None:
    write(installation, "BATCH_SIZE=abc\nDEBUG=perhaps\n")

    with pytest.raises(ConfigurationError) as refusal:
        load_settings(installation)

    message = str(refusal.value)
    assert "BATCH_SIZE" in message and "DEBUG" in message
    assert "\n" not in message


def test_in_development_a_bad_variable_is_said_to_be_a_variable(
    checkout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is not where to look when the value came from the shell."""
    write(checkout, "BATCH_SIZE=4\n")
    monkeypatch.setenv("BATCH_SIZE", "auto")

    with pytest.raises(ConfigurationError) as refusal:
        load_settings(checkout)

    assert str(refusal.value).startswith("environment variable: BATCH_SIZE: ")


def test_the_message_never_repeats_the_value(checkout: Layout) -> None:
    """It may be a password. Here it is one, in the wrong key."""
    write(checkout, f"DATABASE_PORT={PASSWORD}\n")

    with pytest.raises(ConfigurationError) as refusal:
        load_settings(checkout)

    assert "DATABASE_PORT" in str(refusal.value)
    assert PASSWORD not in str(refusal.value)


# --------------------------------------------------------------------------
# The process: one line on stderr, a non-zero exit, no traceback
# --------------------------------------------------------------------------


def test_the_singleton_turns_the_error_into_an_exit_with_its_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(where: Layout) -> LoadedSettings:
        raise ConfigurationError("solidvision.env: BATCH_SIZE: not a number")

    monkeypatch.setattr(settings_module, "load_settings", refuse)

    with pytest.raises(SystemExit) as stopped:
        settings_module._load_or_exit()

    assert stopped.value.code == (
        "SolidVision cannot start: solidvision.env: BATCH_SIZE: not a number"
    )


def test_a_process_with_an_invalid_configuration_dies_in_one_line() -> None:
    """A real process, in the one mode the checkout allows: development,
    where the environment is a source. Importing `settings` is what every
    program of the project does first."""
    completed = subprocess.run(
        [sys.executable, "-c", "import app.infrastructure.config.settings"],
        env={**os.environ, "PYTHONPATH": str(BACKEND), "BATCH_SIZE": "auto"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 1
    assert "Traceback" not in completed.stderr
    (line,) = completed.stderr.strip().splitlines()
    assert line.startswith("SolidVision cannot start: environment variable: BATCH_SIZE")
    assert completed.stdout == ""
