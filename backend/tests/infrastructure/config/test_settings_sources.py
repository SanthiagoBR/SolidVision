"""Where each mode reads its configuration from (RFC-034 section 5).

Every `Settings` here is loaded for a layout in a temporary directory, so the
checkout's own `.env` and the developer's environment decide nothing.
"""

from __future__ import annotations

import re

import pytest

from app.infrastructure.config.layout import Environment, Layout, layout
from app.infrastructure.config.settings import (
    DATABASE_KEYS,
    RETIRED_KEYS,
    DatabaseSource,
    Settings,
    load_settings,
    settings,
)

PASSWORD = "p4ssw0rd-that-must-not-leak"


def write(where: Layout, text: str) -> None:
    where.config_file.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Installed: the process environment is not a source (section 5.2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("DEBUG", "*"),
        ("DEBUG", "release"),
        ("DEBUG", "1"),
        ("BATCH_SIZE", "auto"),
        ("LOG_LEVEL", "ERROR"),
        ("DATABASE_HOST", "somebody-elses-postgres"),
        ("THUMBNAIL_DIRECTORY", "thumbs"),
    ],
)
def test_installed_a_variable_with_a_settings_name_changes_nothing(
    installation: Layout, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """`DEBUG=*` is a convention of a popular Node package. Before RFC-034 it
    stopped the settings from importing at all (measured, section 2.3)."""
    untouched = load_settings(installation).settings
    monkeypatch.setenv(variable, value)

    loaded = load_settings(installation).settings

    assert loaded.model_dump() == untouched.model_dump()
    assert loaded.debug is False


def test_installed_the_users_file_is_read_and_the_environment_is_not(
    installation: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The combination the pilot ran against a subclass, here against the
    real class."""
    monkeypatch.setenv("DEBUG", "*")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    write(installation, "LOG_LEVEL=WARNING\n")

    loaded = load_settings(installation).settings

    assert loaded.debug is False
    assert loaded.log_level == "WARNING"


def test_installed_an_env_file_left_in_the_installation_is_not_read(
    installation: Layout,
) -> None:
    """The installation directory is read-only and per machine, not per user."""
    (installation.root / ".env").write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

    assert load_settings(installation).settings.log_level == "INFO"


def test_installed_without_a_file_the_defaults_are_used(installation: Layout) -> None:
    """The file is optional, and the app never creates it."""
    loaded, report = load_settings(installation)

    assert loaded.batch_size == 8
    assert report.config_file_exists is False
    assert not installation.config_file.exists()


# --------------------------------------------------------------------------
# Development: as before -- the environment above the `.env` (RFC-004)
# --------------------------------------------------------------------------


def test_development_reads_the_env_file_of_the_repository(checkout: Layout) -> None:
    write(checkout, "LOG_LEVEL=WARNING\nBATCH_SIZE=4\n")

    loaded = load_settings(checkout).settings

    assert (loaded.log_level, loaded.batch_size) == ("WARNING", 4)


def test_development_the_environment_is_above_the_env_file(
    checkout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(checkout, "LOG_LEVEL=WARNING\nBATCH_SIZE=4\n")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")

    loaded = load_settings(checkout).settings

    assert (loaded.log_level, loaded.batch_size) == ("ERROR", 4)


def test_development_the_users_file_is_not_read(checkout: Layout) -> None:
    """Each mode reads one file, and never the other's (section 5.1)."""
    checkout.data_directory.mkdir()
    (checkout.data_directory / "solidvision.env").write_text(
        "LOG_LEVEL=DEBUG\n", encoding="utf-8"
    )

    assert load_settings(checkout).settings.log_level == "INFO"


@pytest.mark.parametrize("mode", ["checkout", "installation"])
def test_constructor_arguments_are_the_strongest_source(
    mode: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    where: Layout = request.getfixturevalue(mode)
    write(where, "BATCH_SIZE=4\n")
    monkeypatch.setenv("BATCH_SIZE", "16")

    assert load_settings(where, batch_size=32).settings.batch_size == 32


def test_settings_without_arguments_is_the_running_codes_layout() -> None:
    """`Settings()` keeps meaning "freshly loaded", for the tests that compare
    the singleton against it."""
    assert Settings().environment is layout.environment
    assert Settings().thumbnail_directory == settings.thumbnail_directory


# --------------------------------------------------------------------------
# The mode, as `Settings` reports it (section 4.2)
# --------------------------------------------------------------------------


def test_the_environment_is_the_layouts_and_not_a_field(
    checkout: Layout, installation: Layout
) -> None:
    assert load_settings(checkout).settings.environment is Environment.DEVELOPMENT
    assert load_settings(installation).settings.environment is Environment.INSTALLED
    assert "environment" not in Settings.model_fields


def test_health_keeps_answering_a_plain_string(installation: Layout) -> None:
    """`/health` puts `settings.environment` in its JSON as it is."""
    assert load_settings(installation).settings.environment == "installed"


@pytest.mark.parametrize("mode", ["checkout", "installation"])
def test_writing_environment_in_a_file_changes_nothing_and_is_named(
    mode: str, request: pytest.FixtureRequest
) -> None:
    where: Layout = request.getfixturevalue(mode)
    write(where, "ENVIRONMENT=production\n")

    loaded, report = load_settings(where)

    assert loaded.environment is where.environment
    (warning,) = report.warnings
    assert "ENVIRONMENT is retired" in warning and str(where.config_file) in warning


def test_no_variable_declares_the_mode_either(
    installation: Layout, checkout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", " Production ")

    assert load_settings(installation).settings.environment == "installed"
    assert load_settings(checkout).settings.environment == "development"


# --------------------------------------------------------------------------
# The database source (section 7.1)
# --------------------------------------------------------------------------


def test_development_defaults_to_the_external_database(checkout: Layout) -> None:
    """The `.env` and Docker, as before: a plain `pytest` keeps going there."""
    loaded = load_settings(checkout).settings

    assert loaded.database_source is DatabaseSource.EXTERNAL
    assert (loaded.database_host, loaded.database_port) == ("localhost", 5432)


@pytest.mark.parametrize("written", ["embedded", "EMBEDDED", " Embedded "])
def test_one_line_in_the_env_file_is_development_without_docker(
    checkout: Layout, written: str
) -> None:
    write(checkout, f"DATABASE_SOURCE='{written}'\n")

    assert load_settings(checkout).settings.database_source is DatabaseSource.EMBEDDED


def test_warm_up_is_on_installed_and_off_in_a_checkout(
    checkout: Layout, installation: Layout
) -> None:
    """RFC-035 section 8. Installed nobody writes the user's file, and the
    environment is not read: a `False` there would leave the API cold for
    ever. In a checkout, `TestClient(app)` must not load 924 MB."""
    assert load_settings(installation).settings.warm_up_models is True
    assert load_settings(checkout).settings.warm_up_models is False


@pytest.mark.parametrize("mode", ["checkout", "installation"])
@pytest.mark.parametrize("written", ["true", "false"])
def test_the_file_still_decides_the_warm_up_in_both_modes(
    mode: str, written: str, request: pytest.FixtureRequest
) -> None:
    where: Layout = request.getfixturevalue(mode)
    write(where, f"WARM_UP_MODELS={written}\n")

    assert load_settings(where).settings.warm_up_models is (written == "true")


def test_installed_the_source_is_always_embedded(installation: Layout) -> None:
    write(installation, "DATABASE_SOURCE=external\n")

    loaded = load_settings(installation, database_source="external").settings

    assert loaded.database_source is DatabaseSource.EMBEDDED


def test_installed_the_database_keys_of_the_file_have_no_effect_and_are_named(
    installation: Layout,
) -> None:
    """Not even an invalid one stops the app: it is not read (section 5.4)."""
    write(
        installation,
        "DATABASE_SOURCE=external\nDATABASE_HOST=10.0.0.9\nDATABASE_PORT=abc\n"
        f"DATABASE_NAME=other\nDATABASE_USER=other\nDATABASE_PASSWORD={PASSWORD}\n",
    )

    loaded, report = load_settings(installation)

    assert loaded.database_host == "localhost"
    assert loaded.database_port == 5432
    assert loaded.database_password != PASSWORD
    assert len(report.warnings) == len(DATABASE_KEYS) == 6
    for key, warning in zip(DATABASE_KEYS, report.warnings, strict=True):
        assert f"{key.upper()} has no effect" in warning
    assert PASSWORD not in "".join(report.warnings)


def test_in_development_the_database_keys_do_what_they_say(checkout: Layout) -> None:
    write(checkout, "DATABASE_HOST=10.0.0.9\nDATABASE_PORT=6543\n")

    loaded, report = load_settings(checkout)

    assert (loaded.database_host, loaded.database_port) == ("10.0.0.9", 6543)
    assert report.warnings == ()


def test_the_connection_url_is_no_longer_a_setting() -> None:
    """One place builds a URL: the engine (section 7.6)."""
    assert not hasattr(Settings, "database_url")


# --------------------------------------------------------------------------
# Keys that do not do what they seem to (section 5.4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["checkout", "installation"])
def test_an_almost_right_key_is_ignored_and_named(
    mode: str, request: pytest.FixtureRequest
) -> None:
    """`EMBEDDING_BATCH_SIZE` for `BATCH_SIZE`: read without an error, and the
    value stayed 8 (measured, section 2.4). Five of the eleven keys of a real
    `.env` were like this."""
    where: Layout = request.getfixturevalue(mode)
    write(where, "EMBEDDING_BATCH_SIZE=64\nTHUMBNAIL_SIZE=256\nBATCH_SIZE=4\n")

    loaded, report = load_settings(where)

    assert (loaded.batch_size, loaded.thumbnail_max_edge) == (4, 512)
    assert len(report.warnings) == 2
    assert "EMBEDDING_BATCH_SIZE is not a setting" in report.warnings[0]
    assert "THUMBNAIL_SIZE is not a setting" in report.warnings[1]
    assert all(str(where.config_file) in warning for warning in report.warnings)


def test_a_retired_key_says_what_replaced_it(checkout: Layout) -> None:
    write(checkout, "INDEXING_ROOT_PATH=data/images\n")

    (warning,) = load_settings(checkout).report.warnings

    assert "INDEXING_ROOT_PATH is retired" in warning
    assert "RFC-029" in warning
    assert "indexing_root_path" not in Settings.model_fields
    assert set(RETIRED_KEYS) == {"ENVIRONMENT", "INDEXING_ROOT_PATH", "EMBEDDING_MODEL"}


def test_embedding_model_is_retired_and_says_where_the_model_is_now(
    installation: Layout,
) -> None:
    """RFC-035 section 4.4: the key that used to swap the embedding space.

    Written in the user's file it would have made the next process with a
    network download another 512-dimensional model, and mixed two spaces in
    one index without an error. It is now a warning and nothing else.
    """
    write(installation, "EMBEDDING_MODEL=openai/clip-vit-base-patch32\n")

    loaded, report = load_settings(installation)

    (warning,) = report.warnings
    assert "EMBEDDING_MODEL is retired and was ignored" in warning
    assert "lockfile" in warning and "runtime\\models" in warning
    assert "embedding_model" not in Settings.model_fields
    assert not hasattr(loaded, "embedding_model")


def test_a_clean_file_warns_about_nothing(installation: Layout) -> None:
    write(installation, "LOG_LEVEL=DEBUG\nBATCH_SIZE=4\n# a comment\n\n")

    assert load_settings(installation).report.warnings == ()


def test_a_file_notepad_named_with_txt_is_pointed_out(installation: Layout) -> None:
    """The hypothesis of section 5.3: Notepad saves `solidvision.env` as
    `solidvision.env.txt` and hides the extension."""
    mistaken = installation.data_directory / "solidvision.env.txt"
    mistaken.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

    loaded, report = load_settings(installation)

    assert loaded.log_level == "INFO", "it is not read"
    (warning,) = report.warnings
    assert "solidvision.env.txt" in warning and "Rename" in warning


def test_the_txt_file_is_not_mentioned_once_the_right_one_exists(
    installation: Layout,
) -> None:
    (installation.data_directory / "solidvision.env.txt").write_text(
        "LOG_LEVEL=DEBUG\n", encoding="utf-8"
    )
    write(installation, "LOG_LEVEL=WARNING\n")

    assert load_settings(installation).report.warnings == ()


# --------------------------------------------------------------------------
# The report (section 5.6)
# --------------------------------------------------------------------------


def test_the_report_says_where_the_configuration_came_from(
    installation: Layout,
) -> None:
    write(installation, "LOG_LEVEL=DEBUG\n")

    loaded, report = load_settings(installation)
    text = "\n".join(report.lines())

    assert report.environment is Environment.INSTALLED
    assert report.config_file == installation.config_file
    assert report.config_file_exists is True
    assert report.data_directory == installation.data_directory
    assert report.database_source is DatabaseSource.EMBEDDED
    assert dict(report.directories) == {
        "thumbnail_directory": loaded.thumbnail_directory,
        "log_directory": loaded.log_directory,
        "embedded_postgres_directory": loaded.embedded_postgres_directory,
        "embedded_postgres_runtime_directory": (
            loaded.embedded_postgres_runtime_directory
        ),
        "model_runtime_directory": loaded.model_runtime_directory,
    }
    assert "mode installed" in text
    assert f"file {installation.config_file} (read)" in text
    assert f"data directory {installation.data_directory}" in text
    assert f"thumbnail_directory {loaded.thumbnail_directory}" in text
    assert "database source embedded" in text


def test_the_report_says_when_there_is_no_file(checkout: Layout) -> None:
    text = "\n".join(load_settings(checkout).report.lines())

    assert "mode development" in text
    assert f"file {checkout.config_file} (absent, defaults in use)" in text
    assert "database source external" in text


def test_the_password_is_in_neither_the_repr_nor_the_report(checkout: Layout) -> None:
    """Section 7.7. The pilot found it in the `repr` of the settings."""
    write(checkout, f"DATABASE_PASSWORD={PASSWORD}\n")

    loaded, report = load_settings(checkout)

    assert loaded.database_password == PASSWORD
    assert PASSWORD not in repr(loaded)
    assert PASSWORD not in str(loaded)
    assert PASSWORD not in repr(report)
    assert PASSWORD not in "\n".join(report.lines())


# --------------------------------------------------------------------------
# The vocabulary (section 2.4)
# --------------------------------------------------------------------------


def test_every_key_of_env_example_is_a_setting() -> None:
    """Commented or not. The guard against the example drifting from the
    class again, which is how a real `.env` came to have five dead keys."""
    example = (layout.root / ".env.example").read_text(encoding="utf-8")
    keys = re.findall(r"^#? ?([A-Z][A-Z0-9_]*)=", example, flags=re.MULTILINE)

    assert len(keys) > 20, "the pattern must be finding the keys"
    assert "DATABASE_SOURCE" in keys and "THUMBNAIL_DIRECTORY" in keys
    for key in keys:
        assert key.lower() in Settings.model_fields, key


def test_env_example_names_no_retired_key() -> None:
    example = (layout.root / ".env.example").read_text(encoding="utf-8")

    for key in RETIRED_KEYS:
        assert not re.search(rf"^#? ?{key}=", example, flags=re.MULTILINE), key


def test_the_example_would_load_without_a_warning(checkout: Layout) -> None:
    """Copying `.env.example` to `.env` is how a checkout starts."""
    example = (layout.root / ".env.example").read_text(encoding="utf-8")
    write(checkout, example)

    loaded, report = load_settings(checkout)

    assert report.warnings == ()
    assert loaded.database_source is DatabaseSource.EXTERNAL
    assert loaded.log_directory == checkout.log_directory
