r"""A API como programa próprio, do jeito que o launcher a roda (RFC-036 §5.5).

    python -X utf8 -m app.presentation.api --port 51234 --supervised

O `Server` do uvicorn, com a aplicação já importada, em `127.0.0.1` e na porta
dada. O endereço não é argumento: a API serve só loopback, e a segunda guarda
do RFC-030 §6 parte disso.

Com `--supervised`, o fim do stdin é um Ctrl+C (§7.1): o uvicorn para de aceitar,
dá às requisições em andamento `GRACEFUL_SHUTDOWN_SECONDS` e encerra o lifespan.
O próprio handler dele recebe o sinal e, depois de encerrar, levanta o sinal de
novo (`capture_signals`), que chega aqui como o `KeyboardInterrupt` que o
`uvicorn.main.run` também engole.

Códigos de saída: 0 depois de uma parada; 3, o `STARTUP_FAILURE` do uvicorn,
quando a porta foi tomada antes de o uvicorn conseguir fazer o bind -- a deixa
para o launcher tentar outra (§5.4); 78 quando a configuração é inválida (§6.1).

O log é `api.log`, pela regra do RFC-034 §8.2. `python -m uvicorn
app.presentation.api:app` ainda funciona, escreve `uvicorn.log` e é o que rodar
para `--reload` (§9).

**Sem access log** (RFC-037 §5.3). O uvicorn escreve uma linha por requisição no
stdout, o launcher a passa para o seu stderr, e a janela escreve isso no seu log:
com a interface servida aqui, cada thumbnail, cada arquivo da interface e cada
`/health` do polling. A execução à mão acima o mantém, para quem está
desenvolvendo a API.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from app.infrastructure.process.supervision import SUPERVISED_FLAG, stop_on_stdin_eof
from app.presentation.api import app

LOOPBACK = "127.0.0.1"

GRACEFUL_SHUTDOWN_SECONDS = 5
"""O que as requisições em andamento recebem depois que uma parada é pedida (§7.2).

O launcher espera 15 s pela API inteira: isto, o lifespan e a margem.
"""


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{value} is not a TCP port")
    return port


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.presentation.api",
        description="Serve the SolidVision API on 127.0.0.1 (RFC-036 section 5.5).",
    )
    parser.add_argument("--port", type=_port, required=True, help="The TCP port.")
    parser.add_argument(
        SUPERVISED_FLAG,
        action="store_true",
        help="Stop as Ctrl+C would when stdin reaches its end.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_arg_parser().parse_args(argv)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=LOOPBACK,
            port=arguments.port,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
            access_log=False,
        )
    )
    try:
        if arguments.supervised:
            stop_on_stdin_eof()
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
