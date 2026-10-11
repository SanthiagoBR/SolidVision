"""Heads, facts and the bridge to `Settings` (RFC-033 sections 7.4 and 11).

No database here. The comparison of heads is a function of three sets, the
facts are checked against a row a double returns, and the environment
variables are checked against the `Settings` class itself -- the test that
closes, in code, the trap RFC-033a section 4.2 closed with a port check.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from app.infrastructure.config.settings import Settings
from app.infrastructure.database.embedded.database_setup import (
    ENVIRONMENT_VARIABLES,
    LIBPQ_OVERRIDES,
    ClusterConnection,
    HeadComparison,
    check_facts,
    code_revisions,
    compare_heads,
    verify_login,
)
from app.infrastructure.database.embedded.errors import (
    ClusterRefusedError,
    ServerNotReadyError,
    StartupFailedError,
)

HEAD = "6d77379a36a1"
PREVIOUS = "f4b9e2d7c615"
KNOWN = frozenset({HEAD, PREVIOUS, "999b801e80f4"})
PASSWORD = "p4ssw0rd-that-must-not-leak"


def heads(*revisions: str) -> frozenset[str]:
    return frozenset(revisions)


def test_equal_heads_need_nothing() -> None:
    assert compare_heads(heads(HEAD), heads(HEAD), KNOWN) is HeadComparison.CURRENT


def test_a_database_with_no_revision_is_behind() -> None:
    """A database `CREATE DATABASE` just made has no `alembic_version` at all."""
    assert compare_heads(heads(), heads(HEAD), KNOWN) is HeadComparison.BEHIND


def test_a_database_at_an_older_known_revision_is_behind() -> None:
    assert compare_heads(heads(PREVIOUS), heads(HEAD), KNOWN) is HeadComparison.BEHIND


def test_a_revision_the_code_does_not_know_is_not_behind() -> None:
    """Section 7.4: an older app opening a database a newer one migrated.

    The revision is not in the code's history, so "behind" would be a lie --
    and `upgrade head` from an unknown revision is an error at best.
    """
    newer = heads("0123456789ab")

    assert compare_heads(newer, heads(HEAD), KNOWN) is HeadComparison.UNKNOWN


def test_one_unknown_revision_among_known_ones_is_still_unknown() -> None:
    mixed = heads(PREVIOUS, "0123456789ab")

    assert compare_heads(mixed, heads(HEAD), KNOWN) is HeadComparison.UNKNOWN


def test_the_code_knows_its_own_head_and_its_history() -> None:
    """The project's real `ScriptDirectory`: one head, and it is in the history."""
    code_heads, known = code_revisions()

    assert len(code_heads) == 1
    assert code_heads <= known
    assert "999b801e80f4" in known, "the migration that creates the extension"


def test_every_environment_variable_is_a_settings_field() -> None:
    """Section 11. A misspelt name would not fail: `Settings` would fall back
    to the `.env`, the `.env` points at Docker, and the suite would pass
    against the wrong database printing the same `passed`."""
    for variable in ENVIRONMENT_VARIABLES:
        assert variable.lower() in Settings.model_fields, variable


def test_the_variables_carry_the_connection_into_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ClusterConnection(port=51336, password=PASSWORD)
    for name, value in connection.environment().items():
        monkeypatch.setenv(name, value)

    settings = Settings()

    assert settings.database_url == (
        f"postgresql+psycopg://solidvision:{PASSWORD}@127.0.0.1:51336/solidvision"
    )


def test_the_host_is_the_literal_not_a_name() -> None:
    """Section 4.2: the server listens on IPv4 only."""
    assert ClusterConnection(port=51336, password=PASSWORD).host == "127.0.0.1"


def test_the_password_is_in_no_repr() -> None:
    """Section 7.2: a log line or a traceback that prints the connection."""
    connection = ClusterConnection(port=51336, password=PASSWORD)

    assert PASSWORD not in repr(connection)
    assert PASSWORD not in str(connection)
    assert PASSWORD not in repr(connection.url())
    assert PASSWORD not in str(connection.url())


def test_the_adapters_own_connections_ignore_the_callers_pg_variables() -> None:
    """libpq falls back to `PGOPTIONS` and friends only for a parameter that
    was not given at all, so each is given, even when empty."""
    assert LIBPQ_OVERRIDES["options"] == ""
    assert LIBPQ_OVERRIDES["sslmode"] == "disable"
    assert LIBPQ_OVERRIDES["gssencmode"] == "disable"


def test_an_empty_override_is_still_given_to_libpq() -> None:
    """`options=''` must survive into the connection string: dropped, it
    would hand the parameter back to `PGOPTIONS`."""
    conninfo = ClusterConnection(port=51336, password=PASSWORD).conninfo("postgres")

    assert "options=''" in conninfo
    assert "sslmode=disable" in conninfo
    assert "gssencmode=disable" in conninfo
    assert "dbname=postgres" in conninfo
    assert "host=127.0.0.1" in conninfo and "port=51336" in conninfo


class OneRow:
    """Stands in for a connection whose only query is the facts query."""

    def __init__(self, *row: Any) -> None:
        self._row = row

    def execute(self, *_: Any) -> OneRow:
        return self

    def one(self) -> tuple[Any, ...]:
        return self._row


def facts(*row: Any) -> tuple[str, ...]:
    connection: Any = OneRow(*row)
    return check_facts(connection, major=16, pgvector="0.8.6")


def test_the_cluster_rfc_033a_measured_passes_without_a_warning() -> None:
    assert facts(16, "UTF8", "C", "C", "0.8.6") == ()


@pytest.mark.parametrize(
    ("row", "field"),
    [
        ((17, "UTF8", "C", "C", "0.8.6"), "major version"),
        ((16, "WIN1252", "C", "C", "0.8.6"), "encoding"),
        ((16, "UTF8", "Portuguese_Brazil.1252", "C", "0.8.6"), "datcollate"),
        ((16, "UTF8", "C", "en_US.utf8", "0.8.6"), "datctype"),
    ],
)
def test_a_divergence_names_its_field(row: tuple[Any, ...], field: str) -> None:
    with pytest.raises(ClusterRefusedError, match=field):
        facts(*row)


def test_an_older_vector_is_a_warning_not_a_refusal() -> None:
    """`ALTER EXTENSION vector UPDATE` belongs to upgrades (section 14)."""
    (warning,) = facts(16, "UTF8", "C", "C", "0.8.5")

    assert "0.8.5" in warning and "0.8.6" in warning


def test_versions_are_compared_as_numbers() -> None:
    """`0.8.10` is newer than `0.8.6`; as strings it would sort before it."""
    connection: Any = OneRow(16, "UTF8", "C", "C", "0.8.6")

    (warning,) = check_facts(connection, major=16, pgvector="0.8.10")

    assert "older" in warning


def test_a_newer_vector_than_the_runtimes_is_refused() -> None:
    with pytest.raises(ClusterRefusedError, match="newer"):
        facts(16, "UTF8", "C", "C", "0.8.7")


def test_no_vector_after_the_migrations_is_refused() -> None:
    with pytest.raises(ClusterRefusedError, match="vector"):
        facts(16, "UTF8", "C", "C", None)


def refuse_connection_with(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    def connect(*_: Any, **__: Any) -> None:
        raise psycopg.OperationalError(message)

    monkeypatch.setattr(psycopg, "connect", connect)


CONNECTION = ClusterConnection(port=51336, password=PASSWORD)
REFUSED = 'connection failed: connection to server at "127.0.0.1", port 51336 failed: '


@pytest.mark.parametrize(
    "said",
    [
        "FATAL:  the database system is starting up",
        "FATAL:  the database system is in recovery mode",
        "FATAL:  the database system is not yet accepting connections\n"
        "DETAIL:  Consistent recovery state has not been yet reached.",
    ],
)
def test_a_server_that_says_not_yet_is_told_apart_from_one_that_says_no(
    monkeypatch: pytest.MonkeyPatch, said: str
) -> None:
    """The texts are the ones a real server answered with while it
    reinitialized after a killed backend (RFC-033 section 18)."""
    refuse_connection_with(monkeypatch, REFUSED + said)

    with pytest.raises(ServerNotReadyError):
        verify_login(CONNECTION)


def test_a_rejected_password_is_a_refusal_and_never_a_not_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refuse_connection_with(
        monkeypatch,
        REFUSED + 'FATAL:  password authentication failed for user "solidvision"',
    )

    with pytest.raises(ClusterRefusedError, match="pair is lost"):
        verify_login(CONNECTION)


def test_a_server_that_does_not_answer_is_neither(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refuse_connection_with(monkeypatch, "connection timeout expired")

    with pytest.raises(StartupFailedError) as failure:
        verify_login(CONNECTION)

    assert not isinstance(failure.value, ServerNotReadyError)


def test_no_error_from_a_failed_login_carries_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refuse_connection_with(monkeypatch, "connection timeout expired")

    with pytest.raises(StartupFailedError) as failure:
        verify_login(CONNECTION)

    assert PASSWORD not in str(failure.value)
