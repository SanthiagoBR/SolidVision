"""Caso de uso da Application para a busca semântica de imagens (RFC-025).

O caminho inteiro da consulta, e a camada que não detém nenhuma das mecânicas
dele:

    query text -> EmbeddingModelPort.encode_text()
               -> ImageRepository.search_similar()
               -> ranked SearchHit list

ou, com uma imagem como consulta (`execute_similar_to()`):

    query image -> EmbeddingModelPort.encode_image() -> the same two steps

ou, com uma foto que já está indexada (`FindSimilarImagesUseCase`, RFC-038):

    image id -> ImageRepository.get_embedding() -> the same ranking, no model

A detecção de idioma, a tradução e o template do prompt vivem dentro do
adaptador de embedding; a distância de cosseno, a ordenação e o top-K vivem
dentro do repositório. O que sobra aqui é a parte que não pertence a nenhum
dos dois: decidir o que conta como um pedido utilizável, e entregar o vetor
resultante ao lugar que consegue ranquear contra ele.

**A política do pedido são funções deste módulo, e os dois casos de uso as
chamam** (RFC-038 §4.1): o tamanho da página, o piso do raio e as duas
contagens de fotos excluídas são uma política só, e duas cópias dela seriam
duas respostas para a mesma pergunta.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.domain.entities.image import Image
from app.domain.exceptions import (
    EmptySearchQueryError,
    ImageNotFoundError,
    ImageNotIndexedError,
    InvalidGeoCircleError,
    InvalidSearchLimitError,
)
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHit

# A maior página que qualquer chamador pode pedir. Política da Application, e
# não limite do repositório ou do banco: existe para que um pedido não possa
# exigir de uma futura camada HTTP a serialização de um conjunto de resultados
# sem limite, e é imposta levantando um erro (ver `InvalidSearchLimitError`),
# e não cortando, para que um chamador que pede mais fique sabendo.
MAX_SEARCH_LIMIT = 100


class SearchImagesUseCase:
    """Transforma uma consulta de texto numa lista ranqueada de imagens.

    `default_limit` é injetado em vez de lido da configuração, seguindo o
    `batch_size` do `IndexOrUpdateImagesUseCase`: a camada Application não
    pode importar `settings` (seria uma dependência da Infrastructure, que o
    `test_application_architecture.py` impede), então a composition root
    passa `settings.top_k_results`.

    `min_radius_m` chega do mesmo jeito, de `settings.min_radius_m` (RFC-032
    §6). É política, e não propriedade dos círculos -- `GeoCircle` aceita
    qualquer raio positivo -- e a política é sobre aeronaves: o GPS registra
    onde o drone estava, e uma foto oblíqua mostra um chão a dezenas ou
    centenas de metros (§2.2). Um círculo menor que esse deslocamento
    esconde, com toda a confiança e sem erro, exatamente a foto procurada.
    Com padrão `0.0` -- sem piso -- para que as composições escritas antes do
    RFC-032 continuem se comportando como antes; a API passa o configurado.
    """

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
        default_limit: int,
        min_radius_m: float = 0.0,
    ) -> None:
        validate_limit(default_limit)
        validate_min_radius(min_radius_m)
        self._repository = repository
        self._embedding_model = embedding_model
        self._default_limit = default_limit
        self._min_radius_m = min_radius_m

    def execute(
        self,
        query: str,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        """Devolve as imagens que melhor casam com `query`, da mais parecida.

        A ordem devolvida é a do repositório, repassada intocada. Reordenar
        aqui seria redundante e errado: o repositório ranqueou contra os
        embeddings que guarda, e esta camada não tem acesso a eles -- ordenar
        por `SearchHit.similarity` na melhor hipótese reproduziria essa ordem
        e, na pior, discordaria dela em silêncio sobre os empates.

        `limit` assume o `default_limit` injetado quando omitido. `None`
        quer dizer "não especificado", nunca "sem limite": não há como pedir
        todas as imagens, porque o `MAX_SEARCH_LIMIT` teria de impedir isso
        de qualquer jeito.

        `filters` é repassado direto, sem validação, e isso é deliberado, e
        não uma omissão. Esta camada detém a política sobre o que conta como
        um *pedido* utilizável -- uma consulta vazia, um tamanho de página
        absurdo -- e um dispositivo que não existe não é um deles: o filtro é
        uma restrição sobre o conjunto de candidatos, então nomear um
        dispositivo desconhecido é uma busca que legitimamente não casa com
        nada (RFC-027 §9). Checar também faria este caso de uso adquirir um
        `DeviceRepository` que, fora isso, não tem razão para ter, para
        rejeitar o único caso que já responde corretamente.

        A única exceção é o raio de um círculo (RFC-032 §6): um raio abaixo
        de `min_radius_m` *é* um pedido inutilizável, como um tamanho de
        página absurdo, e é recusado antes de a consulta ser codificada.
        """
        if not query.strip():
            raise EmptySearchQueryError(
                "Search query cannot be empty or only whitespace."
            )

        effective_limit = validated_request(
            limit, filters, self._default_limit, self._min_radius_m
        )
        embedding = self._embedding_model.encode_text(query)
        return self._repository.search_similar(embedding, effective_limit, filters)

    def execute_similar_to(
        self,
        image: Image,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        """Devolve as imagens indexadas mais parecidas com `image`, da mais parecida.

        A mesma busca com uma imagem no lugar das palavras: o CLIP põe
        imagens e texto num espaço só, então o embedding da própria imagem é
        ranqueado contra o índice exatamente como um embedding de texto. É a
        consulta mais forte sempre que o usuário *tem* uma imagem -- a foto de
        uma cópia impressa, uma cópia que alguém mandou -- porque nenhuma
        descrição carrega tanto da imagem quanto a própria imagem.

        `image` não precisa estar indexada; só precisa ser legível no seu
        `absolute_path`, que é onde o adaptador de embedding a abre --
        `query_image()` monta uma para um arquivo que não está. Vale a mesma
        política de pedido de `execute()` -- tamanho de página e o piso do
        raio de um círculo -- e a ordem é a do repositório, intocada. Se
        `image` *está* indexada, ela volta primeiro, com similaridade ~1: uma
        resposta honesta, não uma que esta camada esconde. Para uma foto que o
        índice já tem, `FindSimilarImagesUseCase` é a porta mais barata.
        """
        effective_limit = validated_request(
            limit, filters, self._default_limit, self._min_radius_m
        )
        embedding = self._embedding_model.encode_image(image)
        return self._repository.search_similar(embedding, effective_limit, filters)

    def count_hidden_by_unknown_date(self, filters: SearchFilters | None) -> int | None:
        """Quantas imagens buscáveis um intervalo de datas deixou de fora sem data.

        O RFC-028 §4.1 exige que a UI possa dizer isso: um resultado vazio
        para "2018" tem de ser distinguível de "a foto de 2018 está indexada,
        mas não tem EXIF", ou o produto repete a resposta errada e silenciosa
        que o RFC-028 §2.1 existe para eliminar.

        `None` -- e não 0 -- quando não há intervalo de datas, e nesse caso o
        repositório nem é consultado. As duas respostas significam coisas
        diferentes para um cliente: 0 diz "o filtro de data não escondeu
        nada", `None` diz "não havia filtro de data para esconder nada", e
        uma busca sem filtro não pode pagar uma segunda consulta para saber
        um número que ninguém pediu (RFC-028 §8).

        Uma chamada separada, e não um segundo valor de retorno de
        `execute()`: a contagem é uma segunda consulta com custo próprio,
        sobre um universo diferente do ranking -- a tabela inteira sob os
        filtros, e não a vizinhança que um índice aproximado explorou -- e a
        página que `execute()` devolve continua exatamente a mesma.
        """
        return count_hidden_by_unknown_date(self._repository, filters)

    def count_hidden_by_unknown_position(
        self, filters: SearchFilters | None
    ) -> int | None:
        """Quantas imagens buscáveis um círculo deixou de fora por não terem posição.

        O RFC-032 §6.2, no molde exato de `count_hidden_by_unknown_date()`.
        Sem ele, o filtro espacial responde *"não há foto deste lugar"* quando
        a verdade é *"1.200 fotos não têm coordenadas e nenhuma delas foi
        olhada"* -- e, com os ~4% das fotos de smartphone, toda exportação que
        remove o EXIF e tudo o que foi indexado antes do RFC-032, esse número
        começa grande.

        `None` -- e não 0 -- quando não há círculo, e aí **o repositório nem é
        chamado**. Essa regra mora aqui, e não no repositório, porque
        `count_unknown_position()` responde à mesma pergunta para o mapa sem
        círculo (RFC-032 §7) e lá não pode tomar o atalho.
        """
        return count_hidden_by_unknown_position(self._repository, filters)


class FindSimilarImagesUseCase:
    """Ranqueia o índice contra uma foto que ele já tem (RFC-038 §4.1).

    *"Parecidas com esta"*, a partir do detalhe de uma foto. **Sem modelo**: o
    vetor da foto é o que a indexação guardou, lido de volta com
    `get_embedding()` e entregue ao mesmo `search_similar()` que uma consulta
    de texto usa. Então funciona com o disco da foto na gaveta -- que é o caso
    para o qual o produto existe --, com a busca `loading` ou `unavailable`, e
    sem decodificar nada. `execute_similar_to()`, em vez disso, reabre o
    arquivo, e precisa do disco, do decodificador e de uma passada do CLIP para
    produzir este mesmo vetor; ele fica para o arquivo que *não* está
    indexado.

    `default_limit` e `min_radius_m` chegam como no `SearchImagesUseCase`, da
    composition root, e a política do pedido são as mesmas funções.
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
        image_id: ImageId,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        """As imagens mais parecidas com `image_id`, da mais parecida, sem ela.

        O pedido é checado primeiro, como `SearchImagesUseCase.execute()` o
        checa antes de codificar: uma página ou um círculo inutilizável não
        custa leitura.

        **A própria foto sai, e as cópias dela ficam.** Ela voltaria primeiro,
        com similaridade 1, e ocuparia um dos lugares da página com a foto que
        o usuário acabou de olhar. Uma cópia -- os mesmos bytes em outro disco
        ou em outra pasta -- tem o mesmo vetor e outro id, volta perto do
        topo, e responde *"onde mais está esta foto"*. Uma linha a mais é
        pedida, para que uma página sem a origem continue cheia; a ordem é a
        do repositório, intocada.

        Sem vetor, é `ImageNotFoundError` sem linha (404), e
        `ImageNotIndexedError` com uma (409): o segundo pedido dá certo assim
        que a foto for indexada.
        """
        effective_limit = validated_request(
            limit, filters, self._default_limit, self._min_radius_m
        )
        embedding = self._repository.get_embedding(image_id)
        if embedding is None:
            if not self._repository.exists(image_id):
                raise ImageNotFoundError(f"No indexed image has the id {image_id}.")
            raise ImageNotIndexedError(
                f"The image {image_id} has no embedding yet: index its folder "
                "to compare it with others."
            )
        hits = self._repository.search_similar(embedding, effective_limit + 1, filters)
        return [hit for hit in hits if hit.image.id != image_id][:effective_limit]

    def count_hidden_by_unknown_date(self, filters: SearchFilters | None) -> int | None:
        """`SearchImagesUseCase.count_hidden_by_unknown_date()`, a mesma função."""
        return count_hidden_by_unknown_date(self._repository, filters)

    def count_hidden_by_unknown_position(
        self, filters: SearchFilters | None
    ) -> int | None:
        """`SearchImagesUseCase.count_hidden_by_unknown_position()`, idem."""
        return count_hidden_by_unknown_position(self._repository, filters)


def query_image(path: Path) -> Image:
    """Embrulha um arquivo do disco como o `Image` transitório de uma consulta.

    O adaptador de embedding lê os pixels do `absolute_path` de um `Image`, e
    um `Image` precisa de um id e de um dispositivo. Este nunca é persistido:
    o dispositivo dele é o UUID nulo -- nenhum dispositivo -- e o id é
    derivado do caminho só para que duas execuções sobre o mesmo arquivo
    montem o mesmo valor.

    Aqui desde o RFC-038, fora de `app.cli.search`: o `--image` da CLI e o
    `POST /images/search/by-image` da API montam o mesmo valor, e uma segunda
    cópia seria uma segunda ideia do que é uma imagem de consulta.
    """
    resolved = path.resolve()
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, f"query-image:{resolved}")),
        device_id=DeviceId(uuid.UUID(int=0)),
        relative_path=ImagePath(resolved.name),
        filename=resolved.stem,
        extension=resolved.suffix.lower().lstrip("."),
        absolute_path=ImagePath(resolved),
    )


def validated_request(
    limit: int | None,
    filters: SearchFilters | None,
    default_limit: int,
    min_radius_m: float,
) -> int:
    """O tamanho de página a usar, depois das checagens comuns a toda consulta."""
    effective_limit = default_limit if limit is None else limit
    validate_limit(effective_limit)
    if filters is not None and filters.taken_within is not None:
        validate_radius(filters.taken_within, min_radius_m)
    return effective_limit


def validate_limit(limit: int) -> None:
    """Rejeita um tamanho de página inutilizável, na construção e na chamada.

    Aplicado ao padrão injetado tanto quanto ao valor de cada chamada, para
    que um `top_k_results` mal configurado falhe quando o caso de uso é
    montado, e não na primeira busca que omitir `limit`.
    """
    if limit <= 0:
        raise InvalidSearchLimitError(f"Search limit must be at least 1, got {limit}.")
    if limit > MAX_SEARCH_LIMIT:
        raise InvalidSearchLimitError(
            f"Search limit must be at most {MAX_SEARCH_LIMIT}, got {limit}."
        )


def validate_min_radius(min_radius_m: float) -> None:
    """Um piso negativo é erro de montagem, recusado ao construir o caso de uso."""
    if min_radius_m < 0:
        raise ValueError(f"min_radius_m must not be negative, got {min_radius_m}")


def validate_radius(circle: GeoCircle, min_radius_m: float) -> None:
    """Recusa um círculo menor do que o deslocamento da aeronave pode esconder.

    A mensagem explica o deslocamento, em vez de só citar um número, porque
    quem a lê escolheu o raio num mapa e precisa saber por que um raio de
    aparência precisa é o errado.
    """
    if circle.radius_m < min_radius_m:
        raise InvalidGeoCircleError(
            f"A search radius of {circle.radius_m:g} m is below the minimum "
            f"of {min_radius_m:g} m. Photo coordinates record where the "
            "drone was, not the ground it photographed, which for an oblique "
            "shot can be tens to hundreds of metres away -- a smaller circle "
            "would hide the very photo you are looking for."
        )


def count_hidden_by_unknown_date(
    repository: ImageRepository, filters: SearchFilters | None
) -> int | None:
    """Quantas imagens buscáveis um intervalo de datas deixou de fora sem data.

    `None`, e nenhuma consulta, sem intervalo de datas; ver
    `SearchImagesUseCase.count_hidden_by_unknown_date()`.
    """
    if filters is None or filters.captured_between is None:
        return None
    return repository.count_unknown_capture_date(filters)


def count_hidden_by_unknown_position(
    repository: ImageRepository, filters: SearchFilters | None
) -> int | None:
    """Quantas imagens buscáveis um círculo deixou de fora por não terem posição.

    `None`, e nenhuma consulta, sem círculo; ver
    `SearchImagesUseCase.count_hidden_by_unknown_position()`.
    """
    if filters is None or filters.taken_within is None:
        return None
    return repository.count_unknown_position(filters)
