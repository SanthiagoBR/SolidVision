"""Todas as fotos de um lugar, por data de captura, sem consulta (RFC-044).

O caso de uso por trás de *"Ver todas as fotos daqui, por data"*: o cliente de
um profissional fotografa uma obra até ela ficar pronta -- meses de fotos de um
lugar --, e uma busca por *"construção"* ranqueia o prédio pronto pela
semelhança com uma palavra com a qual ele já não se parece. A pergunta é "o que
existe aqui, no tempo", e não tem texto para codificar.

**Não é uma busca, e não fica no módulo da busca** (RFC-044 §5.4). Sem modelo,
sem vetor e sem score: `SearchHit.similarity` é obrigatório de propósito
(RFC-025 §4.1), e uma listagem que inventasse um apresentaria as fotos como
comparadas. O que ela divide com a busca é a *política da requisição* -- o
tamanho da página, o piso do raio e as duas contagens de fotos excluídas --, e
importa essas funções como são, em vez de uma cópia que poderia divergir.

**Páginas por cursor, não por offset** (§2.1). O cursor é o id da última foto
da página anterior, lido de volta pela chave primária: uma página pode começar
onde a anterior parou enquanto um job indexa fotos novas, o que deslocaria um
offset. Pede-se uma imagem a mais que a página, para que `next_after` seja
exato -- o `MAX_MAP_CELLS + 1` do mapa (RFC-032 §7).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.use_cases.search_images import (
    count_hidden_by_unknown_date,
    count_hidden_by_unknown_position,
    validate_limit,
    validate_min_radius,
    validated_request,
)
from app.domain.entities.image import Image
from app.domain.exceptions import ImageNotFoundError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.listing import ListingOrder, ListingSummary
from app.domain.value_objects.search_filters import SearchFilters


@dataclass(frozen=True)
class ListingOverview:
    """O cabeçalho de uma lista: o seu resumo e o que os filtros deixaram de fora.

    `excluded_unknown_date` e `excluded_unknown_position` são os números da
    busca, pelas funções da busca: `None` sem intervalo de datas, ou sem
    círculo, e aí nem são consultados (RFC-028 §8.2). Com um intervalo de
    datas, as fotos sem data ficam fora da lista e são contadas aqui; sem ele,
    elas estão *na* lista, no fim, e `listing.undated` diz quantas são
    (RFC-044 §3.2).
    """

    listing: ListingSummary
    excluded_unknown_date: int | None
    excluded_unknown_position: int | None


@dataclass(frozen=True)
class ImageListing:
    """Uma página de uma listagem.

    `next_after` é o id a mandar como `after` para a próxima página, e é `None`
    exatamente quando a lista acabou. `overview` é o da primeira página, e
    `None` em todas as seguintes: a tela desenha o cabeçalho uma vez, e uma
    página posterior não paga de novo por um número que já tem.
    """

    order: ListingOrder
    limit: int
    images: list[Image]
    next_after: ImageId | None
    overview: ListingOverview | None


class BrowseImagesUseCase:
    """Lista as fotos que os filtros permitem, por data de captura, página a página.

    `default_limit` e `min_radius_m` são injetados pela raiz de composição, a
    partir de `settings.top_k_results` e `settings.min_radius_m`, como em
    `FindSimilarImagesUseCase` -- a camada de aplicação não lê configuração.
    **Sem modelo de embedding**: a listagem nunca codifica nada, e o seu
    provider não deve compor um (§5.4).
    """

    def __init__(
        self,
        repository: ImageRepository,
        default_limit: int,
        min_radius_m: float = 0.0,
    ) -> None:
        validate_limit(default_limit)
        validate_min_radius(min_radius_m)
        self._repository = repository
        self._default_limit = default_limit
        self._min_radius_m = min_radius_m

    def execute(
        self,
        filters: SearchFilters,
        order: ListingOrder = ListingOrder.NEWEST,
        limit: int | None = None,
        after: ImageId | None = None,
    ) -> ImageListing:
        """Uma página da lista, depois de `after` quando ele vem.

        1. O tamanho da página e o círculo são verificados primeiro, pelo
           `validated_request()` da busca: uma requisição inutilizável não
           custa leitura nenhuma.
        2. `after` é lido pela chave primária. Sem linha, é
           `ImageNotFoundError` -- a última foto da página anterior saiu do
           índice --, que a rota responde com um 404 e a tela com *"A lista
           mudou desde a última página"*. A foto não precisa satisfazer os
           filtros: ela é uma posição na ordem.
        3. Pedem-se `limit + 1` imagens; a extra, quando vem, só diz que a
           lista continua.
        4. Sem `after`, o overview: o resumo do repositório e as duas
           contagens da busca.

        **Quaisquer filtros são aceitos, inclusive nenhum.** A coleção inteira
        por data é uma pergunta válida, que custa a varredura do mapa inteiro;
        a interface só a faz com um círculo (§5.1).
        """
        effective_limit = validated_request(
            limit, filters, self._default_limit, self._min_radius_m
        )
        cursor: Image | None = None
        if after is not None:
            cursor = self._repository.get(after)
            if cursor is None:
                raise ImageNotFoundError(
                    f"No indexed image has the id {after}: the list changed since "
                    "the page that ended with it."
                )

        rows = self._repository.list_by_filters(
            filters, order, effective_limit + 1, cursor
        )
        images = rows[:effective_limit]
        next_after = images[-1].id if len(rows) > effective_limit else None

        overview = None
        if after is None:
            overview = ListingOverview(
                listing=self._repository.summarize_listing(filters),
                excluded_unknown_date=count_hidden_by_unknown_date(
                    self._repository, filters
                ),
                excluded_unknown_position=count_hidden_by_unknown_position(
                    self._repository, filters
                ),
            )

        return ImageListing(
            order=order,
            limit=effective_limit,
            images=images,
            next_after=next_after,
            overview=overview,
        )
