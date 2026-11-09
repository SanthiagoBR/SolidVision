"""O cluster embarcado a partir de um terminal, até o RFC-036 (RFC-033 §11).

    python -m app.infrastructure.database.embedded start
    python -m app.infrastructure.database.embedded status
    python -m app.infrastructure.database.embedded run -- python -m pytest
    python -m app.infrastructure.database.embedded stop

`run -- <command>` sobe o cluster, prepara o banco e roda o comando com
`DATABASE_SOURCE=embedded`, `EMBEDDED_POSTGRES_DIRECTORY` e
`EMBEDDED_POSTGRES_RUNTIME_DIRECTORY` no ambiente dele, que o
`pydantic-settings` põe acima do `.env` de um checkout (RFC-004). Nem a porta
nem a senha são exportadas: o engine do comando lê as duas do cluster a cada
conexão nova, como faz um app instalado (RFC-034 §7.6). Nem as variáveis
`PG*` de quem chama: o adaptador as tira deste processo antes de conectar
(§7.5), e o comando recebe o que sobrou. O código de saída do comando é o
código de saída do `run`. **O `run` deixa o cluster como o encontrou**: se
subiu o cluster, para-o no fim; se adotou um, não para.

Esta é a raiz de composição do adaptador, e o único lugar do pacote que lê
`settings`. Tudo o que ele imprime sobre si mesmo vai para o stderr, de modo
que o stdout do comando continua sendo do comando; a senha nunca é impressa.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence

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


def _found_on_path(command: Sequence[str], environment: Mapping[str, str]) -> list[str]:
    """O comando com o programa achado no `PATH`, como um terminal o acha.

    Dado um nome sem caminho, o Windows procura primeiro na pasta do
    executável em execução. Num venv, essa é a pasta do interpretador base:
    o `python.exe` do venv é um launcher que inicia o base. Assim, `run --
    python scripts/check.py`, digitado num venv ativado, rodava um Python sem
    nenhum dos pacotes do venv (RFC-048 §17.12). Um nome que o `PATH` não tem
    fica como foi digitado.
    """
    found = shutil.which(command[0], path=environment.get("PATH"))
    program = os.path.abspath(found) if found is not None else command[0]
    return [program, *command[1:]]


def _run_command(command: Sequence[str], environment: dict[str, str]) -> int:
    """Roda o comando até o fim, passando por um Ctrl+C se vier um.

    Um Ctrl+C no terminal chega também ao comando -- ele divide o console --,
    então o primeiro cabe ao comando tratar, e esta função espera por ele. Um
    segundo encerra o comando.
    """
    process = subprocess.Popen(_found_on_path(command, environment), env=environment)
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
        environment.update(cluster.environment())
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
        help="Run a command with its settings pointing at the cluster, and leave "
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
