"""O que um programa supervisionado deve ao launcher, e como lhe pedem que pare.

Segue o RFC-036. Duas metades de um contrato, as duas do lado do filho:

* **Como ele diz que não consegue iniciar.** Uma linha no stderr e o código de
  saída 78 (`refuse_to_start`), onde antes eram a mesma linha e o código 1. A
  linha é para uma pessoa, e não mudou; o código é para o launcher, que não
  reinicia um programa que saiu com ele (§6.1).
* **Como ele fica sabendo que lhe pedem que pare.** O launcher fecha o stdin
  do programa, e `stop_on_stdin_eof()` transforma o fim do arquivo no Ctrl+C
  que o programa já trata (§7.1): o job runner devolve o job à fila, o uvicorn
  termina as requisições em andamento.

Só biblioteca padrão, e nada do projeto além das constantes. O singleton
`Settings` recusa por meio de `refuse_to_start`, então este módulo não pode
importar nem ele nem o logger, que o importa.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import signal
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, NoReturn

from app.infrastructure.config.constants import EXIT_CANNOT_START

SUPERVISED_FLAG = "--supervised"
"""O argumento que liga a vigia do stdin, em todo programa supervisionado.

Um argumento, e não uma variável de ambiente: instalado, o ambiente não é fonte
de nada (RFC-034 §5.2), e um argumento aparece na lista de processos para quem
for investigar (RFC-036 §7.1).
"""

POLL_SECONDS = 0.1
"""De quanto em quanto tempo se pergunta a um pipe do stdin se a outra ponta
ainda está aberta."""

_FILE_TYPE_PIPE = 0x0003


class CannotStart(SystemExit):
    """`SystemExit` com código 78, levando a linha que foi impressa.

    Não capturada, encerra o processo com 78 e nada mais, porque a linha já está
    no stderr. Capturada -- o launcher faz isso em volta do próprio import do
    `Settings` --, a linha continua à mão, para o evento `failed`.
    """

    def __init__(self, line: str) -> None:
        super().__init__(EXIT_CANNOT_START)
        self.line = line


def refuse_to_start(line: str) -> NoReturn:
    """Imprime `line` no stderr e sai com 78: reiniciar não vai resolver.

    Sem traceback (RFC-034 §5.5): a linha já nomeia o arquivo, a chave, o
    modelo, e o que fazer.
    """
    if sys.stderr is not None:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    raise CannotStart(line) from None


def stop_on_stdin_eof(stdin: int | None = None) -> threading.Thread:
    """Lê o stdin até o fim numa thread daemon, e então levanta SIGINT no processo.

    O que a thread principal recebe é um Ctrl+C, e este é o caminho mais rápido
    do piloto até um (RFC-036 §2.2): `signal.raise_signal` passa pelo handler
    em C, que também acorda um `time.sleep()` no meio -- medido em 0,000 s --,
    onde `_thread.interrupt_main()` espera o sleep terminar.

    **O pedido vem primeiro, e a thread não faz nada depois dele.** Cada passo
    que precisa do GIL é mais uma espera por uma thread principal que pode
    estar com ele: quando o vigia do piloto escrevia o próprio registro antes
    de pedir, a parada levou 8,48 s em vez de 0,289 s.

    **Um pipe nunca é lido enquanto não há nada nele: a cada `POLL_SECONDS`,
    pergunta-se a ele se a outra ponta ainda está aberta** (`PeekNamedPipe`,
    que não espera). Medido na implementação do RFC-036, os dois jeitos de
    lê-lo falharam no Windows:

    * Uma leitura síncrona deixada pendente no pipe faz o Windows segurar toda
      outra chamada naquele handle até ela terminar -- e carregar uma DLL cuja
      inicialização de runtime olha os handles padrão é uma chamada dessas.
      Com uma thread bloqueada em `os.read(0, 1)`, `import numpy` (e, portanto,
      `import torch`) travou enquanto o stdin ficou aberto: mais de 40 s na
      reprodução, para sempre sob o launcher, contra 0,27 s sem a thread.
    * Por `sys.stdin.buffer`, o lock do leitor fica tomado a leitura inteira, e
      um programa que termina por conta própria -- o uvicorn saindo com 3 por
      uma porta ocupada -- não consegue fechar o stdin no desligamento do
      interpretador: *"Fatal Python error: _enter_buffered_busy: could not
      acquire lock for <_io.BufferedReader name='<stdin>'>"*, e `0xC0000005` em
      vez de 3.

    A consulta custa a uma parada até `POLL_SECONDS` além dos 0,000 s do
    piloto. Um stdin que não é pipe -- `NUL`, um arquivo -- é lido até o fim, o
    que para esses não espera. E, uma vez que o programa está saindo, um pedido
    de parada perde o sentido: daí em diante o SIGINT é ignorado, para que um
    que chegue durante o desligamento não vire um traceback.

    O fim do arquivo chega de dois jeitos, e os dois são um pedido de parada: o
    launcher fecha o pipe, ou o launcher morre e o Windows fecha a ponta dele
    (§7.4). Um processo sem stdin nenhum não tem ninguém segurando-o, e para na
    hora.

    Só chamada com `--supervised`: um desenvolvedor rodando um programa à mão
    num terminal não quer que ele pare quando o stdin do terminal acabar.
    Chame-a dentro do `try` que trata o `KeyboardInterrupt`: um stdin que já
    está no fim levanta o sinal antes de esta função ter retornado.
    """
    descriptor = stdin if stdin is not None else _stdin_descriptor()
    atexit.register(_ignore_interrupts)
    thread = threading.Thread(
        target=_stop_at_end_of,
        args=(descriptor,),
        name="stop-on-stdin-eof",
        daemon=True,
    )
    thread.start()
    return thread


def _stdin_descriptor() -> int | None:
    if sys.stdin is None:
        return None
    try:
        return sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def _stop_at_end_of(descriptor: int | None) -> None:
    if descriptor is not None:
        try:
            handle = _pipe_handle(descriptor)
            if handle is None:
                while os.read(descriptor, 1):
                    pass
            else:
                _wait_for_the_other_end_to_close(handle, descriptor)
        except OSError:
            # Um handle que sumiu: a mesma notícia que o fim do arquivo,
            # ninguém segura a outra ponta.
            pass
    signal.raise_signal(signal.SIGINT)


def _pipe_handle(descriptor: int) -> int | None:
    """O handle do Windows por trás de `descriptor`, quando ele é um pipe."""
    if sys.platform != "win32":
        return None
    import msvcrt

    handle = msvcrt.get_osfhandle(descriptor)
    if _kernel32().GetFileType(handle) != _FILE_TYPE_PIPE:
        return None
    return handle


def _wait_for_the_other_end_to_close(handle: int, descriptor: int) -> None:
    """Retorna quando o pipe se rompe: quem escrevia nele o fechou, ou morreu.

    `PeekNamedPipe` falha então com `ERROR_BROKEN_PIPE`, e qualquer outra falha
    é tomada do mesmo jeito -- não sobrou ninguém a quem ouvir. Bytes no pipe,
    que o launcher nunca manda, são lidos sem esperar e descartados.
    """
    kernel32 = _kernel32()
    available = wintypes.DWORD()
    while kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
        if available.value:
            os.read(descriptor, available.value)
        else:
            time.sleep(POLL_SECONDS)


def _ignore_interrupts() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _kernel32() -> Any:
    """`kernel32` com as duas assinaturas usadas aqui; handles de 64 bits inteiros."""
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    kernel32.GetFileType.restype = wintypes.DWORD
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    return kernel32
