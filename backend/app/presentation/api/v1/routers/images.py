"""Pontos de entrada HTTP para busca de imagens e acesso a arquivos (RFC-026, RFC-030).

As rotas não são donas de nada. Interpretam uma query string, entregam dois
valores ao `SearchImagesUseCase` e dão forma de JSON ao que volta -- e não sabem
que a similaridade é um cosseno, que existe um vetor, que a consulta pode ser
traduzida antes de ser codificada, nem que o PostgreSQL está envolvido. Cada uma
dessas coisas vive atrás do caso de uso, que é toda a razão de os três RFCs
anteriores manterem as camadas separadas.

Vale dizer o que isso exclui, porque cada item é algo plausível de se buscar e
cada um passaria nos testes deste módulo: nada de `SessionLocal`, de
`ClipEmbeddingModel`, de `torch`, de reordenar ou filtrar o ranking, de
construir o caso de uso aqui em vez de recebê-lo, nem de cópia da política de
limite da aplicação. `tests/test_ai_layer_boundaries.py` percorre os imports
deste pacote para manter honestos os quatro primeiros.

O RFC-030 acrescentou três rotas aqui, e **cada uma delas recebe um id de imagem
e nada mais**. Nenhuma rota aceita um caminho de arquivo num corpo, numa query
string ou num header -- essa é a propriedade de segurança do `/reveal`
(RFC-030 §5.1), e ela vale para as outras duas porque não há nada para que um
caminho pudesse servir que o id já não faça.

O RFC-032 acrescentou `GET /map` e um círculo no `/search`. **A ordem de
declaração neste arquivo importa**: o FastAPI casa as rotas na ordem em que são
declaradas, então todo caminho literal -- `/search`, `/map` -- tem de vir antes
de `/{image_id}`, ou é lido como um id de imagem e recusado com um 422 para uma
rota que existe.

O RFC-038 acrescentou as duas buscas por imagem: `GET /{image_id}/similar`, a
partir de uma foto que o índice tem, com o seu vetor armazenado e sem modelo; e
`POST /search/by-image`, com os bytes da imagem como corpo -- ainda sem caminho
em lugar nenhum. O `limit` e os seis filtros são uma dependência,
`search_parameters()`, que as três buscas compartilham.

O RFC-044 acrescentou `GET /browse`: as fotos que os filtros permitem, por data
de captura, uma página por vez, sem consulta e sem modelo -- o quarto leitor de
`search_parameters()`, declarado com os outros caminhos literais.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import FileResponse

from app.application.use_cases.aggregate_positions import AggregatePositionsUseCase
from app.application.use_cases.browse_images import BrowseImagesUseCase
from app.application.use_cases.get_image_details import GetImageDetailsUseCase
from app.application.use_cases.get_thumbnail import (
    GetThumbnailUseCase,
    ThumbnailUnchanged,
)
from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.application.use_cases.reveal_image import RevealImageUseCase
from app.application.use_cases.search_images import (
    FindSimilarImagesUseCase,
    SearchImagesUseCase,
    query_image,
)
from app.domain.exceptions import InvalidGeoCircleError
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.listing import ListingOrder
from app.domain.value_objects.position import BoundingBox, Position
from app.domain.value_objects.search_filters import SearchFilters
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.presentation.dependencies import (
    get_aggregate_positions_use_case,
    get_browse_images_use_case,
    get_find_similar_images_use_case,
    get_image_details_use_case,
    get_resolve_image_location_use_case,
    get_reveal_image_use_case,
    get_search_images_use_case,
    get_thumbnail_use_case,
)
from app.presentation.local_file_actions import (
    require_local_file_actions_enabled,
    require_loopback_client,
)
from app.presentation.query_image_upload import REQUEST_BODY, query_image_file
from app.presentation.schemas.browse_schema import BrowseResponseSchema
from app.presentation.schemas.image_schema import ImageSchema
from app.presentation.schemas.map_schema import MapResponseSchema
from app.presentation.schemas.search_schema import (
    RankedImagesSchema,
    SearchResponseSchema,
    SimilarImagesResponseSchema,
)

router = APIRouter(prefix="/images", tags=["images"])
logger = get_logger(__name__)


@dataclass(frozen=True)
class SearchRequest:
    """O tamanho da página e os filtros de uma busca, seja qual for a consulta."""

    limit: int
    filters: SearchFilters


def search_parameters(
    limit: int | None = Query(
        default=None,
        description="Maximum results to return; omitted means the configured default",
    ),
    device_id: list[UUID] | None = Query(
        default=None,
        description=(
            "Restrict results to these devices; repeat the parameter for "
            "several, omit it to search every indexed device"
        ),
    ),
    captured_from: datetime.datetime | None = Query(
        default=None,
        description=(
            "Only photos taken at or after this camera-local time, e.g. "
            "2018-01-01 or 2018-01-01T00:00:00. Must not carry a time zone or "
            "offset. Photos with an unknown date are never included."
        ),
    ),
    captured_to: datetime.datetime | None = Query(
        default=None,
        description=(
            "Only photos taken strictly before this camera-local time; "
            "2019-01-01 ends the year 2018. Must not carry a time zone or "
            "offset."
        ),
    ),
    near_lat: float | None = Query(
        default=None,
        description=(
            "Latitude of the centre of a 'near here' circle, decimal degrees. "
            "Sent with near_lon and radius_m, or not at all."
        ),
    ),
    near_lon: float | None = Query(
        default=None,
        description="Longitude of the centre of the circle, decimal degrees.",
    ),
    radius_m: float | None = Query(
        default=None,
        description=(
            "Radius of the circle in metres. The photo positions are where the "
            "drone was, not the ground it photographed, so radii below the "
            "configured minimum are refused. Photos with no position never "
            "match, however large the circle."
        ),
    ),
) -> SearchRequest:
    """O `limit` e os seis filtros de toda busca, declarados uma vez (RFC-038 §4.5).

    `/search`, `/{image_id}/similar` e `/search/by-image` recebem os mesmos
    parâmetros com as mesmas regras, e três cópias de sete declarações seriam
    três chances de uma delas divergir -- numa descrição, num padrão ou no
    tudo-ou-nada do círculo. As regras em si estão escritas em
    `search_images()`, que é onde os RFC-026 a 032 as puseram.

    O tamanho de página padrão é resolvido aqui, para que cada resposta possa
    ecoar o valor de fato usado.
    """
    return SearchRequest(
        limit=settings.top_k_results if limit is None else limit,
        filters=SearchFilters(
            device_ids=frozenset(DeviceId(value) for value in device_id or ()),
            captured_between=_capture_range(captured_from, captured_to),
            taken_within=_search_circle(near_lat, near_lon, radius_m),
        ),
    )


@router.get(
    "/search",
    status_code=status.HTTP_200_OK,
    response_model=SearchResponseSchema,
    summary="Search indexed images by natural-language query",
)
def search_images(
    q: str = Query(description="Natural-language query, in any supported language"),
    search: SearchRequest = Depends(search_parameters),
    use_case: SearchImagesUseCase = Depends(get_search_images_use_case),
    locations: ResolveImageLocationUseCase = Depends(
        get_resolve_image_location_use_case
    ),
) -> SearchResponseSchema:
    """Retorna as imagens indexadas que melhor casam com `q`, as mais similares antes.

    `def` síncrono, não `async def`, e isso é uma decisão, não um padrão: tudo
    abaixo desta linha bloqueia. Codificar a consulta são ~90 ms de CPU em
    inglês e ~400 ms em português, com a tradução (RFC-025 §12), e o ranking é
    uma chamada DBAPI bloqueante. O FastAPI roda um endpoint `def` num
    threadpool e um endpoint `async def` no event loop, então declarar isto
    como corrotina travaria toda requisição concorrente -- `/health` inclusive
    -- pela duração de um forward pass.

    `limit` não leva restrição `ge`/`le` de propósito. `MAX_SEARCH_LIMIT` já
    enuncia essa política uma vez, em `search_images.py`, com um comentário
    dizendo que é política da aplicação, e não um limite do banco. Copiar
    `100` para esta assinatura criaria uma segunda cópia que diverge em
    silêncio: subir a constante para 200 deixaria o endpoint rejeitando 101
    com um 422 enquanto todo teste de caso de uso continuaria passando. O custo
    é que um limite fora da faixa volta como 400 em vez de 422, que é o status
    mais preciso de todo modo -- `limit=101` é uma requisição bem formada que a
    política recusa.

    O padrão é resolvido aqui, em vez de deixado para o caso de uso, para que a
    resposta possa ecoar o valor de fato aplicado. Nada é duplicado com isso: o
    número resolvido é o que é passado a `execute()`, então o que o cliente
    ouve e o que a busca usou não podem discordar.

    Nem o texto da consulta nem o embedding vão para o log (RFC-026 §16.1). O
    tamanho da consulta basta para correlacionar uma requisição lenta com uma
    consulta longa; o texto é o histórico de buscas de um usuário, e este é um
    produto local-first cuja premissa é que ele continue sendo dele. A contagem
    de dispositivos vai para o log, e não os ids, pela mesma razão -- a quantos
    discos uma busca foi restrita basta para explicar uma resposta rápida ou
    vazia.

    `device_id` se repete para vários dispositivos (`?device_id=A&device_id=B`)
    e é omitido para todos eles. Omitido tem de significar *todos*, nunca
    *nenhum*: um parâmetro ausente que produzisse um `IN ()` vazio
    transformaria toda busca sem filtro em zero resultados, então o caso vazio
    é levado como um `SearchFilters` vazio, que não acrescenta cláusula
    nenhuma.

    Um id de dispositivo desconhecido não é rejeitado. O filtro restringe um
    conjunto de candidatos em vez de afirmar que os seus membros existem, então
    uma busca que nomeia um disco que o sistema nunca viu corretamente não
    casa com nada (RFC-027 §9). Se os discos nomeados estão *conectados*
    também não é perguntado aqui, e nem poderia ser com proveito: a busca
    ordena o que está indexado, e um resultado num disco na gaveta é a resposta
    que o produto existe para dar.

    Desde o RFC-030, todo resultado traz o seu dispositivo, o seu
    `relative_path` e o seu `absolute_path` quando o disco está plugado.
    Resolver isso é um segundo caso de uso chamado depois da busca, e não um
    passo dentro dela: onde um arquivo está não tem nada a ver com o quanto ele
    casou, e `GET /images/{id}` precisa da mesma resposta sem busca nenhuma. O
    sistema operacional é perguntado uma vez por disco distinto na página, não
    uma vez por resultado (RFC-030 §4.2).

    `captured_from` / `captured_to` (RFC-028) são as duas pontas de uma faixa
    semiaberta, `[from, to)`, e qualquer uma pode ser enviada sozinha. Uma
    ponta omitida vira `datetime.min` ou `datetime.max` *aqui*, para que
    `DateRange` continue um valor com dois limites obrigatórios em vez de
    ganhar um `None` que todo consumidor abaixo teria de tratar. Omitir as duas
    não é uma faixa sem limites; é nenhum filtro de data, porque `[min, max)`
    descartaria em silêncio toda foto com data desconhecida.

    **Um limite com fuso ou offset é recusado, nunca convertido.** O Pydantic
    interpreta `2018-01-01T00:00:00Z`, `...-03:00` e até um timestamp Unix puro
    como um `datetime` aware, e `DateRange` o rejeita com um `DomainError`, que
    o handler global do app responde com 400 e uma mensagem nomeando o fuso.
    Converter exigiria escolher um fuso *para o qual* converter, e a data de
    captura é local da câmera justamente porque esse fuso não existe
    (RFC-028 §5). Uma faixa invertida é recusada do mesmo jeito. Um valor que
    nem é data é um 422 do FastAPI, como para qualquer parâmetro malformado.

    `excluded_unknown_date` só é calculado quando uma faixa foi enviada. É uma
    segunda consulta, e uma busca sem filtro não deve pagar por ela.

    `near_lat` / `near_lon` / `radius_m` (RFC-032 §6) são **tudo ou nada**:
    dois de três é uma requisição malformada e responde 400 nomeando o que
    falta. Completar um raio ausente com um padrão inventaria a intenção do
    usuário exatamente na grandeza que o RFC-032 §2.2 mostrou ser crítica. Uma
    coordenada fora do planeta, um raio que não é positivo, ou um abaixo de
    `settings.min_radius_m` é um 400 com a mensagem do domínio; um valor que
    não é número é o 422 do FastAPI. Metros, nunca quilômetros: uma unidade
    na stack inteira. `excluded_unknown_position` segue a regra do seu gêmeo
    de data -- calculado só quando um círculo foi enviado.

    Nem a consulta nem as coordenadas vão para o log: perto de onde um usuário
    buscou é tão privado quanto o que ele buscou (RFC-026 §16.1), então o log
    diz só que havia um círculo.
    """
    effective_limit = search.limit
    filters = search.filters
    logger.info(
        "search requested: query_length=%d, limit=%d, devices=%d, date_range=%s, "
        "circle=%s",
        len(q),
        effective_limit,
        len(filters.device_ids),
        filters.captured_between is not None,
        filters.taken_within is not None,
    )

    started_at = time.perf_counter()
    hits = use_case.execute(q, effective_limit, filters)
    excluded_unknown_date = use_case.count_hidden_by_unknown_date(filters)
    excluded_unknown_position = use_case.count_hidden_by_unknown_position(filters)
    searched_at = time.perf_counter()
    located = locations.execute([hit.image for hit in hits])
    located_at = time.perf_counter()

    # `elapsed_ms` continua significando o que o RFC-026 mediu -- a busca -- e o
    # passo de localização vai para o log ao lado dele, e não somado a ele, para
    # que uma enumeração de volumes lenta não se esconda num número que antes
    # era o CLIP.
    logger.info(
        "search completed: results=%d, elapsed_ms=%.0f, locate_ms=%.1f",
        len(hits),
        (searched_at - started_at) * 1000,
        (located_at - searched_at) * 1000,
    )
    return SearchResponseSchema.from_hits(
        query=q,
        limit=effective_limit,
        hits=hits,
        locations=located,
        excluded_unknown_date=excluded_unknown_date,
        excluded_unknown_position=excluded_unknown_position,
    )


@router.post(
    "/search/by-image",
    status_code=status.HTTP_200_OK,
    response_model=RankedImagesSchema,
    summary="Search indexed images by an example picture",
    openapi_extra={"requestBody": REQUEST_BODY},
    responses={
        400: {"description": "The picture cannot be decoded, or a filter is refused"},
        413: {"description": "The picture is larger than 50 MB"},
        415: {"description": "Not image/jpeg or image/png, or not what it says"},
        503: {"description": "The model cannot be loaded; the detail says why"},
    },
)
def search_by_image(
    picture: Path = Depends(query_image_file),
    search: SearchRequest = Depends(search_parameters),
    use_case: SearchImagesUseCase = Depends(get_search_images_use_case),
    locations: ResolveImageLocationUseCase = Depends(
        get_resolve_image_location_use_case
    ),
) -> RankedImagesSchema:
    """Ordena o índice contra uma imagem que o usuário soltou (RFC-038 §4.2).

    `execute_similar_to()`, como o `--image` da CLI o roda, sobre a mesma
    `Image` transitória (`query_image()`): o próprio embedding CLIP da imagem é
    a consulta. Precisa do codificador de imagem, e não do tradutor.

    **Os bytes são o corpo, e nenhum caminho entra** (RFC-030 §5.1).
    `query_image_file` recusa um tipo errado (415), mais de 50 MB (413) e uma
    imagem que o Pillow não consegue decodificar (400), nessa ordem e antes de
    o caso de uso ser composto -- então um upload recusado nunca importa o
    `torch` -- e apaga o seu arquivo temporário como quer que a requisição
    termine.

    **Declarada ao lado de `/search`, antes de `/{image_id}`**, pela regra
    deste arquivo. Um `POST` de dois segmentos só poderia colidir com
    `/{image_id}/reveal`, e `by-image` não é `reveal`; está aqui pela leitura.
    `test_search_by_image_api.py` a pede e exige um 200.

    Os filtros são os do `/search`, pelos mesmos parâmetros. Nada da imagem vai
    para o log além do tamanho e do tipo, e dos filtros só quais estão
    presentes (RFC-026 §16.1). A resposta é a do `/search` sem `query`: a
    imagem não está indexada, então não há nada a tirar.
    """
    filters = search.filters
    logger.info(
        "image search requested: bytes=%d, type=%s, limit=%d, devices=%d, "
        "date_range=%s, circle=%s",
        picture.stat().st_size,
        picture.suffix.lstrip("."),
        search.limit,
        len(filters.device_ids),
        filters.captured_between is not None,
        filters.taken_within is not None,
    )
    started_at = time.perf_counter()
    hits = use_case.execute_similar_to(query_image(picture), search.limit, filters)
    excluded_unknown_date = use_case.count_hidden_by_unknown_date(filters)
    excluded_unknown_position = use_case.count_hidden_by_unknown_position(filters)
    searched_at = time.perf_counter()
    located = locations.execute([hit.image for hit in hits])
    logger.info(
        "image search completed: results=%d, elapsed_ms=%.0f, locate_ms=%.1f",
        len(hits),
        (searched_at - started_at) * 1000,
        (time.perf_counter() - searched_at) * 1000,
    )
    return RankedImagesSchema.from_ranking(
        limit=search.limit,
        hits=hits,
        locations=located,
        excluded_unknown_date=excluded_unknown_date,
        excluded_unknown_position=excluded_unknown_position,
    )


@router.get(
    "/map",
    status_code=status.HTTP_200_OK,
    response_model=MapResponseSchema,
    summary="Where the photos are: counts per map cell inside an area",
)
def map_images(
    min_lat: float = Query(description="South edge of the area, decimal degrees"),
    min_lon: float = Query(description="West edge of the area, decimal degrees"),
    max_lat: float = Query(description="North edge of the area, decimal degrees"),
    max_lon: float = Query(description="East edge of the area, decimal degrees"),
    precision: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Decimal places of a degree to round positions to: 3 is about "
            "100 m cells. Omitted means 3. The response says which precision "
            "was applied, which is coarser when the grid would have too many "
            "cells."
        ),
    ),
    device_id: list[UUID] | None = Query(
        default=None,
        description="Only photos on these devices; repeat for several",
    ),
    captured_from: datetime.datetime | None = Query(
        default=None,
        description="Only photos taken at or after this camera-local time",
    ),
    captured_to: datetime.datetime | None = Query(
        default=None,
        description="Only photos taken strictly before this camera-local time",
    ),
    use_case: AggregatePositionsUseCase = Depends(get_aggregate_positions_use_case),
) -> MapResponseSchema:
    """Conta as fotos numa viewport, célula por célula (RFC-032 §7).

    **Declarada antes de `/{image_id}`, ao lado de `/search`, e essa ordem é
    a questão.** Declarada depois, `/images/map` casaria como
    `image_id="map"` e seria recusada com um 422 -- para uma rota que existe.
    `tests/presentation/test_map_api.py` a pede e exige um 200.

    **O texto da consulta é ignorado -- não há `q` -- por desenho.** Um mapa
    que refletisse a consulta só poderia agregar o top K, e o top 100 de um
    índice de 100.000 imagens não diz nada sobre geografia. O mapa mostra onde
    estão as fotos que os *filtros* permitem; ordenar é trabalho do `/search`.
    Os filtros são os da própria busca -- `device_id`, `captured_from`,
    `captured_to`, pelos mesmos parâmetros e pelas mesmas regras -- e um
    círculo não é um deles: a área é a viewport.

    Uma área cujo `min` excede o seu `max` é um 400 -- na longitude, isso é ou
    cantos trocados ou uma viewport que cruza o meridiano 180, que este
    endpoint não suporta (RFC-032 §11). Um `def` síncrono, como o `/search`,
    porque o agrupamento é uma chamada bloqueante ao banco.
    """
    area = BoundingBox(
        min_latitude=min_lat,
        min_longitude=min_lon,
        max_latitude=max_lat,
        max_longitude=max_lon,
    )
    filters = SearchFilters(
        device_ids=frozenset(DeviceId(value) for value in device_id or ()),
        captured_between=_capture_range(captured_from, captured_to),
    )
    started_at = time.perf_counter()
    answer = use_case.execute(area, filters, precision)
    logger.info(
        "map completed: cells=%d, precision=%d->%d, devices=%d, date_range=%s, "
        "elapsed_ms=%.0f",
        len(answer.cells),
        answer.precision_requested,
        answer.precision_applied,
        len(filters.device_ids),
        filters.captured_between is not None,
        (time.perf_counter() - started_at) * 1000,
    )
    return MapResponseSchema.from_map(answer)


@router.get(
    "/browse",
    status_code=status.HTTP_200_OK,
    response_model=BrowseResponseSchema,
    summary="Every photo under the filters, by capture date, a page at a time",
    responses={
        404: {
            "description": (
                "after names no indexed image: the last photo of the page before "
                "left the index"
            )
        }
    },
)
def browse_images(
    order: ListingOrder = Query(
        default=ListingOrder.NEWEST,
        description=(
            "newest (the default) or oldest first, by capture date. Photos with "
            "no capture date come last in both."
        ),
    ),
    after: UUID | None = Query(
        default=None,
        description=(
            "The next_after of the page before; omitted for the first page. "
            "The list continues after that photo."
        ),
    ),
    search: SearchRequest = Depends(search_parameters),
    use_case: BrowseImagesUseCase = Depends(get_browse_images_use_case),
    locations: ResolveImageLocationUseCase = Depends(
        get_resolve_image_location_use_case
    ),
) -> BrowseResponseSchema:
    """*"Ver todas as fotos daqui, por data"*: as fotos de um lugar, no tempo (RFC-044).

    **Sem consulta e sem modelo.** O provider do caso de uso não depende de
    `get_embedding_model()`, que importaria o `torch` para uma rota que nunca
    codifica nada; então isto responde com a busca `unavailable`, que é o único
    jeito de ver as fotos de um lugar sem o modelo. Nada de 503.

    **Declarada antes de `/{image_id}`**, ao lado de `/search` e `/map`, pela
    regra deste arquivo: depois dela, `/images/browse` seria `image_id="browse"`
    e um 422. `test_browse_api.py` a pede e exige um 200.

    Os filtros são os da busca, pela mesma dependência, com as mesmas regras e
    os mesmos 400. Qualquer conjunto é aceito, o vazio inclusive: a coleção
    inteira por data é uma pergunta válida. `after` é um id e nunca um caminho
    (RFC-030 §5.1): a chave do cursor é lida do banco. Um `after` que não nomeia
    linha nenhuma é um 404 -- a foto saiu do índice desde a página anterior.

    Uma linha INFO por requisição, com a direção, os tamanhos, se havia cursor,
    quantos dispositivos e se havia faixa ou círculo -- **nunca as coordenadas,
    as datas da faixa ou o id do cursor**: onde alguém olha é a consulta desta
    rota (RFC-026 §16.1, RFC-040 §2.7).
    """
    filters = search.filters
    started_at = time.perf_counter()
    cursor = ImageId(after) if after is not None else None
    listing = use_case.execute(filters, order, search.limit, cursor)
    listed_at = time.perf_counter()
    located = locations.execute(listing.images)
    logger.info(
        "browse completed: order=%s, limit=%d, results=%d, cursor=%s, devices=%d, "
        "date_range=%s, circle=%s, elapsed_ms=%.0f, locate_ms=%.1f",
        order.value,
        listing.limit,
        len(listing.images),
        after is not None,
        len(filters.device_ids),
        filters.captured_between is not None,
        filters.taken_within is not None,
        (listed_at - started_at) * 1000,
        (time.perf_counter() - listed_at) * 1000,
    )
    return BrowseResponseSchema.from_listing(listing, located)


@router.get(
    "/{image_id}",
    status_code=status.HTTP_200_OK,
    response_model=ImageSchema,
    summary="Describe one indexed image, and where it is right now",
    responses={404: {"description": "No indexed image has this id"}},
)
def get_image(
    image_id: UUID,
    use_case: GetImageDetailsUseCase = Depends(get_image_details_use_case),
) -> ImageSchema:
    """Retorna uma imagem: o seu dispositivo, os seus caminhos e se o disco está aqui.

    **200 quando o disco está desplugado** (RFC-030 §4.2). A linha existe e
    isto descreve a linha; `device.connected: false` com `device.label` é a
    resposta "está no HD3", não um erro.

    Declarada depois de `/search` e `/map`, e essa ordem importa: as rotas
    casam na ordem de declaração, e `/{image_id}` de outro modo capturaria
    qualquer uma das duas e a recusaria como UUID malformado.
    """
    return ImageSchema.from_located(use_case.execute(ImageId(image_id)))


@router.get(
    "/{image_id}/thumbnail",
    status_code=status.HTTP_200_OK,
    response_class=FileResponse,
    summary="The image's thumbnail, from the application's cache",
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The thumbnail"},
        304: {"description": "The client's cached copy is current"},
        404: {
            "description": (
                "No such image, or it has no thumbnail yet -- show a "
                "placeholder rather than an error"
            )
        },
    },
)
def get_thumbnail(
    image_id: UUID,
    if_none_match: str | None = Header(default=None),
    use_case: GetThumbnailUseCase = Depends(get_thumbnail_use_case),
) -> Response:
    """Serve a thumbnail, ou `304` quando a cópia do cliente ainda está certa.

    **Servida com ou sem o disco da foto plugado.** As thumbnails vivem no
    cache do próprio aplicativo, num disco que está sempre lá, e esse é o único
    jeito de um usuário *ver* uma foto cujo disco está na gaveta
    (RFC-030 §4.1).

    **Não é um mount de `StaticFiles`.** Um mount serviria pelo nome do
    arquivo, com os seus próprios validadores, e não conseguiria responder 404
    para um id sem linha de thumbnail nem conferir o `If-None-Match` contra o
    hash de conteúdo da foto antes de tocar no disco.

    **O `ETag` é o hash de conteúdo da foto, e o `Cache-Control` é `no-cache`
    -- não `max-age=31536000`, que o RFC-030 §7.2, como proposto, combinava com
    ele.** Essa combinação reintroduz o bug que a seção estava corrigindo. Uma
    resposta com um ano de validade é servida do cache do navegador sem
    perguntar nada ao servidor (RFC 9111 §4.2), então o `ETag` nunca seria
    reenviado, e uma foto sobrescrita no lugar -- mesmo caminho, mesmo id --
    manteria a thumbnail antiga por um ano, que é exatamente a falha que o
    `immutable` tinha. `no-cache` guarda a thumbnail e a revalida a cada uso;
    uma cópia atual custa um `304` sem corpo e sem leitura de arquivo --
    medido em 6,6 ms de mediana in-process contra o PostgreSQL, contra 8,6 ms
    para enviar o arquivo de 45 KB (`experiments/rfc-030-file-access/
    measure_reveal_and_revalidation.log`). A maior parte disso é a leitura dos
    metadados, então a revalidação economiza mais bytes que tempo numa API
    local, e uma página de cinquenta thumbnails a paga cinquenta vezes. Esse é
    o preço declarado de nunca mostrar uma imagem velha. `private` impede que
    um cache compartilhado entre o usuário e a própria máquina dele, se algum
    dia houver um, guarde as suas fotos.

    **Uma linha sem hash de conteúdo recebe `no-store`.** Linhas indexadas
    antes do RFC-024 não têm contra o que validar, e uma entrada de cache que
    nunca pode ser revalidada é uma que sempre erra em silêncio.

    `If-None-Match` é interpretado aqui porque é HTTP, e comparado no caso de
    uso porque "a minha cópia está atual" não é. Tags fracas comparam de modo
    fraco, como o RFC 9110 §13.1.2 exige para este header, e `*` não é
    atendido -- nenhum navegador o envia num `GET`.
    """
    result = use_case.execute(ImageId(image_id), _entity_tags(if_none_match))
    headers = _thumbnail_cache_headers(result.version)
    if isinstance(result, ThumbnailUnchanged):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return FileResponse(result.path, headers=headers)


@router.get(
    "/{image_id}/similar",
    status_code=status.HTTP_200_OK,
    response_model=SimilarImagesResponseSchema,
    summary="The indexed images that look most like this one",
    responses={
        404: {"description": "No indexed image has this id"},
        409: {"description": "The image is known and has no embedding yet"},
    },
)
def similar_images(
    image_id: UUID,
    search: SearchRequest = Depends(search_parameters),
    use_case: FindSimilarImagesUseCase = Depends(get_find_similar_images_use_case),
    locations: ResolveImageLocationUseCase = Depends(
        get_resolve_image_location_use_case
    ),
) -> SimilarImagesResponseSchema:
    """*"Parecidas com esta"*: ordena o índice contra uma foto que ele tem (RFC-038).

    **O embedding armazenado, e nenhum modelo** (§4.1). O vetor que a indexação
    pagou é lido de volta e ordenado com o mesmo `search_similar()` de uma
    consulta de texto, então isto responde com o disco da foto na gaveta, com
    a busca `loading` ou `unavailable`, e sem decodificar um pixel. O provider
    do caso de uso não depende de `get_embedding_model()`, que importaria o
    `torch` para uma rota que nunca codifica nada.

    A própria foto não está nos resultados -- viria primeiro, com similaridade
    1 -- e as suas cópias estão, perto do topo: os mesmos bytes em outro disco
    respondem *"onde mais está esta foto"*. Os filtros são os do `/search`,
    pelos mesmos parâmetros e regras.

    404 para um id que não nomeia linha nenhuma, 409 para uma linha sem
    embedding: o segundo pedido dá certo assim que a foto é indexada. Nada dos
    filtros vai para o log além de quais estão presentes.
    """
    filters = search.filters
    logger.info(
        "similar requested: limit=%d, devices=%d, date_range=%s, circle=%s",
        search.limit,
        len(filters.device_ids),
        filters.captured_between is not None,
        filters.taken_within is not None,
    )
    started_at = time.perf_counter()
    hits = use_case.execute(ImageId(image_id), search.limit, filters)
    excluded_unknown_date = use_case.count_hidden_by_unknown_date(filters)
    excluded_unknown_position = use_case.count_hidden_by_unknown_position(filters)
    searched_at = time.perf_counter()
    located = locations.execute([hit.image for hit in hits])
    logger.info(
        "similar completed: results=%d, elapsed_ms=%.0f, locate_ms=%.1f",
        len(hits),
        (searched_at - started_at) * 1000,
        (time.perf_counter() - searched_at) * 1000,
    )
    return SimilarImagesResponseSchema.from_source(
        image_id=image_id,
        limit=search.limit,
        hits=hits,
        locations=located,
        excluded_unknown_date=excluded_unknown_date,
        excluded_unknown_position=excluded_unknown_position,
    )


@router.post(
    "/{image_id}/reveal",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Open the file manager on this machine with the image selected",
    dependencies=[
        Depends(require_local_file_actions_enabled),
        Depends(require_loopback_client),
    ],
    responses={
        403: {"description": "The caller is not on this machine"},
        404: {
            "description": (
                "No such image -- or local file actions are switched off, in "
                "which case the route answers as if it did not exist"
            )
        },
        409: {"description": "The image's disk is not connected"},
        410: {"description": "The disk is connected and the file is not on it"},
    },
)
def reveal_image(
    image_id: UUID,
    use_case: RevealImageUseCase = Depends(get_reveal_image_use_case),
) -> Response:
    """Pede ao Explorer que mostre o arquivo, e responde assim que ele foi lançado.

    **A requisição leva um id e nada mais** -- nenhum corpo, nenhum caminho,
    nenhum header que o handler leia (RFC-030 §5.1). O servidor acha a linha,
    resolve onde o disco dela está montado agora e monta o caminho ele mesmo,
    então não há texto do cliente em que contrabandear `..` ou um
    compartilhamento UNC.

    **`POST`, porque isto tem um efeito colateral no mundo físico**
    (RFC-030 §5.2). Um `GET` poderia ser pré-carregado por qualquer coisa que
    segue links, e uma página de dez resultados abriria dez janelas.

    As duas guardas são dependências no decorator, que o FastAPI resolve antes
    de `use_case`: uma requisição recusada nunca abre uma sessão de banco nem
    enumera um volume. Veja `local_file_actions.py`.

    204 significa que o processo foi lançado. O status de saída do Explorer não
    significa nada, então nada aqui pode dizer que uma janela de fato abriu.
    """
    use_case.execute(ImageId(image_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _entity_tags(header: str | None) -> frozenset[str]:
    """As tags opacas de um header `If-None-Match`, sem aspas, fraqueza ignorada."""
    if not header:
        return frozenset()
    tags: set[str] = set()
    for raw in header.split(","):
        tag = raw.strip().removeprefix("W/")
        if len(tag) >= 2 and tag.startswith('"') and tag.endswith('"'):
            tag = tag[1:-1]
        if tag:
            tags.add(tag)
    return frozenset(tags)


def _thumbnail_cache_headers(version: str | None) -> dict[str, str]:
    """`ETag` mais revalidação sempre quando há versão; sem cache quando não há."""
    if version is None:
        return {"Cache-Control": "no-store"}
    return {"ETag": f'"{version}"', "Cache-Control": "private, no-cache"}


def _capture_range(
    captured_from: datetime.datetime | None, captured_to: datetime.datetime | None
) -> DateRange | None:
    """Preenche uma ponta omitida com o extremo que ela representa; `None` se ambas."""
    if captured_from is None and captured_to is None:
        return None
    return DateRange(
        start=captured_from if captured_from is not None else datetime.datetime.min,
        end=captured_to if captured_to is not None else datetime.datetime.max,
    )


def _search_circle(
    near_lat: float | None, near_lon: float | None, radius_m: float | None
) -> GeoCircle | None:
    """Os três parâmetros do círculo como um valor só -- todos eles, ou nenhum.

    `None` quando nenhum foi enviado: nenhum filtro espacial, não um círculo em
    volta do planeta. Dois de três é recusado, nomeando o que falta, em vez de
    completado com um raio padrão (RFC-032 §6).
    """
    given = {"near_lat": near_lat, "near_lon": near_lon, "radius_m": radius_m}
    missing = [name for name, value in given.items() if value is None]
    if len(missing) == len(given):
        return None
    if missing:
        raise InvalidGeoCircleError(
            "A 'near here' search needs near_lat, near_lon and radius_m together; "
            f"missing: {', '.join(missing)}."
        )
    assert near_lat is not None and near_lon is not None and radius_m is not None
    return GeoCircle(center=Position(near_lat, near_lon), radius_m=radius_m)
