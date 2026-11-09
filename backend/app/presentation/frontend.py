"""A interface, servida pela API na sua própria origem (RFC-037 §5.1).

    GET /assets/<file>   os arquivos do build, com o hash no nome:
                         `StaticFiles`, com o seu `ETag`
    GET /, GET /<screen> `index.html`, com `Cache-Control: no-cache`

Mesma origem, então sem CORS e sem preflight: a janela carrega a URL do
`ready`, e a página fala com a origem de onde veio.

**O fallback fica no tratamento do 404, e não numa rota** (§2.2). Um
`StaticFiles` montado em `/`, ou uma rota pega-tudo, responderia a todo caminho
desconhecido, e o Starlette responde com 405 a um caminho que conhece chamado
com um método que não conhece: `POST /api/v1/anything` passaria de 404 a 405, e
o reveal desligado -- que responde 404 para parecer um caminho que não existe
(RFC-030 §6) -- seria o único `POST` a um caminho "desconhecido" ainda dizendo
404, apontando para a rota que esconde. Aqui o roteamento da API não muda: um
caminho desconhecido continua sendo 404, e só então, para um `GET` ou um `HEAD`
fora de `/api/` e de `/assets/`, o 404 vira `index.html`. O próprio `/` não é
rota, e vai pelo mesmo caminho.

Um `/assets/<file>` ausente continua 404. Um `index.html` de um build antigo,
apontando para arquivos de outro, falha de forma visível, em vez de receber
HTML no lugar de JavaScript.

Sem um `index.html` no diretório, nada é montado, e uma linha diz onde a
interface foi procurada: `npm run dev` a serve pelo Vite, e a suíte não precisa
de nada disso.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

INDEX = "index.html"
ASSETS = "assets"
"""Onde o Vite põe os arquivos com hash no nome (`build.assetsDir`)."""

NOT_THE_INTERFACE = ("/api", f"/{ASSETS}")
"""Prefixos cujo 404 continua sendo o 404 da API."""

INTERFACE_METHODS = frozenset({"GET", "HEAD"})


def is_interface_path(path: str) -> bool:
    """Um caminho que uma tela da interface pode ter: qualquer um fora dos dois."""
    return not any(
        path == prefix or path.startswith(prefix + "/") for prefix in NOT_THE_INTERFACE
    )


def serve_frontend(app: FastAPI, directory: Path) -> bool:
    """Serve por `app` o build que está em `directory`, quando houver um.

    `True` quando é servido. O mount e o handler são acrescentados ao próprio
    `app`, então um teste entrega a esta função um app e um diretório só seus.
    """
    index = directory / INDEX
    if not index.is_file():
        logger.info("No interface in %s; the API serves only the API", directory)
        return False

    # Um build sempre tem o diretório. Sem ele, nada é montado, e os arquivos
    # que a página cita são 404 da API -- um `StaticFiles` sobre um diretório
    # que não existe responderia a cada um com um 500.
    assets = directory / ASSETS
    if assets.is_dir():
        app.mount(f"/{ASSETS}", StaticFiles(directory=assets), name=ASSETS)

    async def not_found(request: Request, exc: Exception) -> Response:
        if request.method in INTERFACE_METHODS and is_interface_path(request.url.path):
            # Lido a cada requisição: um novo build é servido sem reiniciar.
            return FileResponse(
                index, media_type="text/html", headers={"Cache-Control": "no-cache"}
            )
        # O Starlette só entrega a um handler de status as suas `HTTPException`.
        assert isinstance(exc, HTTPException)
        return await http_exception_handler(request, exc)

    app.add_exception_handler(404, not_found)
    logger.info("Serving the interface from %s", directory)
    return True
