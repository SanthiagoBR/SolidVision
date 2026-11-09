r"""Um sinal ao postmaster, mandado como o `pg_ctl kill` manda (RFC-036 §7.5).

O PostgreSQL no Windows não tem os sinais do sistema: cada processo do
servidor escuta num named pipe próprio, `\\.\pipe\pgsignal_<pid>`, por um byte
-- o número do sinal -- e responde com o mesmo byte. O `pg_ctl kill` é um
`CallNamedPipe` nesse pipe (`src/port/kill.c`), e o `send()` também.

Para que serve: uma parada que não cria processo. Durante um logoff, um
processo que sobe morre na inicialização das DLLs (`0xC000026B`,
`STATUS_DLL_INIT_FAILED_LOGOFF`), e o `pg_ctl` é um deles -- medido em
2026-10-20, *"Neither a fast nor an immediate shutdown stopped the cluster"*,
sem nada no `pg_ctl.log`. O launcher, já rodando, ainda consegue escrever num
pipe, e um desligamento fast ou immediate usa só os processos do servidor que
já existem.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

FAST = 2
"""`SIGINT`, o desligamento fast do postmaster: o que `pg_ctl stop -m fast` manda."""

IMMEDIATE = 3
"""`SIGQUIT`, o desligamento immediate: a próxima subida se recupera pelo WAL."""

TIMEOUT_MS = 1000
"""Quanto o `CallNamedPipe` espera pelo pipe, como faz o `pg_ctl kill`."""


def send(pid: int, signal: int) -> bool:
    """Manda `signal` ao processo do servidor `pid`; se ele disse que recebeu."""
    if sys.platform != "win32":
        return False
    sent = ctypes.c_ubyte(signal)
    answer = ctypes.c_ubyte(0)
    read = wintypes.DWORD(0)
    if not _kernel32().CallNamedPipeW(
        rf"\\.\pipe\pgsignal_{pid}",
        ctypes.byref(sent),
        1,
        ctypes.byref(answer),
        1,
        ctypes.byref(read),
        TIMEOUT_MS,
    ):
        return False
    return read.value == 1 and answer.value == signal


def _kernel32() -> Any:
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CallNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    kernel32.CallNamedPipeW.restype = wintypes.BOOL
    return kernel32
