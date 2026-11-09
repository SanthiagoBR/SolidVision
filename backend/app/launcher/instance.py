r"""Um launcher por raiz de dados: o lock, o estado, o evento de parada (RFC-036 §8).

    <data>\launcher.lock    detido com exclusividade pela vida inteira do launcher
    <data>\launcher.json    o que ele está fazendo, reescrito num único rename
    Local\SolidVision-launcher-<16 hex do sha256 da raiz de dados>
                            o evento nomeado que `python -m app.launcher stop`
                            sinaliza

**O lock é a verdade sobre haver um launcher.** O Windows o libera quando o
processo termina, como quer que termine, então não há lock velho a limpar. O
arquivo de estado nunca diz se há um launcher no ar: um cujo lock está livre foi
deixado por um launcher que já se foi, e é lido para duas coisas apenas -- a
porta do último início que chegou a `ready` (§5.4) e se o cluster que talvez
ainda esteja no ar foi iniciado por um launcher (§7.3).

Por raiz de dados, e não por usuário, porque a raiz de dados é o que não pode ter
dois donos: o cluster, os logs, as thumbnails. Um checkout (`SolidVision-dev`) e
um app instalado (`SolidVision`) rodam lado a lado, cada um com a sua.

Nada aqui importa o `Settings` nem o logger: o segundo launcher lê o arquivo de
estado e sai sem nenhum dos dois (§8). Os seus registros vão para o logger padrão
do seu nome, que o logger do app assume depois de configurado.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("app.app.launcher.instance")

LOCK_NAME = "launcher.lock"
STATE_NAME = "launcher.json"
STATE_FORMAT = 1
EVENT_PREFIX = "Local\\SolidVision-launcher-"

ADMINISTRATORS_SID = "S-1-5-32-544"

LOCK_ATTEMPTS = 4
LOCK_PAUSE_SECONDS = 0.05
"""Um lock ocupado é tentado de novo por 150 ms antes de significar "outro launcher".

`status` e `stop` tomam o lock por um instante para ver se ele está detido, e um
launcher que iniciasse nesse instante tomaria isso, de outro modo, por um outro.
"""

REPLACE_SECONDS = 2.0
"""Quanto tempo o rename do arquivo de estado é retentado enquanto um leitor o lê.

O Python abre arquivos sem `FILE_SHARE_DELETE`, então um `status` lendo o estado
nesse instante faz o Windows recusar a substituição.
"""

_WAIT_OBJECT_0 = 0
_EVENT_MODIFY_STATE = 0x0002


class LauncherState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    """Escrito por uma parada ordenada: nenhum launcher, e nada de um deixado no ar."""


class ClusterArrival(StrEnum):
    """Como o cluster chegou ao launcher (§7.3)."""

    STARTED = "started"
    """Um launcher o iniciou: um launcher o para."""
    ADOPTED = "adopted"
    """Outro o iniciou -- `embedded start`, `run --`: fica no ar."""
    EXTERNAL = "external"
    """A fonte `external`: o launcher não toca em banco nenhum (§9)."""


@dataclass(frozen=True)
class InstanceRecord:
    """O que o `launcher.json` diz."""

    state: LauncherState
    pid: int | None = None
    url: str | None = None
    children: Mapping[str, int] = field(default_factory=dict)
    cluster: ClusterArrival | None = None
    postmaster_pid: int | None = None
    """O servidor que o launcher iniciou ou adotou, para que um launcher que
    herda um cluster herde esse, e não um servidor que alguém iniciou à mão
    depois (§7.3)."""
    last_ready_port: int | None = None
    """A porta do último início que chegou a `ready` (§5.4)."""

    def to_json(self) -> dict[str, Any]:
        return {
            "format": STATE_FORMAT,
            "state": self.state.value,
            "pid": self.pid,
            "url": self.url,
            "children": dict(self.children),
            "cluster": self.cluster.value if self.cluster else None,
            "postmaster_pid": self.postmaster_pid,
            "last_ready_port": self.last_ready_port,
        }

    @classmethod
    def from_json(cls, raw: object) -> InstanceRecord:
        """O registro, ou `ValueError` para tudo o que esta versão não escreveu."""
        if not isinstance(raw, dict) or raw.get("format") != STATE_FORMAT:
            raise ValueError("not a launcher state of this format")
        children = raw.get("children") or {}
        if not isinstance(children, dict):
            raise ValueError("children is not an object")
        cluster = raw.get("cluster")
        return cls(
            state=LauncherState(raw["state"]),
            pid=_optional_int(raw.get("pid")),
            url=str(raw["url"]) if raw.get("url") is not None else None,
            children={str(name): int(pid) for name, pid in children.items()},
            cluster=ClusterArrival(cluster) if cluster is not None else None,
            postmaster_pid=_optional_int(raw.get("postmaster_pid")),
            last_ready_port=_optional_int(raw.get("last_ready_port")),
        )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{value!r} is not an integer")
    return value


class StateFile:
    """`launcher.json`, lido com tolerância e escrito num único rename."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> InstanceRecord | None:
        """O registro, ou `None` quando não há nenhum que esta versão saiba ler.

        Um arquivo que não pode ser interpretado é tratado como ausente: o pior
        que isso custa é uma porta nova, e um cluster deixado no ar em vez de
        parado.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            logger.warning("%s could not be read (%s); ignored", self._path, error)
            return None
        try:
            return InstanceRecord.from_json(raw)
        except (KeyError, TypeError, ValueError) as error:
            logger.warning(
                "%s is not a state this version wrote (%s)", self._path, error
            )
            return None

    def write(self, record: InstanceRecord) -> bool:
        """Substitui o arquivo por `record`, para que nenhum leitor veja meio registro.

        Um estado que não pode ser escrito vai para o log e é, fora isso,
        ignorado: o launcher funciona sem ele, e o próximo início escolhe uma
        porta nova.
        """
        temporary = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(record.to_json(), indent=2) + "\n", encoding="utf-8"
            )
            deadline = time.monotonic() + REPLACE_SECONDS
            while True:
                try:
                    os.replace(temporary, self._path)
                    return True
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        except OSError as error:
            logger.warning("%s could not be written (%s)", self._path, error)
            temporary.unlink(missing_ok=True)
            return False


class InstanceLock:
    """`launcher.lock`, travado com `msvcrt.locking`, como o lock do adaptador.

    Detido pela vida do launcher, nunca liberado enquanto ele roda. O Windows o
    libera quando o processo termina, morto ou não.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def acquire(
        self, attempts: int = LOCK_ATTEMPTS, pause: float = LOCK_PAUSE_SECONDS
    ) -> bool:
        """Toma o lock, ou informa que outro processo o detém."""
        import msvcrt

        if self._descriptor is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        for attempt in range(attempts):
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                if attempt + 1 < attempts:
                    time.sleep(pause)
                continue
            self._descriptor = descriptor
            return True
        os.close(descriptor)
        return False

    def release(self) -> None:
        import msvcrt

        if self._descriptor is None:
            return
        try:
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            msvcrt.locking(self._descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self._descriptor)
            self._descriptor = None

    def held_elsewhere(self) -> bool:
        """Se um launcher detém o lock agora; toma-o por um instante."""
        if self._descriptor is not None:
            return False
        if self.acquire(attempts=1):
            self.release()
            return False
        return True


@dataclass(frozen=True)
class Instance:
    """As três coisas de uma raiz de dados, pelos seus caminhos e nome."""

    data_directory: Path

    @property
    def lock(self) -> Path:
        return self.data_directory / LOCK_NAME

    @property
    def state(self) -> Path:
        return self.data_directory / STATE_NAME

    @property
    def stop_event_name(self) -> str:
        """Por sessão (`Local\\`) e por raiz de dados: o `stop` de um checkout
        não para o app instalado."""
        key = os.path.normcase(str(self.data_directory)).encode("utf-8")
        return EVENT_PREFIX + hashlib.sha256(key).hexdigest()[:16]


class StopRequests:
    """Todo modo de pedir uma parada, contado onde o supervisor pode consultá-los.

    `ask()` é um Ctrl+C, o fim do stdin ou um `stop`; o segundo durante uma
    parada pula os prazos (§7.2). `end_session()` é a janela de §7.5: encurta os
    prazos e nunca os pula, porque o que os prazos protegem -- o job voltando
    para a fila -- é a razão de parar em ordem num logoff, para começo de
    conversa.

    Aqui, antes de qualquer `Settings`: o signal handler que o alimenta é
    instalado antes mesmo de o lock ser tentado. Um lock reentrante, porque esse
    handler roda na thread principal entre duas instruções quaisquer --
    inclusive as de um `count()` que já detém o lock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._count = 0
        self._session_ending = False
        self._sources: list[Callable[[], bool]] = []

    def ask(self) -> None:
        with self._lock:
            self._count += 1

    def end_session(self) -> None:
        with self._lock:
            self._session_ending = True

    def poll(self, source: Callable[[], bool]) -> None:
        """Acrescenta uma fonte que diz, quando perguntada, se chegou um pedido."""
        self._sources.append(source)

    def count(self) -> int:
        for source in self._sources:
            if source():
                self.ask()
        with self._lock:
            return self._count

    def session_ending(self) -> bool:
        with self._lock:
            return self._session_ending


class StopEvent:
    """O evento nomeado que o launcher espera, e que o `stop` sinaliza.

    Auto-reset: cada sinal é um pedido. O segundo pedido durante uma parada é a
    ordem que pula os prazos (§7.2).
    """

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    @classmethod
    def create(cls, name: str) -> StopEvent:
        kernel32 = _kernel32()
        handle = kernel32.CreateEventW(None, False, False, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), f"Could not create the event {name}")
        # Um sinal deixado de antes -- um `stop` que chegou enquanto o launcher
        # anterior saía -- não é endereçado a este.
        kernel32.ResetEvent(handle)
        return cls(handle)

    def is_set(self) -> bool:
        """Se chegou um pedido desde a última olhada; consome-o."""
        if self._handle is None:
            return False
        return bool(_kernel32().WaitForSingleObject(self._handle, 0) == _WAIT_OBJECT_0)

    def close(self) -> None:
        if self._handle is not None:
            _kernel32().CloseHandle(self._handle)
            self._handle = None

    @staticmethod
    def signal(name: str) -> bool:
        """Pede ao launcher de `name` que pare. `False` quando não há nenhum."""
        kernel32 = _kernel32()
        handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(handle))
        finally:
            kernel32.CloseHandle(handle)


def administrators_enabled() -> bool | None:
    """Se o grupo Administradores está em vigor no token deste processo.

    O grupo, e não `TokenElevation`, que o RFC-033 §8.3 mediu lendo `True` até no
    token que o `pg_ctl` restringe. `CheckTokenMembership` responde sim só para
    um grupo habilitado, não para um mantido apenas para negar. `None` quando o
    Windows não diz.
    """
    from ctypes import wintypes

    advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = _kernel32()
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.CheckTokenMembership.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
    ]
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(ADMINISTRATORS_SID, ctypes.byref(sid)):
        return None
    try:
        member = wintypes.BOOL()
        if not advapi32.CheckTokenMembership(None, sid, ctypes.byref(member)):
            return None
        return bool(member.value)
    finally:
        kernel32.LocalFree(sid)


def _kernel32() -> Any:
    """`kernel32` com as assinaturas que este módulo chama, definidas toda vez.

    Tipado como `Any` pela razão que o `windows_processes.py` dá: uma DLL do
    `ctypes` não tem assinatura estática, e os `argtypes` mantêm inteiros os
    handles de 64 bits.
    """
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return kernel32
