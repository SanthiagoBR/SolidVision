"""`python -m app.infrastructure.database.embedded` (RFC-033 section 11).

The cluster is a double; the command `run` launches is a real child process,
because what is being checked is what a child process actually receives: the
source and the two directories (RFC-034 section 7.6), and an exit code that
comes back unchanged.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from app.infrastructure.database.embedded import __main__ as cli
from app.infrastructure.database.embedded.cluster import (
    ClusterReport,
    ClusterStatus,
    Phase,
    StartedCluster,
    StopOutcome,
)
from app.infrastructure.database.embedded.database_setup import (
    ClusterConnection,
    PreparedDatabase,
)
from app.infrastructure.database.embedded.errors import (
    ClusterRefusedError,
    EmbeddedPostgresError,
)

PASSWORD = "p4ssw0rd-that-must-not-leak"
PORT = 51336


class FakeCluster:
    def __init__(self, *, started_here: bool) -> None:
        self.started_here = started_here
        self.calls: list[str] = []
        self.prepare_error: EmbeddedPostgresError | None = None
        self.start_error: EmbeddedPostgresError | None = None

    def start(self, on_phase: Callable[[Phase], None] | None = None) -> StartedCluster:
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error
        return StartedCluster(
            connection=ClusterConnection(port=PORT, password=PASSWORD),
            started_here=self.started_here,
            created=False,
            recovered=False,
        )

    def prepare(
        self,
        connection: ClusterConnection,
        on_phase: Callable[[Phase], None] | None = None,
    ) -> PreparedDatabase:
        self.calls.append("prepare")
        if self.prepare_error is not None:
            raise self.prepare_error
        return PreparedDatabase(
            created_database=False,
            migrated_from=None,
            heads=("6d77379a36a1",),
            warnings=("vector 0.8.5 is installed, older than this runtime's 0.8.6",),
        )

    def stop(self) -> StopOutcome:
        self.calls.append("stop")
        return StopOutcome.FAST

    def status(self) -> ClusterReport:
        return ClusterReport(
            status=ClusterStatus.READY,
            data_directory=Path(r"C:\somewhere\postgres\16\data"),
            port=PORT,
            pid=3000,
        )

    def environment(self) -> dict[str, str]:
        return {
            "DATABASE_SOURCE": "embedded",
            "EMBEDDED_POSTGRES_DIRECTORY": r"C:\somewhere\postgres",
            "EMBEDDED_POSTGRES_RUNTIME_DIRECTORY": r"C:\code\runtime\postgres",
        }


def use(monkeypatch: pytest.MonkeyPatch, cluster: FakeCluster) -> FakeCluster:
    monkeypatch.setattr(cli, "_build_cluster", lambda: cluster)
    return cluster


def child(code: str) -> list[str]:
    return ["run", "--", sys.executable, "-c", code]


SEES_THE_CLUSTER = (
    "import os, sys\n"
    "expected = {'DATABASE_SOURCE': 'embedded', "
    "'EMBEDDED_POSTGRES_DIRECTORY': r'C:\\somewhere\\postgres', "
    "'EMBEDDED_POSTGRES_RUNTIME_DIRECTORY': r'C:\\code\\runtime\\postgres'}\n"
    "found = {name: os.environ.get(name) for name in expected}\n"
    "sys.exit(0 if found == expected else 9)\n"
)

CARRIES_NO_CONNECTION = (
    "import os, sys\n"
    f"leaked = [name for name, value in os.environ.items() if '{PASSWORD}' in value]\n"
    "sys.exit(9 if leaked else 0)\n"
)


def test_run_gives_the_command_the_source_and_the_two_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, FakeCluster(started_here=True))

    assert cli.main(child(SEES_THE_CLUSTER)) == 0


def test_run_does_not_put_the_password_in_the_commands_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC-034 section 7.6: the command reads it from the cluster, as the
    port. Before, it travelled in the environment block of every child."""
    use(monkeypatch, FakeCluster(started_here=True))

    assert cli.main(child(CARRIES_NO_CONNECTION)) == 0


def test_the_commands_exit_code_is_runs_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, FakeCluster(started_here=True))

    assert cli.main(child("import sys; sys.exit(7)")) == 7


def test_run_stops_the_cluster_it_started(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = use(monkeypatch, FakeCluster(started_here=True))

    cli.main(child("pass"))

    assert cluster.calls == ["start", "prepare", "stop"]


def test_run_leaves_running_a_cluster_it_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 11: `run` leaves the cluster as it found it."""
    cluster = use(monkeypatch, FakeCluster(started_here=False))

    cli.main(child("pass"))

    assert cluster.calls == ["start", "prepare"]


def test_a_command_that_fails_does_not_leave_the_cluster_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = use(monkeypatch, FakeCluster(started_here=True))

    assert cli.main(child("raise SystemExit(3)")) == 3
    assert cluster.calls[-1] == "stop"


def test_a_database_that_is_refused_stops_what_was_started_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cluster = use(monkeypatch, FakeCluster(started_here=True))
    cluster.prepare_error = ClusterRefusedError("a newer SolidVision migrated it")
    marker = tmp_path / "the-command-ran"

    code = cli.main(child(f"open(r'{marker}', 'w').close()"))

    assert code == 1
    assert cluster.calls == ["start", "prepare", "stop"]
    assert not marker.exists()


def test_a_refused_database_on_an_adopted_cluster_stops_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = use(monkeypatch, FakeCluster(started_here=False))
    cluster.prepare_error = ClusterRefusedError("a newer SolidVision migrated it")

    assert cli.main(child("pass")) == 1
    assert cluster.calls == ["start", "prepare"]


def test_run_without_a_command_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = use(monkeypatch, FakeCluster(started_here=True))

    assert cli.main(["run"]) == 2
    assert cli.main(["run", "--"]) == 2
    assert cluster.calls == []


def test_an_adapter_error_is_a_message_and_exit_code_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = use(monkeypatch, FakeCluster(started_here=True))
    cluster.start_error = ClusterRefusedError(
        "holds a cluster of another major version"
    )

    assert cli.main(["start"]) == 1
    assert "another major version" in capsys.readouterr().err


def test_start_prepares_and_leaves_the_cluster_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = use(monkeypatch, FakeCluster(started_here=True))

    assert cli.main(["start"]) == 0
    assert cluster.calls == ["start", "prepare"]


def test_a_warning_from_prepare_reaches_the_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, FakeCluster(started_here=True))

    cli.main(["start"])

    assert "warning: vector 0.8.5" in capsys.readouterr().err


def test_stop_reports_what_happened(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = use(monkeypatch, FakeCluster(started_here=True))

    assert cli.main(["stop"]) == 0
    assert cluster.calls == ["stop"]
    assert "stopped (fast)" in capsys.readouterr().err


def test_status_says_where_and_on_which_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, FakeCluster(started_here=True))

    assert cli.main(["status"]) == 0

    output = capsys.readouterr().out
    assert "status:  ready" in output
    assert f"port:    {PORT}" in output


@pytest.mark.parametrize("arguments", [["start"], ["status"], ["stop"]])
def test_no_action_prints_the_password(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    """Section 7.2: never in a log, and not in the CLI's output either."""
    use(monkeypatch, FakeCluster(started_here=True))

    cli.main(arguments)

    captured = capsys.readouterr()
    assert PASSWORD not in captured.out + captured.err


def test_the_adapters_own_messages_go_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The command's stdout stays the command's."""
    use(monkeypatch, FakeCluster(started_here=True))

    cli.main(child("print('only this')"))

    assert capfd.readouterr().out.strip() == "only this"
