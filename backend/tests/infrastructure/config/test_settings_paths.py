"""No path relative to the working directory is left in `Settings`.

The roadmap's second criterion for RFC-034, and section 6.3: every path
setting is a `Path` and leaves `Settings` absolute -- the defaults, what a
file says, and what the environment says.
"""

from __future__ import annotations

import os
import typing
from pathlib import Path

import pytest

from app.infrastructure.config.layout import Layout
from app.infrastructure.config.settings import (
    PATH_DEFAULTS,
    ConfigurationError,
    Settings,
    absolute_path,
    load_settings,
)

PATH_FIELDS = sorted(
    name for name, field in Settings.model_fields.items() if field.annotation is Path
)

BASE = Path(r"C:\Users\Ana\AppData\Local\SolidVision")

windows_paths = pytest.mark.skipif(
    os.name != "nt", reason="The paths written here are Windows paths"
)


def write(where: Layout, text: str) -> None:
    where.config_file.write_text(text, encoding="utf-8")


def relative_to_every_path_field() -> str:
    return "".join(f"{name.upper()}=some/{name}\n" for name in PATH_FIELDS)


# --------------------------------------------------------------------------
# The criterion, and its guards
# --------------------------------------------------------------------------


def test_the_path_settings_are_the_five_the_rfcs_name() -> None:
    """RFC-034's four, and the model runtime of RFC-035 section 4.5."""
    assert PATH_FIELDS == [
        "embedded_postgres_directory",
        "embedded_postgres_runtime_directory",
        "log_directory",
        "model_runtime_directory",
        "thumbnail_directory",
    ]
    assert sorted(PATH_DEFAULTS) == PATH_FIELDS


def test_every_field_named_like_a_path_is_a_path() -> None:
    """The guard against the next `log_directory: str`: a field that names a
    place and is typed as text escapes every check that looks for `Path`."""
    hints = typing.get_type_hints(Settings)
    named_like_a_path = [
        name
        for name in Settings.model_fields
        if name.endswith(("_directory", "_path", "_file"))
    ]

    assert sorted(named_like_a_path) == PATH_FIELDS
    for name in named_like_a_path:
        assert hints[name] is Path, name


@pytest.mark.parametrize("mode", ["checkout", "installation"])
def test_without_a_file_every_path_is_absolute(
    mode: str, request: pytest.FixtureRequest
) -> None:
    loaded = load_settings(request.getfixturevalue(mode)).settings

    for name in PATH_FIELDS:
        assert getattr(loaded, name).is_absolute(), name


@pytest.mark.parametrize("mode", ["checkout", "installation"])
def test_a_file_that_gives_each_a_relative_value_still_yields_absolute_paths(
    mode: str, request: pytest.FixtureRequest
) -> None:
    where: Layout = request.getfixturevalue(mode)
    write(where, relative_to_every_path_field())

    loaded = load_settings(where).settings

    for name in PATH_FIELDS:
        assert getattr(loaded, name) == where.config_base / "some" / name, name


def test_in_development_the_environment_giving_relative_values_does_too(
    checkout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in PATH_FIELDS:
        monkeypatch.setenv(name.upper(), f"some/{name}")

    loaded = load_settings(checkout).settings

    for name in PATH_FIELDS:
        assert getattr(loaded, name) == checkout.config_base / "some" / name, name


@pytest.mark.parametrize("mode", ["checkout", "installation"])
def test_a_relative_constructor_argument_is_anchored_like_any_other(
    mode: str, request: pytest.FixtureRequest
) -> None:
    where: Layout = request.getfixturevalue(mode)

    loaded = load_settings(where, thumbnail_directory=Path("thumbs")).settings

    assert loaded.thumbnail_directory == where.config_base / "thumbs"


def test_the_working_directory_decides_nothing(
    checkout: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(checkout, "THUMBNAIL_DIRECTORY=thumbs\n")
    here = load_settings(checkout).settings.model_dump()
    elsewhere = tmp_path / "somewhere else"
    elsewhere.mkdir()

    monkeypatch.chdir(elsewhere)

    assert load_settings(checkout).settings.model_dump() == here


# --------------------------------------------------------------------------
# The defaults come from the layout (sections 6.1 and 6.3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["checkout", "installation"])
def test_the_defaults_are_the_layouts(
    mode: str, request: pytest.FixtureRequest
) -> None:
    where: Layout = request.getfixturevalue(mode)

    loaded = load_settings(where).settings

    assert loaded.thumbnail_directory == where.data_directory / "thumbnails"
    assert loaded.log_directory == where.data_directory / "logs"
    assert loaded.embedded_postgres_directory == where.data_directory / "postgres"
    assert loaded.embedded_postgres_runtime_directory == (
        where.root / "runtime" / "postgres"
    )
    assert loaded.model_runtime_directory == where.root / "runtime" / "models"


def test_a_checkout_and_an_installation_share_no_user_directory(
    checkout: Layout, installation: Layout
) -> None:
    """Section 2.5: on the pilot's machine a checkout had already created the
    cluster where an installed app would look for its own."""
    developer = load_settings(checkout).settings
    user = load_settings(installation).settings

    for name in ("thumbnail_directory", "log_directory", "embedded_postgres_directory"):
        assert getattr(developer, name) != getattr(user, name), name
        assert installation.data_directory in getattr(user, name).parents
        assert installation.data_directory not in getattr(developer, name).parents


def test_log_directory_logs_still_means_the_repositorys_logs(checkout: Layout) -> None:
    """What `.env.example` shipped, and what RFC-005 section 3.5 defined. The
    sum is now made once, in `Settings`, and not in the consumer."""
    write(checkout, "LOG_DIRECTORY=logs\n")

    assert load_settings(checkout).settings.log_directory == checkout.root / "logs"


# --------------------------------------------------------------------------
# The five rules, one at a time (section 6.3)
# --------------------------------------------------------------------------


@windows_paths
def test_an_absolute_path_is_left_as_it_is() -> None:
    assert absolute_path(r"D:\fotos\thumbs", BASE) == Path(r"D:\fotos\thumbs")


@windows_paths
def test_a_relative_path_is_anchored_on_the_folder_of_the_file() -> None:
    assert absolute_path("thumbs", BASE) == BASE / "thumbs"
    assert absolute_path(r"cache\thumbs", BASE) == BASE / "cache" / "thumbs"


@windows_paths
def test_the_result_is_normalized() -> None:
    assert absolute_path(r"..\thumbs", BASE) == BASE.parent / "thumbs"
    assert absolute_path(r"D:\fotos\.\raw\..\thumbs", BASE) == Path(r"D:\fotos\thumbs")
    assert absolute_path("D:/fotos/thumbs", BASE) == Path(r"D:\fotos\thumbs")


@windows_paths
def test_the_disk_is_never_consulted() -> None:
    """A path on a drive this machine does not have is still just a path."""
    assert absolute_path(r"Q:\no\such\place", BASE) == Path(r"Q:\no\such\place")
    assert absolute_path("thumbs", Path(r"Q:\nowhere")) == Path(r"Q:\nowhere\thumbs")


@windows_paths
def test_a_path_with_spaces_needs_no_quotes() -> None:
    assert absolute_path(r"D:\Meus thumbs\novos", BASE) == Path(r"D:\Meus thumbs\novos")


@pytest.mark.parametrize(
    "mangled",
    ["D:\fotos\raw\thumbs", "D:\\Meus thumbs\novos", "D:\\thumbs\x00", "D:\\a\x7fb"],
)
def test_rule_1_a_control_character_is_refused(mangled: str) -> None:
    """The trace of double quotes: `\\f`, `\\r`, `\\t` and `\\n` are escapes
    inside them, and `fotos`, `raw`, `thumbs` and `novos` all start with one."""
    with pytest.raises(ValueError, match="control character") as refusal:
        absolute_path(mangled, BASE)

    assert "without quotes" in str(refusal.value)
    assert str(refusal.value).isprintable(), "the message must not carry it"


@pytest.mark.parametrize(
    ("written", "name"),
    [
        (r"%LOCALAPPDATA%\thumbs", "LOCALAPPDATA"),
        (r"D:\cache\%USERNAME%\thumbs", "USERNAME"),
        (r"%ProgramFiles(x86)%\thumbs", "ProgramFiles(x86)"),
    ],
)
def test_rule_2_a_percent_variable_is_refused_with_the_form_that_works(
    written: str, name: str
) -> None:
    """Not expanded by the format: it would be a folder called `%LOCALAPPDATA%`."""
    with pytest.raises(ValueError) as refusal:
        absolute_path(written, BASE)

    assert f"%{name}%" in str(refusal.value)
    assert f"${{{name}}}" in str(refusal.value)


@windows_paths
def test_a_percent_sign_alone_is_just_a_character() -> None:
    assert absolute_path(r"D:\100% fotos", BASE) == Path(r"D:\100% fotos")


@windows_paths
def test_rule_3_a_drive_without_a_root_is_refused() -> None:
    """`D:thumbs` is relative to drive D's current directory, and stays
    relative after it is anchored."""
    with pytest.raises(ValueError, match="names a drive and no root"):
        absolute_path("D:thumbs", BASE)


@windows_paths
@pytest.mark.parametrize("written", [r"\thumbs", "/thumbs"])
def test_rule_3_a_root_without_a_drive_is_refused(written: str) -> None:
    """`\\thumbs` inherits the drive of wherever it is anchored."""
    with pytest.raises(ValueError, match="names no drive"):
        absolute_path(written, BASE)


@windows_paths
def test_a_network_share_is_absolute() -> None:
    share = r"\\server\share\thumbs"

    assert absolute_path(share, BASE) == Path(share)


def test_rule_1_comes_before_rule_4() -> None:
    """A mangled relative path is refused, not anchored into a strange folder."""
    with pytest.raises(ValueError, match="control character"):
        absolute_path("meus\thumbs", BASE)


@pytest.mark.parametrize("written", ["", " ", "\t"])
def test_an_empty_value_is_refused_before_any_rule(written: str) -> None:
    """`THUMBNAIL_DIRECTORY=` is the path `.`: anchored, it would be the
    folder of the configuration file itself, with the thumbnails loose in it."""
    with pytest.raises(ValueError, match="is empty") as refusal:
        absolute_path(written, BASE)

    assert "Delete the line" in str(refusal.value)


@pytest.mark.parametrize("mode", ["checkout", "installation"])
@pytest.mark.parametrize("name", PATH_FIELDS)
def test_an_empty_path_in_the_file_refuses_to_load_naming_the_key(
    mode: str, name: str, request: pytest.FixtureRequest
) -> None:
    where: Layout = request.getfixturevalue(mode)
    write(where, f"{name.upper()}=\n")

    with pytest.raises(ConfigurationError) as refusal:
        load_settings(where)

    assert str(refusal.value).startswith(f"{where.config_file}: {name.upper()}: ")
    assert "is empty" in str(refusal.value)
