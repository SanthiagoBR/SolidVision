"""O nome do programa que este processo está rodando (RFC-034 §8.2).

Cada programa escreve o seu próprio arquivo de log, com o nome dele::

    python -m uvicorn app.presentation.api:app          uvicorn.log
    python -m app.infrastructure.workers.job_runner     job_runner.log
    python -m app.infrastructure.database.embedded ...  embedded.log
    python -m app.cli ...                               cli.log
    python -m pytest, or pytest                         pytest.log
    qualquer outra coisa (python -c, um REPL)           python.log

Derivado aqui e em nenhum outro lugar, para que nenhum ponto de entrada
precise declarar nada -- o que nenhum conseguiria fazer a tempo: o logger é
configurado pelo primeiro `get_logger()`, durante o import, antes de qualquer
`main()` rodar.

Pela mesma razão, o nome é lido de `sys.orig_argv`, a linha de comando como o
interpretador a recebeu. Enquanto os pacotes de um módulo `-m` ainda estão
sendo importados, `sys.argv[0]` é o literal `-m` e `__main__.__spec__` ainda
não foi definido.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import PureWindowsPath

FALLBACK = "python"

_OPTIONS_WITH_A_VALUE = "WX"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def program_name(argv: Sequence[str] | None = None) -> str:
    """O módulo passado a `-m`, ou o script, ou `python`."""
    arguments = list(sys.orig_argv if argv is None else argv)[1:]
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        if argument == "--check-hash-based-pycs":
            position += 2
            continue
        if argument.startswith("--"):
            position += 1
            continue
        if argument == "-":
            return FALLBACK
        if not argument.startswith("-"):
            return _clean(PureWindowsPath(argument).stem)
        # Um grupo de opções de uma letra. `-m` e `-c` encerram as opções do
        # próprio interpretador, e o que vier depois da letra é o valor delas.
        for index, letter in enumerate(argument[1:], start=1):
            if letter == "m":
                attached = argument[index + 1 :]
                if attached:
                    return _module(attached)
                following = arguments[position + 1 : position + 2]
                return _module(following[0]) if following else FALLBACK
            if letter == "c":
                return FALLBACK
            if letter in _OPTIONS_WITH_A_VALUE:
                if index == len(argument) - 1:
                    position += 1
                break
        position += 1
    return FALLBACK


def _module(name: str) -> str:
    parts = [part for part in name.split(".") if part and part != "__main__"]
    return _clean(parts[-1]) if parts else FALLBACK


def _clean(name: str) -> str:
    return _UNSAFE.sub("_", name) or FALLBACK
