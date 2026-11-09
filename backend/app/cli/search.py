"""Busca semântica pela linha de comando, composta sobre `SearchImagesUseCase`.

Hoje não há entrada para a busca fora do HTTP (o RFC-025/RFC-026 a entregou só
como rota do FastAPI). Este módulo compõe o mesmo caso de uso que a API usa, do
jeito que `job_runner.build_runner()` já o compõe para a indexação: o
`PostgresImageRepository` sobre uma sessão nova, e o adaptador CLIP do
processo, de `app.presentation.dependencies.get_embedding_model()`.

Duas consultas: texto (`run()`) e uma imagem (`run_image()`), que ranqueia o
índice contra o embedding CLIP da própria imagem. As duas podem ser
restringidas a um círculo -- `--near LAT LON --radius METRES` -- com as regras
da API para `near_lat`/`near_lon`/`radius_m` (RFC-032 §6), e todo resultado é
impresso com o lugar onde foi tirado.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from app.application.use_cases.search_images import query_image as query_image
from app.domain.entities.image import Image
from app.domain.exceptions import (
    EmptySearchQueryError,
    InvalidGeoCircleError,
    InvalidPositionError,
    InvalidSearchLimitError,
)
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.position import Position
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHits

# Recusas do próprio pedido, que quem digita consegue corrigir: impressas como
# a mensagem delas, e não como traceback. Todo o resto -- um banco fora do ar,
# um embedding do tamanho errado -- é uma falha, e mantém o traceback.
_REQUEST_REFUSALS = (
    EmptySearchQueryError,
    InvalidSearchLimitError,
    InvalidGeoCircleError,
)


def format_position(image: Image) -> str:
    """Onde `image` foi tirada, como `lat, lon` -- ou por que isso não se sabe.

    Seis casas decimais (~0,1 m) na ordem `lat, lon`: a forma que a caixa de
    busca de um mapa aceita colada como está. É a posição da aeronave, e não a
    do chão que ela fotografou (RFC-032 §2.2).

    As duas maneiras de não ter posição têm textos diferentes porque pedem
    ações diferentes. Nunca examinada (`position_source` é `None`) quer dizer
    que a imagem foi indexada antes do RFC-032, e o `exif_backfill` vai lê-la;
    examinada e `unknown` quer dizer que o arquivo não tem GPS utilizável, e
    nada vai.
    """
    position = image.position
    if position is not None:
        return f"{position.latitude:.6f}, {position.longitude:.6f}"
    if image.position_source is None:
        return "position not read yet"
    return "no position"


def format_hits(hits: SearchHits) -> str:
    """Escreve os resultados ranqueados, um por linha, do mais parecido.

    Uma função pura, para que o formato da saída possa ser testado sem banco
    nem modelo -- `run()` é o único chamador que precisa de um deles.
    """
    if not hits:
        return "No results."
    return "\n".join(
        f"{rank}. {hit.similarity:.3f}  {hit.image.relative_path}  "
        f"(device {hit.image.device_id})  {format_position(hit.image)}"
        for rank, hit in enumerate(hits, start=1)
    )


def format_hidden_by_unknown_position(count: int) -> str:
    """Diz quantas imagens um círculo não pôde considerar por não terem posição.

    O `excluded_unknown_position` da CLI (RFC-032 §6.2): sem ele, uma página
    vazia se lê como "nenhuma foto foi tirada aqui", quando a verdade pode ser
    "estas fotos nunca foram postas em lugar nenhum".
    """
    noun = "image has" if count == 1 else "images have"
    return (
        f"{count} indexed {noun} no known position and could not be matched "
        "against the circle."
    )


def search_filters(
    near: Sequence[float] | None, radius_m: float | None
) -> SearchFilters | None:
    """`--near LAT LON` e `--radius METRES` como filtro -- os dois, ou nenhum.

    `None` quando nenhum foi dado: filtro nenhum, que é exatamente a busca que
    este módulo fazia antes do RFC-032. Meio círculo é recusado, em vez de
    completado com um raio padrão, pela razão por que a API o recusa (RFC-032
    §6): o raio é a parte da pergunta que o deslocamento do drone torna
    crítica, e só quem pergunta a conhece.

    Chamado antes de o modelo ser carregado ou de o banco ser aberto, então um
    círculo malformado não custa nada. O piso do raio (`settings.min_radius_m`)
    não é checado aqui: é política da Application, e o caso de uso o aplica com
    a mensagem que o explica.
    """
    if near is None and radius_m is None:
        return None
    if near is None or radius_m is None:
        raise SystemExit(
            "--near LAT LON and --radius METRES go together: a circle needs "
            "a centre and a radius, and the radius is never assumed."
        )
    latitude, longitude = near
    try:
        circle = GeoCircle(center=Position(latitude, longitude), radius_m=radius_m)
    except (InvalidGeoCircleError, InvalidPositionError) as error:
        raise SystemExit(str(error)) from error
    return SearchFilters(taken_within=circle)


def run(
    query: str,
    limit: int | None,
    near: Sequence[float] | None = None,
    radius_m: float | None = None,
) -> None:
    """Busca `query` no índice e imprime os resultados ranqueados."""
    _search(query, limit, search_filters(near, radius_m))


def run_image(
    path: Path,
    limit: int | None,
    near: Sequence[float] | None = None,
    radius_m: float | None = None,
) -> None:
    """Acha as imagens indexadas mais parecidas com a imagem em `path`.

    A consulta é a própria imagem, codificada pelo encoder de imagem do CLIP,
    então a foto de uma foto impressa -- ou qualquer cópia -- consegue achar o
    original sem que ninguém o descreva em palavras. Corte tudo o que não é a
    imagem antes de consultar (uma mesa, uma mão, uma moldura): o CLIP vê o
    arquivo inteiro, e o fundo é parte do que ele compara.

    Um arquivo ausente é recusado antes de o modelo ser carregado ou de o banco
    ser aberto, para que o erro nomeie o caminho, e não um decodificador.
    """
    if not path.is_file():
        raise SystemExit(f"No image file at {path}")
    _search(query_image(path), limit, search_filters(near, radius_m))


def _search(
    query: str | Image, limit: int | None, filters: SearchFilters | None
) -> None:
    """Compõe o caso de uso como a API faz, roda uma consulta, imprime a página.

    Imports locais à função pela mesma razão por que `indexing_worker.run()`
    os usa: importar este módulo não pode arrastar o SQLAlchemy ou o adaptador
    CLIP para um grafo de imports que um teste da leitura de argumentos de
    `app.cli.__main__` não precisa pagar.

    `min_radius_m` é injetado da configuração como
    `get_search_images_use_case()` o injeta, então um círculo que a API
    recusaria é recusado aqui também. A contagem de imagens que o círculo não
    pôde considerar só é impressa quando não é zero; sem círculo, o caso de uso
    nem a pede ao repositório.

    A presença dos dois modelos é checada primeiro, antes de importar o
    `torch` ou abrir o banco (RFC-035 §6.1). Um modelo que não pode ser
    carregado, aqui ou na carga, levanta `ModelUnavailableError`, que
    `app.cli.__main__` imprime numa linha, com código de saída 78 (RFC-036
    §6.1).
    """
    from app.infrastructure.ai.model_runtime import default_runtime

    default_runtime().check_presence()

    from app.application.use_cases.search_images import SearchImagesUseCase
    from app.infrastructure.config.settings import settings
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal
    from app.presentation.dependencies import get_embedding_model

    session = SessionLocal()
    try:
        use_case = SearchImagesUseCase(
            repository=PostgresImageRepository(session),
            embedding_model=get_embedding_model(),
            default_limit=settings.top_k_results,
            min_radius_m=settings.min_radius_m,
        )
        if isinstance(query, Image):
            hits = use_case.execute_similar_to(query, limit=limit, filters=filters)
        else:
            hits = use_case.execute(query, limit=limit, filters=filters)
        hidden = use_case.count_hidden_by_unknown_position(filters)
    except _REQUEST_REFUSALS as error:
        raise SystemExit(str(error)) from error
    finally:
        session.close()

    print(format_hits(hits))
    if hidden:
        print(format_hidden_by_unknown_position(hidden))
