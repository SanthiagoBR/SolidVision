"""The embedded cluster from a terminal, until RFC-034 and RFC-036 (RFC-033 11).

    python -m app.infrastructure.database.embedded start
    python -m app.infrastructure.database.embedded status
    python -m app.infrastructure.database.embedded run -- python -m pytest
    python -m app.infrastructure.database.embedded stop

`run -- <command>` starts the cluster, prepares the database, and runs the
command with `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_USER`,
`DATABASE_PASSWORD` and `DATABASE_NAME` in its environment, which
`pydantic-settings` puts above the `.env` (RFC-004). The command's exit code
is `run`'s exit code. **`run` leaves the cluster as it found it**: if it
started the cluster it stops it at the end, and if it adopted one it does not.

This is the composition root of the adapter, and the only place in the
package that reads `settings`. Everything it prints about itself goes to
stderr, so the command's stdout stays the command's; the password is never
printed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence

from app.infrastructure.config.settings import settings

from .cluster import EmbeddedPostgres, Phase, StartedCluster
from .cluster_home import ClusterHome
from .errors import EmbeddedPostgresError
from .runtime import PostgresRuntime

PHASE_MESSAGES = {
    Phase.INITDB: "creating the cluster (first run; this takes a while)",
    Phase.STARTING: "starting the server",
    Phase.RECOVERING: "recovering from an unclean shutdown",
    Phase.MIGRATING: "applying migrations",
}


def _say(message: str) -> None:
    print(f"embedded postgres: {message}", file=sys.stderr, flush=True)


def _report(phase: Phase) -> None:
    _say(PHASE_MESSAGES[phase])


def _build_cluster() -> EmbeddedPostgres:
    return EmbeddedPostgres(
        runtime=PostgresRuntime(settings.embedded_postgres_runtime_directory),
        home=ClusterHome(settings.embedded_postgres_directory),
    )


def _start_and_prepare(cluster: EmbeddedPostgres) -> StartedCluster:
    started = cluster.start(on_phase=_report)
    how = "started" if started.started_here else "adopted, already running"
    facts = [how]
    if started.created:
        facts.append("created now")
    if started.recovered:
        facts.append("recovered from an unclean shutdown")
    _say(
        f"ready on {started.connection.host}:{started.connection.port} "
        f"({', '.join(facts)})"
    )
    try:
        prepared = cluster.prepare(started.connection, on_phase=_report)
    except BaseException:
        if started.started_here:
            cluster.stop()
        raise
    for warning in prepared.warnings:
        _say(f"warning: {warning}")
    _say(f"database {started.connection.database} at {', '.join(prepared.heads)}")
    return started


def _run_command(command: Sequence[str], environment: dict[str, str]) -> int:
    """Run the command to its end, through a Ctrl+C if one comes.

    A Ctrl+C in the terminal reaches the command too -- it shares the
    console -- so the first one is the command's to handle, and this waits
    for it. A second one ends the command.
    """
    process = subprocess.Popen(list(command), env=environment)
    interrupted = False
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            if interrupted:
                process.terminate()
            interrupted = True


def _start(cluster: EmbeddedPostgres) -> int:
    _start_and_prepare(cluster)
    return 0


def _stop(cluster: EmbeddedPostgres) -> int:
    _say(cluster.stop().value)
    return 0


def _status(cluster: EmbeddedPostgres) -> int:
    report = cluster.status()
    print(f"status:  {report.status.value}")
    print(f"data:    {report.data_directory}")
    print(f"runtime: {settings.embedded_postgres_runtime_directory}")
    if report.port is not None:
        print(f"port:    {report.port}")
    if report.pid is not None:
        print(f"pid:     {report.pid}")
    if report.detail:
        print(f"detail:  {report.detail}")
    return 0


def _run(cluster: EmbeddedPostgres, command: Sequence[str]) -> int:
    if not command:
        _say("run needs a command: run -- <command> [arguments]")
        return 2
    started = _start_and_prepare(cluster)
    try:
        environment = os.environ.copy()
        environment.update(started.connection.environment())
        return _run_command(command, environment)
    finally:
        if started.started_here:
            _say(cluster.stop().value)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.database.embedded",
        description="The embedded PostgreSQL cluster (RFC-033).",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser(
        "start", help="Create the cluster if needed, start it, prepare the database."
    )
    subparsers.add_parser("stop", help="Stop the cluster (fast, then immediate).")
    subparsers.add_parser("status", help="Say what is there. Never the password.")
    run_parser = subparsers.add_parser(
        "run",
        help="Run a command with DATABASE_* pointing at the cluster, and leave "
        "the cluster as it was found.",
    )
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_arg_parser().parse_args(argv)
    try:
        cluster = _build_cluster()
        if arguments.action == "start":
            return _start(cluster)
        if arguments.action == "stop":
            return _stop(cluster)
        if arguments.action == "status":
            return _status(cluster)
        command = list(arguments.command)
        if command[:1] == ["--"]:
            command = command[1:]
        return _run(cluster, command)
    except EmbeddedPostgresError as error:
        _say(f"error: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
