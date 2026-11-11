"""O diretório do cluster do usuário: caminhos, DACL, senha, trava de transição.

RFC-033 §6, §7.1 e §7.2. O layout, sob `%LOCALAPPDATA%` por padrão (a
configuração `embedded_postgres_directory`)::

    postgres\\                   DACL explícita: o usuário e SYSTEM
        16\\
            data\\               PGDATA
            password            uma linha
            adapter.lock        trava de transição (§7.1)
            startup.log         `pg_ctl -l`, reescrito a cada subida (§8.1)
            pg_ctl.log          a saída do próprio `pg_ctl`, reescrita a cada chamada

A versão major está no caminho para que o `17\\` de um upgrade futuro caiba
ao lado do `16\\` sem renomear nada: o `pg_upgrade` precisa dos dois lados ao
mesmo tempo.
"""

from __future__ import annotations

import ctypes
import os
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .code_page import argv_safe
from .errors import EmbeddedPostgresError

SYSTEM_SID = "S-1-5-18"
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class ClusterFiles:
    """Todo caminho do cluster de uma versão major, e nada mais."""

    directory: Path

    @property
    def data(self) -> Path:
        return self.directory / "data"

    @property
    def data_initdb(self) -> Path:
        """Onde o `initdb` escreve; renomeado para `data` só se der certo (§7.1)."""
        return self.directory / "data.initdb"

    @property
    def password(self) -> Path:
        return self.directory / "password"

    @property
    def password_tmp(self) -> Path:
        return self.directory / "password.tmp"

    @property
    def lock(self) -> Path:
        return self.directory / "adapter.lock"

    @property
    def startup_log(self) -> Path:
        return self.directory / "startup.log"

    @property
    def pg_ctl_log(self) -> Path:
        return self.directory / "pg_ctl.log"

    @property
    def postmaster_pid(self) -> Path:
        return self.data / "postmaster.pid"

    @property
    def pg_version(self) -> Path:
        return self.data / "PG_VERSION"

    @property
    def pg_control(self) -> Path:
        return self.data / "global" / "pg_control"

    @property
    def postgresql_conf(self) -> Path:
        return self.data / "postgresql.conf"

    @property
    def server_log_directory(self) -> Path:
        """Onde o logging collector escreve: um arquivo por dia da semana (§8.1)."""
        return self.data / "log"

    @property
    def server_conf(self) -> Path:
        """`solidvision.conf`, incluído pelo `postgresql.conf` (§8.1)."""
        return self.data / "solidvision.conf"


class DirectoryAcl(ABC):
    """Porta para restringir um diretório ao usuário atual e ao SYSTEM."""

    @abstractmethod
    def restrict_to_current_user(self, directory: Path) -> None:
        """Troca a herança por controle total para o SID do usuário e o SYSTEM."""


class WindowsDirectoryAcl(DirectoryAcl):
    """A DACL pela API do Windows, com SIDs, não nomes.

    Num Windows em português o grupo `Everyone` se chama `Todos`: um nome
    traduzido não pode decidir uma permissão, então o usuário é nomeado pelo
    SID lido do token do processo (RFC-033 §6).
    """

    def restrict_to_current_user(self, directory: Path) -> None:
        entries = (f"A;OICI;FA;;;{current_user_sid()}", f"A;OICI;FA;;;{SYSTEM_SID}")
        try:
            set_protected_dacl(directory, entries)
        except OSError as error:
            raise EmbeddedPostgresError(
                f"Could not restrict {directory}: {error}"
            ) from error


SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1


def set_protected_dacl(path: Path, entries: Sequence[str]) -> None:
    """Troca a DACL de `path` por uma protegida, só com `entries`, em SDDL.

    Pelo `SetNamedSecurityInfoW`, e não pelo `icacls /inheritance:r`: no
    build 26100 do Windows, o `icacls` não tira as entradas herdadas, só as
    torna explícitas, e os Administradores continuavam com controle total
    (RFC-050 §16). Aqui a DACL é substituída inteira, sem herança, e o
    Windows recalcula pelas entradas herdáveis tudo o que está abaixo.
    Levanta `OSError` com o erro do Windows.
    """
    from ctypes import wintypes

    advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    sddl = "D:P" + "".join(f"({entry})" for entry in entries)
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        error = advapi32.SetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel32.LocalFree(descriptor)


def current_user_sid() -> str:
    """O SID em string do usuário com que este processo roda, lido do token."""
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    token_query = 0x0008
    token_user = 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise EmbeddedPostgresError(
            "Could not open the process token "
            f"(Windows error {ctypes.get_last_error()})."
        )
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, token_user, buffer, size, ctypes.byref(size)
        ):
            raise EmbeddedPostgresError(
                "Could not read the token user "
                f"(Windows error {ctypes.get_last_error()})."
            )
        # TOKEN_USER começa com SID_AND_ATTRIBUTES, que começa com o PSID.
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        string_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(string_sid)):
            raise EmbeddedPostgresError(
                "Could not format the user SID "
                f"(Windows error {ctypes.get_last_error()})."
            )
        try:
            return str(string_sid.value)
        finally:
            kernel32.LocalFree(string_sid)
    finally:
        kernel32.CloseHandle(token)


class ClusterHome:
    """O diretório por usuário sob o qual mora todo cluster de toda versão major."""

    LOCK_POLL_SECONDS = 0.1

    def __init__(self, root: Path, *, acl: DirectoryAcl | None = None) -> None:
        if not root.is_absolute():
            raise EmbeddedPostgresError(
                f"The cluster directory must be absolute, not {root}."
            )
        self._root = root
        self._acl = acl if acl is not None else WindowsDirectoryAcl()

    @property
    def root(self) -> Path:
        return self._root

    def files(self, major: int) -> ClusterFiles:
        return ClusterFiles(self._root / str(major))

    def ensure_directory(self, files: ClusterFiles) -> None:
        """Cria `postgres\\` e o diretório da versão, onde mora a trava."""
        files.directory.mkdir(parents=True, exist_ok=True)

    def for_commands(self, files: ClusterFiles) -> ClusterFiles:
        """Os mesmos arquivos, por um caminho que dá para passar a `initdb` e `pg_ctl`.

        §10: sob um diretório de usuário fora da code page ANSI, a forma curta
        8.3 do diretório da versão. Tudo abaixo dele tem um nome ASCII
        escolhido pelo próprio adaptador. Um diretório que ainda não existe não
        tem forma curta, e também nada para rodar nele.
        """
        if not files.directory.exists():
            return files
        return ClusterFiles(
            argv_safe(files.directory, setting="EMBEDDED_POSTGRES_DIRECTORY")
        )

    def secure(self) -> None:
        """Aplica a DACL explícita a `postgres\\` (§6).

        Chamado no caminho da primeira execução, antes de a senha ser escrita,
        em vez de só no `mkdir` que cria o diretório: senão, um processo morto
        entre os dois deixaria o diretório com a DACL herdada para sempre.
        Trocar a DACL é idempotente, e isto roda uma vez por cluster, ao lado
        de um `initdb` que leva dezenas de segundos.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        self._acl.restrict_to_current_user(self._root)

    # -- a senha (§7.2) ----------------------------------------------------

    def read_password(self, files: ClusterFiles) -> str | None:
        try:
            password = files.password.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        return password or None

    def write_new_password(self, files: ClusterFiles) -> str:
        """Gera a senha do cluster e a escreve antes de o `initdb` rodar.

        O `token_urlsafe` importa pelo alfabeto (`A-Z a-z 0-9 _ -`): nada nele
        precisa de escape nem de aspas, numa URL ou numa string de conexão da
        libpq, onde quer que a senha seja posta. Escrita com um nome temporário
        e renomeada, para que um leitor nunca veja meia senha.
        """
        password = secrets.token_urlsafe(24)
        files.password_tmp.write_text(password + "\n", encoding="ascii")
        os.replace(files.password_tmp, files.password)
        return password

    def discard_password(self, files: ClusterFiles) -> None:
        """Uma senha sem um `data` não é metade de um par (§7.1)."""
        files.password.unlink(missing_ok=True)
        files.password_tmp.unlink(missing_ok=True)

    # -- a trava de transição (§7.1) ---------------------------------------

    @contextmanager
    def transition_lock(self, files: ClusterFiles, timeout: float) -> Iterator[None]:
        """Segura `adapter.lock` com exclusividade por uma criação, subida ou parada.

        Nunca fica presa enquanto o cluster vive: quem chega em segundo espera
        e depois adota. O `msvcrt.locking` é liberado pelo sistema operacional
        quando o processo morre, então um processo morto no meio de um `initdb`
        não deixa o diretório travado.
        """
        import msvcrt

        self.ensure_directory(files)
        descriptor = os.open(files.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise EmbeddedPostgresError(
                            f"Another process has held {files.lock} for more than "
                            f"{timeout:.0f} s; it may be creating or recovering "
                            "the cluster."
                        ) from None
                    time.sleep(self.LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)
