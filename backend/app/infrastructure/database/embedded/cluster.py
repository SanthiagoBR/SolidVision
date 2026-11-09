"""`EmbeddedPostgres`: o ciclo de vida do cluster, como máquina de estados (RFC-033).

    cluster = EmbeddedPostgres(
        runtime=PostgresRuntime(settings.embedded_postgres_runtime_directory),
        home=ClusterHome(settings.embedded_postgres_directory),
    )
    started = cluster.start(on_phase=report)  # cria se preciso; sobe ou adota
    cluster.prepare(started.connection)       # banco e migrations, idempotente
    cluster.status()                          # absent | stopped | starting | ready
    cluster.locate()                          # a conexão de um cluster pronto
    cluster.stop()                            # fast; immediate se o fast falhar

O que o `start()` observa, e o que faz (§9.1):

    observado                                  ação
    data.initdb existe                         apaga, depois cria (§7.1)
    data não existe                            initdb atômico
    PG_VERSION não é a major do manifesto      recusa; nunca initdb por cima
    sem postmaster.pid                         sobe
    pid vivo, este runtime, ready              adota (§8.4)
    o mesmo, starting                          espera o ready (§8.3)
    o mesmo, stopping                          espera o PID sumir, depois sobe
    pid vivo, postgres.exe de outro runtime    recusa, nomeando esse runtime
    pid morto                                  pg_ctl start; o servidor se recupera
    subida falha: memória compartilhada em uso espera, encerra só os nossos órfãos
                                               (§9.3), tenta mais uma vez
    subida falha: bind                         outra porta (§8.2, passo 4)
    qualquer outra falha                       erro com o que o servidor registrou

O adaptador nunca apaga o `postmaster.pid` (§9.2) e nunca mata o postmaster
(§8.5).
"""

from __future__ import annotations

import errno
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.infrastructure.logging.logger import get_logger

from .cluster_home import CREATE_NO_WINDOW, ClusterFiles, ClusterHome
from .database_setup import (
    LOOPBACK,
    SUPERUSER,
    ClusterConnection,
    PreparedDatabase,
    prepare_database,
    verify_login,
)
from .errors import (
    ClusterNotRunningError,
    ClusterRefusedError,
    EmbeddedPostgresError,
    ServerNotReadyError,
    StartupFailedError,
    StopFailedError,
)
from .postmaster_pid import (
    STATUS_READY,
    STATUS_STANDBY,
    STATUS_STOPPING,
    PostmasterPid,
    read_postmaster_pid,
)
from .postmaster_signal import FAST, IMMEDIATE, send
from .runtime import PostgresRuntime, RuntimeManifest
from .windows_processes import ProcessInfo, ProcessTable, WindowsProcesses

logger = get_logger(__name__)

INCLUDE_LINE = "include_if_exists = 'solidvision.conf'"

BIND_FAILURE_MARKERS = (
    "could not bind IPv4 address",
    "could not create any TCP/IP sockets",
)
"""Reconhecidos pelo texto, que é estável: este build não tem traduções, e o
`--locale=C` ainda definiu `lc_messages = 'C'` (§8.1)."""

SHARED_MEMORY_IN_USE = "pre-existing shared memory block is still in use"

ENVIRONMENT_VARIABLES = (
    "DATABASE_SOURCE",
    "EMBEDDED_POSTGRES_DIRECTORY",
    "EMBEDDED_POSTGRES_RUNTIME_DIRECTORY",
)
"""O que o `run -- <command>` dá ao comando, nomeado num só lugar (RFC-034 §7.6).

A fonte e os dois diretórios, e não mais host, porta e senha: com eles o
próprio engine do comando acha o cluster pelo `locate()`, como faz um app
instalado, e a senha não viaja no ambiente de processo nenhum.

Um teste confere que todo nome é um campo de `Settings`. Um nome escrito
errado não falharia: o `Settings` recorreria ao `.env`, o `.env` aponta para o
Docker, e a suíte passaria contra o banco errado imprimindo o mesmo `passed`
(RFC-033a §4.2).
"""

EMBEDDED_SOURCE = "embedded"
"""O valor de `DATABASE_SOURCE`, escrito aqui porque este pacote não lê
`settings`. O mesmo teste o confere contra `DatabaseSource`."""

HOW_TO_START = "python -m app.infrastructure.database.embedded start"

_WSAEACCES = 10013
_WSAEADDRINUSE = 10048

_PG_CONTROL_VERSION = 1300
_DB_SHUTDOWNED = 1
_DB_SHUTDOWNED_IN_RECOVERY = 2

POLL_SECONDS = 0.1


class Phase(Enum):
    """O que o `on_phase` informa, à medida que acontece.

    A primeira execução leva dezenas de segundos (RFC-033a §13), e uma tela
    que não se mexe por tanto tempo parece travada.
    """

    INITDB = "initdb"
    STARTING = "starting"
    RECOVERING = "recovering"
    MIGRATING = "migrating"


class ClusterStatus(Enum):
    ABSENT = "absent"
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    OTHER_VERSION = "other version"
    OTHER_RUNTIME = "other runtime"


class Observation(Enum):
    """Uma linha da tabela do §9.1: o que foi visto, antes de qualquer ação."""

    INTERRUPTED_INITDB = "interrupted initdb"
    ABSENT = "absent"
    OTHER_MAJOR = "other major version"
    STOPPED = "stopped"
    STALE = "stale postmaster.pid"
    READY = "ready"
    STARTING = "starting"
    STOPPING = "stopping"
    FOREIGN = "held by another runtime"


@dataclass(frozen=True)
class ClusterState:
    observation: Observation
    pid_file: PostmasterPid | None = None
    detail: str = ""


@dataclass(frozen=True)
class StartedCluster:
    """A conexão, e três fatos sobre como ela veio a existir (§4.2).

    O `started_here` é como o launcher do RFC-036 decide quem para o banco no
    fim: quem o subiu, nunca quem o adotou.
    """

    connection: ClusterConnection
    started_here: bool
    created: bool
    recovered: bool


@dataclass(frozen=True)
class ClusterReport:
    """`status()`: o que há agora. Nunca a senha."""

    status: ClusterStatus
    data_directory: Path
    port: int | None = None
    pid: int | None = None
    detail: str = ""


class StopOutcome(Enum):
    NOT_RUNNING = "not running"
    FAST = "stopped (fast)"
    IMMEDIATE = "stopped (immediate, after fast failed)"


@dataclass(frozen=True)
class ClusterTimeouts:
    """Toda espera que o adaptador faz, com a medição de onde cada uma vem."""

    pg_ctl_start: int = 30
    """O `T` do §8.3: uma subida normal com folga.

    Medido (RFC-033 §18): o `pg_ctl start -w` levou de 0,6 a 1,9 s numa
    subida limpa, e até 9,6 s quando o servidor tinha um crash do qual se
    recuperar. Passado o `T`, um postmaster vivo ainda em `starting` está se
    recuperando, não travado, e o adaptador continua esperando até o
    `recovery_ceiling`.
    """

    recovery_ceiling: float = 600.0
    """Quanto uma recuperação de crash pode levar antes de o adaptador desistir.

    Ele não para o servidor quando desiste. É uma escolha, não uma medição: a
    recuperação mais longa medida (RFC-033 §18) levou 13,4 s, num SSD, e a
    recuperação cresce com o WAL escrito desde o último checkpoint e com o
    disco. O que um disco lento precisa não foi medido.
    """

    pg_ctl_stop: int = 60
    lock: float = 900.0
    """Mais longo que qualquer coisa que outro dono da trava possa estar fazendo:
    um `initdb` (38,9 s, o pior que o RFC-033a mediu) mais uma subida de até
    `recovery_ceiling`."""

    orphan_grace: float = 5.0
    """Quanto tempo os filhos de um postmaster morto têm para sair sozinhos.

    Medido (RFC-033 §18): num cluster ocioso o último filho some de 0,05 a
    0,17 s depois do postmaster. Só um filho que sobrevive à carência é
    encerrado, o que aconteceu sem provocação uma vez em 27 mortes da árvore.
    """

    stopping_wait: float = 60.0
    initdb: float = 600.0
    rename: float = 10.0
    max_port_attempts: int = 3
    """Subidas seguidas recusadas pelo `bind` antes de desistir (§8.2)."""


class PortProber:
    """§8.2, passos 2 e 3: sonda uma porta, ou pede uma ao sistema."""

    def usable(self, port: int) -> bool:
        """Tenta o bind nela, e vê se o Windows deixa.

        Tanto "em uso" (`WSAEADDRINUSE`) quanto "acesso negado" (`WSAEACCES`,
        uma faixa que o Hyper-V, o WSL ou o Docker reservaram) querem dizer
        "não". A sonda pede a porta com exclusividade, então também diz "não" a
        uma porta que outro socket segura de um jeito que deixaria um segundo
        bind compartilhá-la. Um "não" errado custa um número de porta novo; um
        "sim" errado custa uma subida que falha e é tentada de novo (passo 4).
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            try:
                probe.bind((LOOPBACK, port))
            except OSError as error:
                codes = {getattr(error, "winerror", None), error.errno}
                refusals = {
                    _WSAEADDRINUSE,
                    _WSAEACCES,
                    errno.EADDRINUSE,
                    errno.EACCES,
                }
                if codes & refusals:
                    return False
                raise
            return True

    def free_port(self) -> int:
        """Porta 0: o sistema escolhe uma livre, fora das faixas excluídas."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOOPBACK, 0))
            return int(probe.getsockname()[1])


class PostgresCommands:
    """`initdb` e `pg_ctl`, rodados do jeito que o §8.3 diz.

    `CREATE_NO_WINDOW`: o `pg_ctl` recebe um console sem janela e o servidor o
    herda, então nenhuma janela preta se abre para quem chama sem console, e
    um Ctrl+C no terminal de quem chama não chega a um console que o servidor
    não divide. A saída vai sempre para um arquivo, nunca para um pipe: o
    servidor herdaria o pipe, e quem esperasse o `pg_ctl` terminar esperaria
    o servidor terminar.
    """

    def __init__(self, runtime: PostgresRuntime) -> None:
        self._runtime = runtime

    def initdb(
        self, target: Path, password_file: Path, timeout: float
    ) -> tuple[int, str]:
        """As flags do RFC-033a: scram, o arquivo de senha, `UTF8` e `C`.

        Um pipe é seguro aqui: todo processo que o `initdb` inicia já saiu
        quando ele sai.
        """
        completed = subprocess.run(
            [
                str(self._runtime.initdb),
                "-D",
                str(target),
                "-U",
                SUPERUSER,
                "--auth=scram-sha-256",
                f"--pwfile={password_file}",
                "--encoding=UTF8",
                "--locale=C",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=self._runtime.child_environment(),
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
        return completed.returncode, _decode(completed.stdout + completed.stderr)

    def clear_startup_log(self, files: ClusterFiles) -> None:
        """Esvazia o `startup.log`: ele é reescrito a cada subida (§8.1).

        Levanta `PermissionError` enquanto algum processo do servidor está
        vivo. O `pg_ctl` não inicia o `postgres.exe` ele mesmo: inicia
        `cmd.exe /D /C ""postgres.exe" -D "..." < "nul" >> "startup.log" 2>&1"`
        (lido do `cmd.exe` em execução), e daí até o último processo desse
        servidor sair o arquivo não pode ser aberto para escrita por mais
        ninguém.
        """
        files.startup_log.write_bytes(b"")

    def start(self, files: ClusterFiles, timeout: int) -> int:
        return self._pg_ctl(
            files,
            [
                "start",
                "-w",
                "-t",
                str(timeout),
                "-D",
                str(files.data),
                "-l",
                str(files.startup_log),
            ],
            timeout,
        )

    def stop(self, files: ClusterFiles, mode: str, timeout: int) -> int:
        return self._pg_ctl(
            files,
            ["stop", "-m", mode, "-w", "-t", str(timeout), "-D", str(files.data)],
            timeout,
        )

    def _pg_ctl(self, files: ClusterFiles, arguments: list[str], timeout: int) -> int:
        with files.pg_ctl_log.open("wb") as output:
            completed = subprocess.run(
                [str(self._runtime.pg_ctl), *arguments],
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=self._runtime.child_environment(),
                creationflags=CREATE_NO_WINDOW,
                timeout=timeout + 60,
            )
        return completed.returncode


def _decode(raw: bytes) -> str:
    return raw.decode("mbcs" if sys.platform == "win32" else "utf-8", "replace")


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _ignore(_: Phase) -> None:
    return None


class EmbeddedPostgres:
    """O adaptador. Não guarda estado entre chamadas: os arquivos são o estado."""

    def __init__(
        self,
        runtime: PostgresRuntime,
        home: ClusterHome,
        *,
        processes: ProcessTable | None = None,
        commands: PostgresCommands | None = None,
        ports: PortProber | None = None,
        login_check: Callable[[ClusterConnection], None] = verify_login,
        timeouts: ClusterTimeouts | None = None,
        signal: Callable[[int, int], bool] = send,
    ) -> None:
        if sys.platform != "win32":
            raise EmbeddedPostgresError(
                "EmbeddedPostgres requires Windows; this process is running on "
                f"{sys.platform!r}."
            )
        self._runtime = runtime
        self._home = home
        self._processes = processes if processes is not None else WindowsProcesses()
        self._commands = commands if commands is not None else PostgresCommands(runtime)
        self._ports = ports if ports is not None else PortProber()
        self._login_check = login_check
        self._timeouts = timeouts if timeouts is not None else ClusterTimeouts()
        self._signal = signal

    # -- o contrato (§4.2) -------------------------------------------------

    def start(self, on_phase: Callable[[Phase], None] | None = None) -> StartedCluster:
        """Cria se preciso; sobe, ou adota um cluster que já está de pé."""
        report = on_phase or _ignore
        manifest = self._runtime.manifest()
        files = self._home.files(manifest.major)
        self._home.ensure_directory(files)
        files = self._home.for_commands(files)
        with self._home.transition_lock(files, self._timeouts.lock):
            state = self._observe(files, manifest)
            if state.observation is Observation.INTERRUPTED_INITDB:
                logger.warning(
                    "Removing %s, left by an interrupted initdb", files.data_initdb
                )
                self._discard_interrupted_initdb(files)
                state = self._observe(files, manifest)
            created = False
            if state.observation is Observation.ABSENT:
                self._create(files, report)
                created = True
                state = self._observe(files, manifest)
            self._refuse_foreign(files, state)
            password = self._home.read_password(files)
            if password is None:
                raise ClusterRefusedError(
                    f"{files.data} holds a cluster, but {files.password} is "
                    "missing: the password-cluster pair is lost."
                )
            if state.observation is Observation.STOPPING:
                self._wait_until_gone(files, state)
                state = self._observe(files, manifest)
                self._refuse_foreign(files, state)
            if state.observation in (Observation.READY, Observation.STARTING):
                return self._adopt(files, state, password, report)
            recovered = not self._shut_down_cleanly(files)
            port = self._start_server(files, state, report, recovered)
            connection = ClusterConnection(port=port, password=password)
            self._login(connection, report)
            logger.info(
                "Embedded cluster started on %s:%s (created: %s, recovered: %s)",
                LOOPBACK,
                port,
                created,
                recovered,
            )
            return StartedCluster(
                connection=connection,
                started_here=True,
                created=created,
                recovered=recovered,
            )

    def prepare(
        self,
        connection: ClusterConnection,
        on_phase: Callable[[Phase], None] | None = None,
    ) -> PreparedDatabase:
        """O banco, as migrations e os fatos (§7.4)."""
        report = on_phase or _ignore
        manifest = self._runtime.manifest()
        return prepare_database(
            connection,
            major=manifest.major,
            pgvector=manifest.pgvector,
            on_migrating=lambda: report(Phase.MIGRATING),
        )

    def status(self) -> ClusterReport:
        """Uma leitura dos arquivos e da tabela de processos; não pega trava."""
        manifest = self._runtime.manifest()
        shown = self._home.files(manifest.major).data
        files = self._home.for_commands(self._home.files(manifest.major))
        if not files.data.exists():
            return ClusterReport(ClusterStatus.ABSENT, shown)
        state = self._observe(files, manifest, leftover_initdb=False)
        status = {
            Observation.OTHER_MAJOR: ClusterStatus.OTHER_VERSION,
            Observation.STOPPED: ClusterStatus.STOPPED,
            Observation.STALE: ClusterStatus.STOPPED,
            Observation.READY: ClusterStatus.READY,
            Observation.STARTING: ClusterStatus.STARTING,
            Observation.STOPPING: ClusterStatus.STOPPING,
            Observation.FOREIGN: ClusterStatus.OTHER_RUNTIME,
        }[state.observation]
        running = status in (
            ClusterStatus.READY,
            ClusterStatus.STARTING,
            ClusterStatus.STOPPING,
            ClusterStatus.OTHER_RUNTIME,
        )
        pid_file = state.pid_file if running else None
        return ClusterReport(
            status=status,
            data_directory=shown,
            port=pid_file.port if pid_file else None,
            pid=pid_file.pid if pid_file else None,
            detail=state.detail,
        )

    def locate(self) -> ClusterConnection:
        """A conexão de um cluster que está de pé. Lê; não sobe nada.

        RFC-034 §7.2. Sem trava, sem conexão, sem espera: a porta que o
        servidor está *usando*, da linha 4 do `postmaster.pid`, e a senha do
        arquivo dela, depois das conferências que o `status()` faz -- o PID
        está vivo, o executável é deste runtime, e o arquivo de trava nomeia
        este `data\\`. Qualquer coisa que não seja `ready` é
        `ClusterNotRunningError`.

        `ready` no arquivo não é `ready` no servidor (RFC-033 §8.4). Uma
        conexão aberta enquanto o servidor se reinicializa é recusada pelo
        servidor, e quem chama recebe um erro de banco comum.
        """
        manifest = self._runtime.manifest()
        shown = self._home.files(manifest.major).data
        files = self._home.for_commands(self._home.files(manifest.major))
        if not files.data.exists():
            raise ClusterNotRunningError(
                f"There is no embedded cluster in {shown} yet. "
                f"Create and start it with `{HOW_TO_START}`."
            )
        state = self._observe(files, manifest, leftover_initdb=False)
        if state.observation is not Observation.READY or state.pid_file is None:
            found = state.observation.value
            if state.detail:
                found = f"{found} ({state.detail})"
            raise ClusterNotRunningError(
                f"The embedded cluster in {shown} is not ready to be connected "
                f"to: {found}. Start it with `{HOW_TO_START}`."
            )
        if state.pid_file.port is None:
            raise ClusterNotRunningError(
                f"The embedded cluster in {shown} is up, but "
                f"{files.postmaster_pid} names no port yet."
            )
        password = self._home.read_password(files)
        if password is None:
            raise ClusterRefusedError(
                f"{shown} holds a running cluster, but {files.password} is "
                "missing: the password-cluster pair is lost."
            )
        return ClusterConnection(port=state.pid_file.port, password=password)

    def environment(self) -> dict[str, str]:
        """`ENVIRONMENT_VARIABLES` para um processo filho, apontando-o a este cluster.

        Acima do `.env` num checkout (RFC-004). Nem a porta nem a senha estão
        aqui: o filho lê as duas do cluster, pelo `locate()`.
        """
        return {
            "DATABASE_SOURCE": EMBEDDED_SOURCE,
            "EMBEDDED_POSTGRES_DIRECTORY": str(self._home.root),
            "EMBEDDED_POSTGRES_RUNTIME_DIRECTORY": str(self._runtime.root),
        }

    def stop(self) -> StopOutcome:
        """`fast`; `immediate` se o `fast` falhar; erro se os dois falharem (§8.5)."""
        manifest = self._runtime.manifest()
        files = self._home.for_commands(self._home.files(manifest.major))
        if not files.data.exists():
            return StopOutcome.NOT_RUNNING
        with self._home.transition_lock(files, self._timeouts.lock):
            state = self._observe(files, manifest, leftover_initdb=False)
            self._refuse_foreign(files, state)
            running = (Observation.READY, Observation.STARTING, Observation.STOPPING)
            if state.observation not in running:
                return StopOutcome.NOT_RUNNING
            assert state.pid_file is not None
            postmaster = state.pid_file.pid
            timeout = self._timeouts.pg_ctl_stop
            if self._commands.stop(files, "fast", timeout) == 0:
                self._wait_for_exit(postmaster)
                logger.info("Embedded cluster stopped (fast)")
                return StopOutcome.FAST
            logger.warning("Fast shutdown failed; trying immediate")
            if self._commands.stop(files, "immediate", timeout) == 0:
                self._wait_for_exit(postmaster)
                return StopOutcome.IMMEDIATE
            raise StopFailedError(
                f"Neither a fast nor an immediate shutdown stopped the cluster in "
                f"{files.data}. The adapter does not kill the postmaster. "
                f"pg_ctl said:\n{_tail(self._read_text(files.pg_ctl_log))}"
            )

    def stop_by_signal(self, timeout: float) -> StopOutcome:
        """`stop()` sem iniciar processo: para uma sessão que está terminando.

        RFC-036 §7.5. Durante um logoff o `pg_ctl` não consegue subir, então o
        postmaster recebe o sinal que o `pg_ctl` teria mandado, pelo pipe de
        sinais dele: `fast`, e `immediate` se o servidor ainda estiver lá
        `timeout` depois. Cada um espera os processos sumirem, não o
        `postmaster.pid` -- a próxima subida não acha servidor de um jeito ou
        de outro. Até `timeout` também para a trava: num logoff, ninguém mais
        deveria segurá-la, e a sessão não vai esperar 15 minutos. As mesmas
        recusas e o mesmo erro do `stop()`.
        """
        manifest = self._runtime.manifest()
        files = self._home.for_commands(self._home.files(manifest.major))
        if not files.data.exists():
            return StopOutcome.NOT_RUNNING
        with self._home.transition_lock(files, timeout):
            state = self._observe(files, manifest, leftover_initdb=False)
            self._refuse_foreign(files, state)
            running = (Observation.READY, Observation.STARTING, Observation.STOPPING)
            if state.observation not in running:
                return StopOutcome.NOT_RUNNING
            assert state.pid_file is not None
            postmaster = state.pid_file.pid
            # Listados antes do sinal: um processo de saída pode não dizer mais
            # o que roda, e passaria por sumido (medido).
            server = sorted({postmaster, *self._server_processes(postmaster)})
            if self._signal(postmaster, FAST) and self._gone(server, timeout):
                logger.info("Embedded cluster stopped (fast, by its signal pipe)")
                return StopOutcome.FAST
            logger.warning("Fast shutdown by the signal pipe failed; trying immediate")
            if self._signal(postmaster, IMMEDIATE) and self._gone(server, timeout):
                return StopOutcome.IMMEDIATE
            raise StopFailedError(
                f"Neither a fast nor an immediate shutdown, sent through the "
                f"signal pipe of PID {postmaster}, stopped the cluster in "
                f"{files.data} within {timeout:.0f} s each. The adapter does not "
                "kill the postmaster."
            )

    def _gone(self, server: list[int], timeout: float) -> bool:
        """Se todo processo em `server` saiu dentro de `timeout`."""
        deadline = time.monotonic() + timeout
        while any(self._processes.is_alive(pid) for pid in server):
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_SECONDS / 10)
        return True

    def _server_processes(self, postmaster: int) -> list[int]:
        """O postmaster e os filhos dele que existem agora, por identidade.

        Cada um contado aqui é um programa deste runtime, e não só um número.
        Um processo registra o PID do pai uma vez e o Windows nunca o atualiza,
        então um processo que nada tem a ver com o cluster aparece como "filho"
        do postmaster sempre que o seu próprio pai, há muito sumido, tinha o
        número que o postmaster tem agora -- na máquina em que isto foi medido,
        18 de 328 processos nomeavam um PID de pai que não era mais de ninguém.
        """
        return [
            process.pid
            for process in self._processes.snapshot()
            if (process.pid == postmaster or process.parent_pid == postmaster)
            and self._ours(process.pid)
        ]

    def _wait_for_exit(self, postmaster: int) -> None:
        """Volta do `stop()` só quando os processos sumiram, não só a trava.

        O `pg_ctl stop -w` espera o `postmaster.pid` desaparecer, o que o
        postmaster faz ao sair; os próprios processos ainda ficam lá por um
        momento depois. Quem ouviu "parado" não deveria achar `postgres.exe`
        deste cluster, e um `start()` em seguida deveria achar o `startup.log`
        livre. Ninguém é encerrado aqui: a trava foi liberada, e o que sobrou
        está de saída.
        """
        deadline = time.monotonic() + self._timeouts.orphan_grace
        while self._server_processes(postmaster):
            if time.monotonic() >= deadline:
                logger.warning(
                    "The cluster released its lock, but some of its processes "
                    "are still exiting after %.0f s",
                    self._timeouts.orphan_grace,
                )
                return
            time.sleep(POLL_SECONDS / 10)

    # -- observar (§9.1) ---------------------------------------------------

    def _observe(
        self,
        files: ClusterFiles,
        manifest: RuntimeManifest,
        *,
        leftover_initdb: bool = True,
    ) -> ClusterState:
        if leftover_initdb and files.data_initdb.exists():
            return ClusterState(Observation.INTERRUPTED_INITDB)
        if not files.data.exists():
            return ClusterState(Observation.ABSENT)
        version = self._read_text(files.pg_version).strip()
        if version != str(manifest.major):
            return ClusterState(
                Observation.OTHER_MAJOR,
                detail=f"PG_VERSION says {version or 'nothing'!r}; this runtime is "
                f"PostgreSQL {manifest.major}",
            )
        pid_file = read_postmaster_pid(files.postmaster_pid)
        if pid_file is None:
            if files.postmaster_pid.exists():
                return ClusterState(
                    Observation.STALE, detail="postmaster.pid has no PID"
                )
            return ClusterState(Observation.STOPPED)
        if not self._processes.is_alive(pid_file.pid):
            return ClusterState(Observation.STALE, pid_file)
        executable = self._processes.executable(pid_file.pid)
        if executable is None or executable.name.lower() != "postgres.exe":
            # Um PID reusado por outro programa, ou um que este usuário não
            # consegue inspecionar. Velho até onde o adaptador consegue ver; na
            # subida, o próprio servidor decide se a trava vale (§9.2).
            return ClusterState(
                Observation.STALE,
                pid_file,
                detail=f"PID {pid_file.pid} is {executable or 'not inspectable'}",
            )
        if not self._runtime.contains(executable):
            return ClusterState(Observation.FOREIGN, pid_file, detail=str(executable))
        if not pid_file.points_to(files.data):
            return ClusterState(
                Observation.STALE,
                pid_file,
                detail=f"postmaster.pid names {pid_file.data_directory}",
            )
        if pid_file.status == STATUS_STOPPING:
            return ClusterState(Observation.STOPPING, pid_file)
        if pid_file.status in (STATUS_READY, STATUS_STANDBY):
            return ClusterState(Observation.READY, pid_file)
        return ClusterState(Observation.STARTING, pid_file)

    def _refuse_foreign(self, files: ClusterFiles, state: ClusterState) -> None:
        if state.observation is Observation.OTHER_MAJOR:
            raise ClusterRefusedError(
                f"{files.data} holds a cluster of another major version "
                f"({state.detail}). It is left untouched: never an initdb over it, "
                "never a deletion."
            )
        if state.observation is Observation.FOREIGN:
            raise ClusterRefusedError(
                f"The cluster in {files.data} is running from another runtime: "
                f"{state.detail}. Stop it there, or point this one elsewhere with "
                "EMBEDDED_POSTGRES_DIRECTORY."
            )

    def _shut_down_cleanly(self, files: ClusterFiles) -> bool:
        """Se o `pg_control` diz que o último desligamento foi limpo.

        Lido antes da subida, para que `recovered` seja sabido em vez de
        inferido de um log: o estado é o quinto campo de 32 bits do
        `pg_control`, cuja versão de layout 1300 não mudou desde o PostgreSQL 13.
        """
        try:
            with files.pg_control.open("rb") as handle:
                header = handle.read(20)
        except OSError:
            return True
        if len(header) < 20:
            return True
        version = int.from_bytes(header[8:12], "little")
        state = int.from_bytes(header[16:20], "little")
        if version != _PG_CONTROL_VERSION:
            return True
        return state in (_DB_SHUTDOWNED, _DB_SHUTDOWNED_IN_RECOVERY)

    # -- criar (§7.1) ------------------------------------------------------

    def _create(self, files: ClusterFiles, report: Callable[[Phase], None]) -> None:
        report(Phase.INITDB)
        logger.info("Creating the embedded cluster in %s", files.data)
        self._home.secure()
        self._home.discard_password(files)
        self._home.write_new_password(files)
        returncode, output = self._commands.initdb(
            files.data_initdb, files.password, self._timeouts.initdb
        )
        if returncode != 0:
            shutil.rmtree(files.data_initdb, ignore_errors=True)
            raise StartupFailedError(f"initdb exited {returncode}:\n{_tail(output)}")
        conf = files.data_initdb / files.postgresql_conf.name
        with conf.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# SolidVision (RFC-033 section 8.1)\n{INCLUDE_LINE}\n")
        self._rename(files.data_initdb, files.data)

    def _rename(self, source: Path, target: Path) -> None:
        """Atômico num volume; tentado de novo enquanto algo segura um handle.

        Um scanner abrindo os arquivos que o `initdb` acabou de escrever pode
        fazer o Windows recusar o rename de um diretório por um momento.
        """
        deadline = time.monotonic() + self._timeouts.rename
        while True:
            try:
                source.rename(target)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(POLL_SECONDS)

    def _discard_interrupted_initdb(self, files: ClusterFiles) -> None:
        """Apaga um `data.initdb` deixado por uma criação que não terminou.

        Um `initdb` deste runtime ainda rodando -- órfão de um pai morto sozinho
        -- é esperado primeiro: senão, continuaria escrevendo no diretório que
        está sendo apagado.
        """
        deadline = time.monotonic() + self._timeouts.initdb
        while self._running_initdb() and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        if self._running_initdb():
            raise EmbeddedPostgresError(
                f"An initdb from {self._runtime.root} is still running; "
                f"{files.data_initdb} is left in place until it ends."
            )
        deadline = time.monotonic() + self._timeouts.rename
        while files.data_initdb.exists():
            shutil.rmtree(files.data_initdb, ignore_errors=True)
            if files.data_initdb.exists():
                if time.monotonic() >= deadline:
                    raise EmbeddedPostgresError(
                        f"{files.data_initdb} could not be removed."
                    )
                time.sleep(POLL_SECONDS)

    def _running_initdb(self) -> list[ProcessInfo]:
        return [
            process
            for process in self._processes.snapshot()
            if process.name.lower() == "initdb.exe" and self._ours(process.pid)
        ]

    # -- subir (§8.2 e §8.3) -------------------------------------------------

    def _start_server(
        self,
        files: ClusterFiles,
        state: ClusterState,
        report: Callable[[Phase], None],
        recovery_expected: bool,
    ) -> int:
        stale_pid = (
            state.pid_file.pid
            if state.observation is Observation.STALE and state.pid_file
            else None
        )
        report(Phase.STARTING)
        if recovery_expected:
            report(Phase.RECOVERING)
        port = self._configured_port(files)
        bind_failures = 0
        orphans_cleared = False
        while True:
            if port is None or not self._ports.usable(port):
                port = self._ports.free_port()
            self._write_server_conf(files, port)
            self._clear_startup_log(files, stale_pid)
            marks = self._server_log_marks(files)
            returncode = self._commands.start(files, self._timeouts.pg_ctl_start)
            # O código de saída do `pg_ctl` é uma pista, não a resposta. No
            # Windows ele não consegue casar o `postmaster.pid` com o processo
            # que iniciou -- o que ele iniciou é o `cmd.exe` --, então aceita
            # qualquer arquivo cujo horário de início tenha no máximo dois
            # segundos, inclusive o arquivo *velho* de um postmaster que acabou
            # de ser morto, que ainda diz `ready`. A resposta é um postmaster
            # vivo deste runtime no arquivo, e o que esse arquivo diz.
            pid_file = self._live_postmaster(files, marks, wait=returncode == 0)
            if pid_file is not None:
                if pid_file.status not in (STATUS_READY, STATUS_STANDBY):
                    # Vivo e ainda em `starting` depois de o `-t` se esgotar, ou
                    # depois de um `ready` velho: recuperação de crash, não um
                    # travamento (§8.3).
                    if not recovery_expected:
                        report(Phase.RECOVERING)
                    pid_file = self._wait_until_ready(files, pid_file, marks)
                break
            log = self._start_output(files, marks)
            if any(marker in log for marker in BIND_FAILURE_MARKERS):
                bind_failures += 1
                logger.warning(
                    "Port %s refused by bind (attempt %s)", port, bind_failures
                )
                if bind_failures >= self._timeouts.max_port_attempts:
                    raise StartupFailedError(
                        f"The server could not bind a port {bind_failures} times in "
                        f"a row. What it logged:\n{_tail(log)}"
                    )
                port = None
                continue
            if (
                SHARED_MEMORY_IN_USE in log
                and stale_pid is not None
                and not orphans_cleared
            ):
                orphans_cleared = True
                self._clear_orphans(stale_pid)
                continue
            raise StartupFailedError(
                f"pg_ctl start exited {returncode}. What the server logged "
                f"({files.startup_log}, then {files.server_log_directory}):"
                f"\n{_tail(log)}"
            )
        if pid_file.port is None:
            raise StartupFailedError(
                f"The server started, but {files.postmaster_pid} has no port."
            )
        return pid_file.port

    def _live_postmaster(
        self, files: ClusterFiles, marks: dict[Path, int], *, wait: bool
    ) -> PostmasterPid | None:
        """O `postmaster.pid` de um servidor vivo deste runtime, se houver um.

        Com `wait`, por tanto tempo quanto uma subida normal pode levar: o
        `pg_ctl` disse que o servidor está de pé, então um arquivo que ainda não
        o mostra é um que o novo postmaster não reescreveu. Um `FATAL` no
        `startup.log`, ou um `bind` recusado no log do próprio servidor,
        encerra a espera antes -- esse servidor não vem.
        """
        deadline = time.monotonic() + (self._timeouts.pg_ctl_start if wait else 0)
        while True:
            pid_file = read_postmaster_pid(files.postmaster_pid)
            if pid_file is not None and self._is_our_postmaster(files, pid_file):
                return pid_file
            if time.monotonic() >= deadline:
                return None
            if "FATAL" in self._read_text(files.startup_log):
                return None
            written = self._server_log_since(files, marks)
            if any(marker in written for marker in BIND_FAILURE_MARKERS):
                return None
            time.sleep(POLL_SECONDS)

    # -- o que o servidor disse (§8.1) ---------------------------------------

    def _server_log_marks(self, files: ClusterFiles) -> dict[Path, int]:
        """O tamanho de cada arquivo do collector, antes de uma subida.

        Os arquivos são um por dia da semana e toda subida daquele dia escreve
        no fim do mesmo, então "o que esta subida registrou" é o que fica além
        desses tamanhos.
        """
        marks: dict[Path, int] = {}
        if files.server_log_directory.is_dir():
            for path in files.server_log_directory.iterdir():
                try:
                    marks[path] = path.stat().st_size
                except OSError:
                    continue
        return marks

    def _server_log_since(self, files: ClusterFiles, marks: dict[Path, int]) -> str:
        if not files.server_log_directory.is_dir():
            return ""
        written = []
        for path in sorted(files.server_log_directory.iterdir()):
            try:
                with path.open("rb") as handle:
                    mark = marks.get(path, 0)
                    # Um arquivo menor que a sua marca foi truncado por uma
                    # rotação: ele é todo novo.
                    handle.seek(mark if path.stat().st_size >= mark else 0)
                    written.append(_decode(handle.read()))
            except OSError:
                continue
        return "".join(written)

    def _start_output(self, files: ClusterFiles, marks: dict[Path, int]) -> str:
        """Tudo o que o servidor registrou nesta subida, na ordem em que registrou.

        **Dois logs, e não a divisão que o RFC-033 §8.1 supôs primeiro.** O
        PostgreSQL 16 sobe o logging collector *antes* de abrir os sockets,
        então o `startup.log` -- `pg_ctl -l` -- guarda só o que vem antes ainda:
        o arquivo de trava, a configuração, a memória compartilhada. Um `bind`
        recusado está no arquivo do collector, com tudo o que vem depois.
        """
        return self._read_text(files.startup_log) + self._server_log_since(files, marks)

    def _clear_startup_log(self, files: ClusterFiles, stale_pid: int | None) -> None:
        """Reescreve o `startup.log`, esperando quem ainda o segura largar.

        Depois que um postmaster morre à força, os filhos dele são a primeira
        coisa no caminho: seguram o `startup.log` até sair, antes que qualquer
        servidor novo chegue longe o bastante para achar a memória
        compartilhada em uso. Então a regra dos órfãos do §9.3 é aplicada aqui
        também -- espera a carência, encerra só os órfãos que são deste
        cluster, e espera de novo. Sem postmaster morto para nomear, ninguém é
        encerrado.
        """
        if self._startup_log_cleared(files):
            return
        if stale_pid is not None:
            self._clear_orphans(stale_pid)
        deadline = time.monotonic() + self._timeouts.orphan_grace
        while not self._startup_log_cleared(files):
            if time.monotonic() >= deadline:
                raise StartupFailedError(
                    f"{files.startup_log} is held open by another process -- a "
                    "server process of this cluster that has not exited. The "
                    "adapter ends only the children of this cluster's own dead "
                    "postmaster."
                )
            time.sleep(POLL_SECONDS)

    def _startup_log_cleared(self, files: ClusterFiles) -> bool:
        try:
            self._commands.clear_startup_log(files)
        except PermissionError:
            return False
        return True

    def _configured_port(self, files: ClusterFiles) -> int | None:
        """A porta da última subida, como o `solidvision.conf` a pediu."""
        for line in self._read_text(files.server_conf).splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "port":
                digits = value.split("#", 1)[0].strip()
                if digits.isdigit():
                    return int(digits)
        return None

    def _write_server_conf(self, files: ClusterFiles, port: int) -> None:
        """§8.1. Nada passa pelo `pg_ctl -o`: quem roda o `pg_ctl` à mão para
        diagnosticar um problema sobe o mesmo servidor, na mesma porta."""
        files.server_conf.write_text(
            "# Written by the SolidVision embedded adapter before every start\n"
            "# (RFC-033 section 8.1). Edits here are overwritten.\n"
            f"listen_addresses = '{LOOPBACK}'\n"
            f"port = {port}\n"
            "log_line_prefix = '%m [%p] '\n"
            "logging_collector = on\n"
            f"log_directory = '{files.server_log_directory.name}'\n"
            "log_filename = 'postgresql-%a.log'\n"
            "log_rotation_age = 1d\n"
            "log_rotation_size = 0\n"
            "log_truncate_on_rotation = on\n",
            encoding="utf-8",
        )

    def _wait_until_ready(
        self,
        files: ClusterFiles,
        pid_file: PostmasterPid,
        marks: dict[Path, int] | None = None,
    ) -> PostmasterPid:
        if marks is None:
            marks = self._server_log_marks(files)
        deadline = time.monotonic() + self._timeouts.recovery_ceiling
        while True:
            current = read_postmaster_pid(files.postmaster_pid)
            if current is not None and current.pid == pid_file.pid:
                if current.status in (STATUS_READY, STATUS_STANDBY):
                    return current
            if not self._processes.is_alive(pid_file.pid):
                raise StartupFailedError(
                    f"The server (PID {pid_file.pid}) exited while starting. What "
                    f"it logged:\n{_tail(self._start_output(files, marks))}"
                )
            if time.monotonic() >= deadline:
                raise StartupFailedError(
                    f"The server (PID {pid_file.pid}) is still starting after "
                    f"{self._timeouts.recovery_ceiling:.0f} s. It was left running."
                )
            time.sleep(POLL_SECONDS)

    def _wait_until_gone(self, files: ClusterFiles, state: ClusterState) -> None:
        assert state.pid_file is not None
        deadline = time.monotonic() + self._timeouts.stopping_wait
        while self._processes.is_alive(state.pid_file.pid):
            if time.monotonic() >= deadline:
                raise StartupFailedError(
                    f"The server (PID {state.pid_file.pid}) has been stopping for "
                    f"more than {self._timeouts.stopping_wait:.0f} s."
                )
            time.sleep(POLL_SECONDS)

    def _adopt(
        self,
        files: ClusterFiles,
        state: ClusterState,
        password: str,
        report: Callable[[Phase], None],
    ) -> StartedCluster:
        """§8.4: este diretório, este runtime, e a senha funciona."""
        assert state.pid_file is not None
        pid_file = state.pid_file
        if state.observation is Observation.STARTING:
            report(Phase.RECOVERING)
            pid_file = self._wait_until_ready(files, pid_file)
        if pid_file.port is None:
            raise StartupFailedError(f"{files.postmaster_pid} has no port.")
        connection = ClusterConnection(port=pid_file.port, password=password)
        self._login(connection, report)
        logger.info("Adopted the embedded cluster on %s:%s", LOOPBACK, pid_file.port)
        return StartedCluster(
            connection=connection, started_here=False, created=False, recovered=False
        )

    def _login(
        self, connection: ClusterConnection, report: Callable[[Phase], None]
    ) -> None:
        """A terceira condição do §8.4, e a palavra final sobre "pronto".

        Um servidor se reinicializando depois que um dos seus próprios
        processos morreu ainda tem `ready` no `postmaster.pid`; quem diz a
        verdade é a conexão, recusada com "in recovery mode". Essa recusa é
        esperada, como qualquer outra recuperação, até o mesmo teto. Uma senha
        rejeitada não é.
        """
        deadline = time.monotonic() + self._timeouts.recovery_ceiling
        reported = False
        while True:
            try:
                self._login_check(connection)
                return
            except ServerNotReadyError:
                if time.monotonic() >= deadline:
                    raise
                if not reported:
                    report(Phase.RECOVERING)
                    reported = True
                time.sleep(POLL_SECONDS)

    # -- órfãos (§9.3) -------------------------------------------------------

    def _ours(self, pid: int) -> bool:
        executable = self._processes.executable(pid)
        return executable is not None and self._runtime.contains(executable)

    def _is_our_postmaster(self, files: ClusterFiles, pid_file: PostmasterPid) -> bool:
        return (
            self._processes.is_alive(pid_file.pid)
            and self._ours(pid_file.pid)
            and pid_file.points_to(files.data)
        )

    def _orphans_of(self, dead_pid: int) -> list[ProcessInfo]:
        """Filhos do postmaster morto *deste* diretório de dados, *deste* runtime.

        As duas condições, sempre juntas: são elas que protegem um PostgreSQL
        que o usuário instalou e o cluster de outra conta do Windows na mesma
        máquina. E nada se o PID "morto" estiver vivo de novo -- reusado, então
        os filhos dele não são os procurados.
        """
        if self._processes.is_alive(dead_pid):
            return []
        return [
            process
            for process in self._processes.snapshot()
            if process.parent_pid == dead_pid
            and process.pid != dead_pid
            and self._ours(process.pid)
        ]

    def _clear_orphans(self, dead_pid: int) -> None:
        deadline = time.monotonic() + self._timeouts.orphan_grace
        orphans = self._orphans_of(dead_pid)
        while orphans and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            orphans = self._orphans_of(dead_pid)
        for orphan in orphans:
            logger.warning(
                "Ending PID %s, a child of the dead postmaster %s that outlived "
                "the %.0f s grace",
                orphan.pid,
                dead_pid,
                self._timeouts.orphan_grace,
            )
            self._processes.terminate(orphan.pid)
        deadline = time.monotonic() + self._timeouts.orphan_grace
        while self._orphans_of(dead_pid) and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return _decode(path.read_bytes())
        except FileNotFoundError:
            return ""
