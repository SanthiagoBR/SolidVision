"""A tabela de processos, pelo `kernel32` (RFC-033 §8.4 e §9.3).

Quatro perguntas, cada uma com uma ou duas chamadas ao `kernel32` via
`ctypes`, no estilo do adaptador de volumes e sem dependência nova: quais
processos existem e quem são seus pais (`CreateToolhelp32Snapshot`), onde
está o executável de um processo (`QueryFullProcessImageNameW`), se um PID
ainda está vivo, e como encerrar um (`TerminateProcess`).

Listar processos pelo PowerShell, como faz o script do RFC-033a, custa
segundos por chamada -- mais do que uma subida a quente inteira deveria
(§2.1).
"""

from __future__ import annotations

import ctypes
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import EmbeddedPostgresError

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_TERMINATE_WAIT_MS = 5000
_ERROR_ACCESS_DENIED = 5
_MAX_PATH = 32768


@dataclass(frozen=True)
class ProcessInfo:
    """Uma linha de um snapshot de processos."""

    pid: int
    parent_pid: int
    name: str


class ProcessTable(ABC):
    """Porta sobre a tabela de processos do sistema operacional.

    O adaptador decide quem adotar e quem encerrar a partir destas respostas,
    então os testes unitários da máquina de estados (§12) põem um dublê no
    lugar da tabela real.
    """

    @abstractmethod
    def snapshot(self) -> list[ProcessInfo]:
        """Todo processo, com o PID do pai, neste instante."""

    @abstractmethod
    def executable(self, pid: int) -> Path | None:
        """O caminho completo da imagem de um processo, ou `None` se ilegível."""

    @abstractmethod
    def is_alive(self, pid: int) -> bool:
        """Se um processo com este PID está rodando agora."""

    @abstractmethod
    def terminate(self, pid: int) -> bool:
        """Encerra um processo e espera que ele suma.

        `False` quando ele já tinha sumido ou recusou.
        """


class WindowsProcesses(ProcessTable):
    """`ProcessTable` sobre o `kernel32`. Recusa ser construída fora do Windows."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise EmbeddedPostgresError(
                f"WindowsProcesses requires Windows; this is {sys.platform!r}."
            )

    @staticmethod
    def _kernel32() -> Any:
        """O `kernel32` com as assinaturas que este módulo chama, definidas toda vez.

        Tipado `Any` pelo motivo que o adaptador de volumes dá: uma DLL do
        `ctypes` não tem assinatura estática. São o `argtypes` e o `restype`
        que impedem que handles de 64 bits sejam truncados para um `int` de C.
        """
        from ctypes import wintypes

        kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        return kernel32

    def snapshot(self) -> list[ProcessInfo]:
        kernel32 = self._kernel32()
        handle = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise EmbeddedPostgresError(
                "Could not snapshot the process table "
                f"(Windows error {ctypes.get_last_error()})."
            )
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
            found: list[ProcessInfo] = []
            more = kernel32.Process32FirstW(handle, ctypes.byref(entry))
            while more:
                found.append(
                    ProcessInfo(
                        pid=int(entry.th32ProcessID),
                        parent_pid=int(entry.th32ParentProcessID),
                        name=str(entry.szExeFile),
                    )
                )
                more = kernel32.Process32NextW(handle, ctypes.byref(entry))
            return found
        finally:
            kernel32.CloseHandle(handle)

    def executable(self, pid: int) -> Path | None:
        from ctypes import wintypes

        kernel32 = self._kernel32()
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(_MAX_PATH)
            size = wintypes.DWORD(_MAX_PATH)
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return None
            return Path(buffer.value)
        finally:
            kernel32.CloseHandle(handle)

    def is_alive(self, pid: int) -> bool:
        """Vivo quer dizer "não saiu", perguntado ao próprio objeto do processo.

        `WaitForSingleObject` em vez de `GetExitCodeProcess`, que não distingue
        um processo rodando de um que saiu com o código 259. Um processo que o
        Windows não abre para este usuário está vivo: recusar acesso é algo que
        só um processo existente faz.
        """
        if pid <= 0:
            return False
        kernel32 = self._kernel32()
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
        )
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            return bool(kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT)
        finally:
            kernel32.CloseHandle(handle)

    def terminate(self, pid: int) -> bool:
        """Encerra um processo e espera até o Windows informar que ele sumiu.

        O `TerminateProcess` só pede: ele retorna com o objeto do processo
        ainda não sinalizado, então um `is_alive` logo depois ainda diz sim (49
        de 50 tentativas). Quem reinicia o cluster na hora adotaria então um
        postmaster que está morrendo.
        """
        kernel32 = self._kernel32()
        handle = kernel32.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            if not kernel32.TerminateProcess(handle, 1):
                return False
            kernel32.WaitForSingleObject(handle, _TERMINATE_WAIT_MS)
            return True
        finally:
            kernel32.CloseHandle(handle)


class _ProcessEntry32W(ctypes.Structure):
    """`PROCESSENTRY32W`, como o `tlhelp32.h` a declara."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]
