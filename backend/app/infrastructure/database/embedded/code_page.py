"""Caminhos como um programa do PostgreSQL consegue recebê-los (RFC-033 §10).

`initdb`, `pg_ctl` e `postgres` são programas em C com um `main` estreito: no
Windows, recebem os argumentos na code page ANSI. Um caminho com um caractere
fora dela não chega. Medido numa máquina com code page 1252 e um diretório
chamado `Łukasz 東京`: o `initdb` recebeu `Lukasz ??` -- o `Ł` trocado pelo
`L` de best-fit, que nomeia *outro* diretório, e os ideogramas por `?` -- e
falhou procurando o arquivo de senha lá. Com o próprio runtime sob um
diretório assim, o `initdb` não achou nem o próprio executável.

`João Silva` não é esse problema: todos os caracteres dele estão na code page
1252, e funcionou sem mudança.

Então o segundo ramo que o RFC-033 escreveu antes de medir é o que vale. Um
caminho que não cabe é trocado pela sua forma curta 8.3, que é ASCII e nomeia
o mesmo diretório; o `initdb` rodou por uma na mesma medição. Um volume que
não gera nomes curtos não deixa nada para entregar, e o adaptador recusa,
nomeando a configuração que move o diretório para outro lugar.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

from .errors import EmbeddedPostgresError

_BUFFER_LENGTH = 32768


def fits_ansi_code_page(text: str) -> bool:
    """Se todo caractere sobrevive à passagem pela code page ANSI.

    O `mbcs` em modo estrito recusa as substituições de best-fit, então `Ł`
    numa máquina 1252 é um "não", e não um `L` silencioso.
    """
    if sys.platform != "win32":
        return True
    try:
        text.encode("mbcs")
    except UnicodeEncodeError:
        return False
    return True


def short_path(path: Path) -> str:
    """`GetShortPathNameW`: a forma 8.3, ou `""` quando o Windows não dá uma.

    O caminho precisa existir. Num volume sem nomes curtos a chamada dá certo
    e responde com o nome longo sem mudança, o que quem chama distingue
    testando a resposta, não o código de retorno.
    """
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetShortPathNameW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    buffer = ctypes.create_unicode_buffer(_BUFFER_LENGTH)
    if not kernel32.GetShortPathNameW(str(path), buffer, _BUFFER_LENGTH):
        return ""
    return buffer.value


def argv_safe(path: Path, *, setting: str) -> Path:
    """`path`, escrito de modo a chegar intacto a um programa de `main` estreito.

    Sem mudança quando já cabe na code page; a forma curta quando não cabe; e
    uma recusa que nomeia `setting` quando não há forma curta.
    """
    if fits_ansi_code_page(str(path)):
        return path
    short = short_path(path)
    if short and fits_ansi_code_page(short):
        return Path(short)
    code_page = ctypes.WinDLL("kernel32").GetACP()
    raise EmbeddedPostgresError(
        f"{path} has characters outside this Windows' ANSI code page "
        f"({code_page}). PostgreSQL's programs cannot be given such a path, and "
        "this volume has no 8.3 short name to give them instead. Point "
        f"{setting} at a directory whose path fits the code page."
    )
