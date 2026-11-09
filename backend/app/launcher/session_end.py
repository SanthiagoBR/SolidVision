"""O fim da sessão do Windows: uma janela oculta, e a parada nela (RFC-036 §7.5).

No logoff, o Windows encerra o que tiver sobrado. Um job runner encerrado no meio
de um job o deixa para o reaper, com uma tentativa a menos (§6.3): quem sai da
sessão toda noite com uma indexação rodando veria o job falhar na terceira noite.

Os eventos de controle do console não ajudam. Segundo a documentação de
`SetConsoleCtrlHandler`, um processo de console que carrega `user32.dll` ou
`gdi32.dll` -- o Python com `ctypes` carrega, e o `torch` também -- não recebe
nem `CTRL_LOGOFF_EVENT` nem `CTRL_SHUTDOWN_EVENT`: é tratado como aplicação com
janela. O que está documentado para essas é uma janela. Uma de nível superior,
nunca mostrada, e não uma janela só de mensagens, que segundo a mesma
documentação não recebe mensagens de broadcast.

    mensagem                 o que esta janela faz
    WM_QUERYENDSESSION       registra o motivo (`ShutdownBlockReasonCreate`)
                             e responde sim: o launcher nunca veta o fim de
                             uma sessão
    WM_ENDSESSION, ending    pede a parada de §7.2, com os prazos mais curtos,
                             e a espera nesta thread -- o processo pode ser
                             encerrado assim que isto retornar -- e depois
                             retira o motivo
    WM_ENDSESSION, not       retira o motivo: alguém cancelou

**A janela não basta: o launcher também precisa vir primeiro.** Dois logoffs
reais em 19/10/2026 discordaram. No primeiro, os filhos estavam vivos quando a
parada começou, e pararam em 0,3 s; no segundo, os filhos e o postmaster já
estavam mortos -- dentro de um tick do supervisor -- e o job foi para o reaper
com uma tentativa gasta. O Windows encerra os processos de uma sessão do nível de
shutdown mais alto para baixo, e todo processo começa no mesmo, 0x280, onde a
ordem não é definida. `end_before_the_children()` põe o launcher acima dele, para
que o seu `WM_ENDSESSION` venha, e seja esperado, enquanto os outros ainda vivem:
assim foi nos seis logoffs que se seguiram (§16, à mão). Se algum dia não for, o
custo é o do primeiro parágrafo, e o reaper da próxima sessão recupera o job.

**E a parada que ela roda não cria processo nenhum**, porque nenhum inicia durante
um logoff (`0xC000026B`): o job runner adia uma interrupção que cai numa chamada
ao banco (`app.infrastructure.process.interrupts`), e o cluster é parado pelo
seu pipe de sinal (§17.1).
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

logger = logging.getLogger("app.app.launcher.session_end")

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016

REASON = "SolidVision está devolvendo a indexação à fila"
"""O que a tela de desligamento mostra enquanto a parada roda (§7.5)."""

READY_SECONDS = 10.0

DEFAULT_SHUTDOWN_LEVEL = 0x280
"""Onde todo processo começa, segundo `SetProcessShutdownParameters`: os
filhos, o postmaster e o que mais a sessão rodar."""

SHUTDOWN_LEVEL = 0x300
"""Onde o launcher pede para ser encerrado: o início da primeira faixa das
aplicações (0x300-0x3FF), acima do padrão."""

LRESULT = wintypes.LPARAM
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _WindowClass(ctypes.Structure):
    """`WNDCLASSW`, como o `winuser.h` a declara."""

    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class SessionEndWindow:
    """A janela oculta, numa thread só dela que bombeia as suas mensagens.

    `on_session_end` é chamado nessa thread quando a sessão está de fato
    terminando, e deve retornar quando a parada tiver terminado ou desistido: o
    Windows pode encerrar o processo no momento em que o procedimento da janela
    retornar.
    """

    def __init__(self, on_session_end: Callable[[], None]) -> None:
        self._on_session_end = on_session_end
        # Uma classe por janela, para que uma segunda janela no mesmo processo
        # -- os testes criam várias -- nunca rode o procedimento da primeira.
        self._class_name = f"SolidVisionLauncher-{os.getpid()}-{id(self):x}"
        self._procedure = WNDPROC(self._handle)
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: str | None = None

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    def start(self) -> bool:
        """Cria a janela e inicia o seu loop; `False` se o Windows recusou."""
        self._thread = threading.Thread(
            target=self._run, name="session-end-window", daemon=True
        )
        self._thread.start()
        self._ready.wait(READY_SECONDS)
        if self._hwnd is None:
            logger.warning(
                "The window that receives the end of the session could not be "
                "created (%s); a logoff ends the children without the ordered "
                "stop",
                self._failure or "no answer",
            )
            return False
        return True

    def close(self) -> None:
        """Encerra o loop e destrói a janela."""
        if self._hwnd is not None:
            _user32().PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(READY_SECONDS)

    def _run(self) -> None:
        user32 = _user32()
        instance = _kernel32().GetModuleHandleW(None)
        window_class = _WindowClass()
        window_class.lpfnWndProc = self._procedure
        window_class.hInstance = instance
        window_class.lpszClassName = self._class_name
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            self._failure = f"RegisterClassW, Windows error {ctypes.get_last_error()}"
            self._ready.set()
            return
        try:
            hwnd = user32.CreateWindowExW(
                0,
                self._class_name,
                "SolidVision launcher",
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                self._failure = (
                    f"CreateWindowExW, Windows error {ctypes.get_last_error()}"
                )
                self._ready.set()
                return
            self._hwnd = hwnd
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            user32.UnregisterClassW(self._class_name, instance)

    def _handle(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        user32 = _user32()
        if message == WM_QUERYENDSESSION:
            # O momento em que o Windows começou por este processo: ao lado das
            # mortes dos filhos no log, diz quem foi primeiro.
            logger.info("Windows asked whether the session may end")
            user32.ShutdownBlockReasonCreate(hwnd, REASON)
            return 1
        if message == WM_ENDSESSION:
            try:
                if wparam:
                    logger.warning("The Windows session is ending; stopping in order")
                    self._on_session_end()
            except Exception:
                logger.exception("The stop at the end of the session failed")
            finally:
                user32.ShutdownBlockReasonDestroy(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))


def end_before_the_children() -> bool:
    """Pede ao Windows que encerre este processo antes do resto da sessão.

    `SetProcessShutdownParameters`: o sistema encerra os processos do nível mais
    alto para baixo. Não é herdado -- um filho criado depois fica no padrão
    (testado) -- então os filhos e o postmaster ficam abaixo do launcher.
    `False`, com uma linha no log, se o Windows recusou.
    """
    kernel32 = _kernel32()
    if not kernel32.SetProcessShutdownParameters(SHUTDOWN_LEVEL, 0):
        logger.warning(
            "The launcher could not ask to be ended before its children "
            "(Windows error %s); at a logoff, they may be ended first",
            ctypes.get_last_error(),
        )
        return False
    return True


def shutdown_level() -> int:
    """O nível de shutdown deste processo, como o Windows o tem."""
    level = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not _kernel32().GetProcessShutdownParameters(
        ctypes.byref(level), ctypes.byref(flags)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return level.value


def _user32() -> Any:
    """`user32` com as assinaturas que o módulo chama; handles de 64 bits inteiros."""
    user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WindowClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = LRESULT
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
    return user32


def _kernel32() -> Any:
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.SetProcessShutdownParameters.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.SetProcessShutdownParameters.restype = wintypes.BOOL
    kernel32.GetProcessShutdownParameters.argtypes = [
        wintypes.LPDWORD,
        wintypes.LPDWORD,
    ]
    kernel32.GetProcessShutdownParameters.restype = wintypes.BOOL
    return kernel32
