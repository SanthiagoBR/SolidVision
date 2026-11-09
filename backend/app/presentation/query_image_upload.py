"""A imagem de `POST /images/search/by-image`, do corpo até um arquivo (RFC-038 §4.2).

    verificação                                 recusa
    `Content-Type` é image/jpeg ou image/png    415
    `Content-Length`, se enviado, é <= 50 MB    413, antes de ler
    o corpo, contado ao chegar, é <= 50 MB      413, cortado no teto
    os primeiros bytes são do tipo declarado    415
    o Pillow o decodifica, sob o seu limite     400 (UnreadableQueryImageError)
    de pixels

**O corpo são os bytes**, não `multipart/form-data`: não há outro campo a
enviar, e o `python-multipart`, de que o FastAPI precisa para formulários, não
está instalado. Exigir o tipo também fecha, para esta rota, o formulário do
RFC-037 §2.3: um `<form>` só envia urlencoded, multipart ou texto puro, e um
`fetch` de outra origem com `image/jpeg` dispara um preflight que esta API não
responde.

**Duas dependências, e a divisão é a do event loop.** O corpo é lido numa
`async`, para a memória, porque ler uma requisição é I/O que o loop faz bem;
gravá-lo em disco ali prenderia todas as outras requisições -- `/health`
inclusive -- pelo tempo da escrita. O arquivo é gravado, decodificado e
apagado num gerador comum, que o FastAPI roda no seu threadpool, e cujo
`finally` roda de qualquer jeito que a requisição termine: sucesso, recusa ou
erro.

**Nenhum caminho entra** (RFC-030 §5.1): o corpo são bytes, e o arquivo
temporário é do servidor, com nome aleatório, em `cache\\query-images` sob a
raiz de dados. A pasta é esvaziada quando a API inicia, pelo que um processo
que morreu tenha deixado para trás.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status

from app.infrastructure.config.layout import layout
from app.infrastructure.filesystem.query_image import inspect_query_image
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

MAX_QUERY_IMAGE_BYTES = 50 * 1024 * 1024
"""50 MB. Uma escolha: um JPEG de drone de 20 MP tem de 8 a 20 MB, uma captura
de tela 4K em PNG, uns 20. Uma constante, e o 413 a informa."""

SIGNATURES = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
}
"""Os media types aceitos, e os bytes com que cada arquivo começa."""

EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png"}

REQUEST_BODY = {
    "required": True,
    "description": (
        "The picture's bytes, as the body. Up to 50 MB; the type must match "
        "the file."
    ),
    "content": {
        media_type: {"schema": {"type": "string", "format": "binary"}}
        for media_type in SIGNATURES
    },
}
"""O `requestBody` da rota no documento OpenAPI, para clientes tipados.

A rota lê o corpo ela mesma, então o FastAPI não publicaria nenhum."""


@dataclass(frozen=True)
class QueryImageBody:
    """Os bytes da requisição, e o tipo que ela declarou."""

    media_type: str
    data: bytes


def media_type_of(content_type: str | None) -> str | None:
    """`image/jpeg; charset=x` vira `image/jpeg`; nada vira `None`."""
    if content_type is None:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


async def read_query_image_body(request: Request) -> QueryImageBody:
    """O corpo, recusado por tipo e por tamanho antes de qualquer decodificação."""
    media_type = media_type_of(request.headers.get("content-type"))
    if media_type not in SIGNATURES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Send the picture as the body, with Content-Type image/jpeg "
            "or image/png.",
        )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit():
        if int(declared) > MAX_QUERY_IMAGE_BYTES:
            raise _too_large()
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > MAX_QUERY_IMAGE_BYTES:
            raise _too_large()
    data = bytes(received)
    if not data.startswith(SIGNATURES[media_type]):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"The body does not start like a {media_type} file.",
        )
    return QueryImageBody(media_type=media_type, data=data)


def get_query_image_directory() -> Path:
    """`cache\\query-images` sob a raiz de dados: vem do layout, sem configuração.

    O ponto que os testes substituem, e o que a inicialização da API esvazia --
    o lifespan o resolve por `app.dependency_overrides` --, para que a pasta
    gravada e a pasta esvaziada sejam sempre a mesma.
    """
    return layout.query_image_directory


def query_image_file(
    body: QueryImageBody = Depends(read_query_image_body),
    directory: Path = Depends(get_query_image_directory),
) -> Iterator[Path]:
    """A imagem como arquivo decodificado, apagado quando a requisição termina."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{secrets.token_hex(16)}{EXTENSIONS[body.media_type]}"
    try:
        path.write_bytes(body.data)
        inspect_query_image(path, body.media_type)
        yield path
    finally:
        path.unlink(missing_ok=True)


def empty_query_image_directory(directory: Path) -> int:
    """Apaga os arquivos que um processo morto deixou em `directory`; quantos.

    Uma pasta ausente é uma pasta vazia. Um arquivo que não pode ser apagado --
    aberto em outro processo -- fica, vai para o log, e não impede a API.
    """
    if not directory.is_dir():
        return 0
    removed = 0
    for leftover in directory.iterdir():
        if not leftover.is_file():
            continue
        try:
            leftover.unlink()
            removed += 1
        except OSError as error:
            logger.warning("A query image could not be deleted: %s", error)
    return removed


def _too_large() -> HTTPException:
    megabytes = MAX_QUERY_IMAGE_BYTES // (1024 * 1024)
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=f"The picture is larger than {megabytes} MB.",
    )
