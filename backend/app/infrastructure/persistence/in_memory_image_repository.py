"""Repositório de imagens em memória, para desenvolvimento e ligação de dependências."""

from __future__ import annotations

import dataclasses
import datetime
import math
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal

from app.domain.entities.image import Image
from app.domain.exceptions import EmbeddingDimensionMismatchError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.change_signals import ChangeSignals
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_presence import (
    ImagePresence,
    MissingImages,
    Presences,
    PurgedImages,
)
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.listing import (
    ListedImages,
    ListingOrder,
    ListingSummary,
)
from app.domain.value_objects.position import (
    BoundingBox,
    PositionCell,
    PositionCells,
    PositionReading,
)
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHit, SearchHits
from app.infrastructure.config.settings import settings


def cosine_search(
    query: EmbeddingVector,
    indexed: Iterable[tuple[Image, EmbeddingVector]],
    limit: int,
) -> SearchHits:
    """Ordena `indexed` contra `query` como o PostgreSQL faria, em Python.

    Compartilhada pelos dois doubles em processo de `ImageRepository` -- o
    deste módulo e o de `tests/application/fakes.py` --, em vez de escrita
    duas vezes. A única razão de eles implementarem busca é serem presos ao
    mesmo teste de contrato que o `PostgresImageRepository`, e dois loops de
    cosseno escritos à mão seriam duas chances de divergir dele e um do
    outro.

    Calcula de propósito o cosseno completo, dividindo pelas duas normas, em
    vez de um produto escalar puro. Quem chama guarda vetores normalizados
    em L2 hoje, porque o `ClipEmbeddingModel` normaliza, mas o `<=>` do
    pgvector não supõe isso, e um double que o exigisse em silêncio
    discordaria do PostgreSQL na primeira vez em que um teste usasse um
    vetor montado à mão com qualquer outra norma.

    O desempate é pelo id da imagem, como o ORDER BY de
    `PostgresImageRepository.search_similar()`: o PostgreSQL compara UUIDs
    byte a byte e o Python compara `UUID.int`, que é a mesma ordem sobre os
    mesmos 16 bytes.
    """
    _require_indexed_dimension(query)

    hits = [
        SearchHit(image=image, similarity=_cosine_similarity(query, embedding))
        for image, embedding in indexed
    ]
    hits.sort(key=lambda hit: (-hit.similarity, hit.image.id.value))
    return hits[:limit]


def matches_filters(image: Image, filters: SearchFilters | None) -> bool:
    """Diz se `image` passa por `filters`, para uma ordenação em processo.

    Compartilhada pelos dois doubles em processo de `ImageRepository` pela
    mesma razão que `cosine_search()`: um predicado escrito uma vez não pode
    divergir do outro, e os dois são presos ao contrato do PostgreSQL por
    `test_search_similar_contract.py`.

    `None` e um `SearchFilters()` vazio significam, os dois, "sem
    restrição", nunca "um conjunto vazio de dispositivos". Inverter isso
    faria um argumento com valor padrão devolver nada, em silêncio.

    Cada campo restringe por conta própria, e só quando preenchido, como
    cada um acrescenta sua própria cláusula `WHERE` no PostgreSQL.

    A verificação `captured_at is not None` é explícita de propósito, e é a
    linha com mais chance de ser "simplificada" e sumir. O PostgreSQL ganha
    a regra do RFC-020 de graça -- uma comparação com NULL nunca é
    verdadeira --, mas o Python não: `start <= None` levanta `TypeError` em
    vez de devolver `False`, então, sem a verificação, uma data
    desconhecida derrubaria a busca em memória onde o banco apenas exclui a
    linha, em silêncio.

    A verificação da posição é explícita pela mesma razão (RFC-032 §6):
    `haversine(None, ...)` levanta erro onde o SQL compara NULL como falso.
    **Uma posição desconhecida nunca casa com um círculo, por maior que
    seja.** O círculo é testado só pela distância exata -- não há aqui
    índice que uma caixa delimitadora ajudasse, e acrescentar uma seria só
    mais um lugar em que a resposta poderia diferir da exata.
    """
    if filters is None or filters.is_empty():
        return True
    if filters.device_ids and image.device_id not in filters.device_ids:
        return False
    if filters.captured_between is not None:
        if image.captured_at is None:
            return False
        if not filters.captured_between.contains(image.captured_at):
            return False
    if filters.taken_within is not None:
        position = image.position
        if position is None:
            return False
        if not filters.taken_within.contains(position):
            return False
    return True


def count_unknown_capture_date(indexed: Iterable[Image], filters: SearchFilters) -> int:
    """Conta o que um intervalo de datas escondeu por não ter data, entre as buscáveis.

    `indexed` já deve estar restrito às imagens com embedding, exatamente
    como `cosine_search()` espera. Todas as outras cláusulas -- o conjunto
    de dispositivos e, desde o RFC-032, o círculo -- são aplicadas por
    `matches_filters()` com o intervalo de datas removido, para que a
    definição de "todas as outras cláusulas" não possa divergir do
    predicado da busca.

    Compartilhada pelos dois doubles em processo pela razão de
    `matches_filters()`.
    """
    if filters.captured_between is None:
        return 0
    without_date = dataclasses.replace(filters, captured_between=None)
    return sum(
        1
        for image in indexed
        if image.captured_at is None and matches_filters(image, without_date)
    )


def count_unknown_position(indexed: Iterable[Image], filters: SearchFilters) -> int:
    """Conta as imagens buscáveis sem posição, sob todas as outras cláusulas.

    A gêmea de `count_unknown_capture_date()` para o RFC-032 §6.2, com a
    única diferença que a porta documenta: nada de devolver 0 direto sem um
    círculo, porque o mapa faz a mesma pergunta sem nenhum. O próprio
    círculo é removido antes de `matches_filters()` -- é a cláusula cujas
    vítimas estão sendo contadas.
    """
    without_circle = dataclasses.replace(filters, taken_within=None)
    return sum(
        1
        for image in indexed
        if image.latitude is None and matches_filters(image, without_circle)
    )


def round_to_cell(value: float, precision: int) -> float:
    """Arredonda uma coordenada como `round(value::numeric, precision)` no PostgreSQL.

    Não o `round()` do Python, que arredonda o float *binário* metade para o
    par: `round(2.675, 2)` dá 2.67 porque 2.675 é guardado como
    2.67499999... O PostgreSQL converte um `double precision` em `numeric`
    pelos seus 15 dígitos decimais significativos e então arredonda esse
    decimal metade para longe do zero. Uma foto exatamente na borda de uma
    célula precisa cair na mesma célula nas três implementações, então isto
    reproduz a conversão e a regra em vez de aproximá-las.
    """
    decimal = Decimal(format(value, ".15g"))
    step = Decimal(1).scaleb(-precision)
    return float(decimal.quantize(step, rounding=ROUND_HALF_UP))


def aggregate_positions(
    indexed: Iterable[Image],
    filters: SearchFilters,
    area: BoundingBox,
    precision: int,
    cell_limit: int | None,
) -> PositionCells:
    """Agrupa em células as imagens buscáveis de `area`, como faz o `GROUP BY`.

    `indexed` já deve estar restrito às imagens com embedding. As cláusulas
    de dispositivo e de data vêm de `matches_filters()` com o círculo
    removido, e a área é testada com as bordas incluídas, para que isto
    concorde com os `BETWEEN`s do PostgreSQL. Ordenadas por latitude e
    depois longitude, e cortadas em `cell_limit`, para o mesmo truncamento
    determinístico.

    Compartilhada pelos dois doubles em processo pela razão de
    `matches_filters()`: `test_position_filter_contract.py` prende as três
    implementações a uma só resposta.
    """
    universe = dataclasses.replace(filters, taken_within=None)
    counts: dict[tuple[float, float], int] = {}
    for image in indexed:
        position = image.position
        if position is None or not area.contains(position):
            continue
        if not matches_filters(image, universe):
            continue
        key = (
            round_to_cell(position.latitude, precision),
            round_to_cell(position.longitude, precision),
        )
        counts[key] = counts.get(key, 0) + 1

    cells = [
        PositionCell(latitude=latitude, longitude=longitude, count=count)
        for (latitude, longitude), count in sorted(counts.items())
    ]
    return cells if cell_limit is None else cells[:cell_limit]


_ONE_MICROSECOND = datetime.timedelta(microseconds=1)


def listing_key(image: Image, order: ListingOrder) -> tuple[int, int, str, int]:
    """A chave de ordenação do RFC-044 §5.2, como uma tupla comparável.

    `(undated, date, path, id)`: as sem data depois de toda imagem datada,
    nas duas direções; a data na direção de `order`; depois o caminho e o
    id, sempre ascendentes. A data é em microssegundos inteiros desde
    `datetime.min` -- um inteiro, então negá-lo para `NEWEST` é exato, onde
    um float de segundos arredondaria e perderia os microssegundos de uma
    data do ano 9999.

    O caminho é comparado como `str`, por code point, que é a ordem do
    `COLLATE "C"` pela qual a implementação do PostgreSQL ordena; o id, como
    `UUID.int`, que é a ordem de bytes do PostgreSQL sobre os mesmos 16
    bytes.
    """
    path = str(image.relative_path)
    identity = image.id.value.int
    if image.captured_at is None:
        return (1, 0, path, identity)
    micros = (image.captured_at - datetime.datetime.min) // _ONE_MICROSECOND
    return (0, -micros if order is ListingOrder.NEWEST else micros, path, identity)


def list_by_filters(
    indexed: Iterable[Image],
    filters: SearchFilters,
    order: ListingOrder,
    limit: int,
    after: Image | None,
) -> ListedImages:
    """Uma página da listagem, como o `ORDER BY` e a cláusula do cursor a dão.

    `indexed` já deve estar restrito às imagens com embedding. Os filtros
    são `matches_filters()`, o próprio predicado da busca; o cursor é "toda
    chave depois da de `after`", com a chave de `after` calculada pela mesma
    função -- então a ordem e o cursor não podem discordar, que é a
    propriedade que o teste de contrato percorre, página a página.

    Compartilhada pelos dois doubles em processo pela razão de
    `matches_filters()`.
    """
    start = None if after is None else listing_key(after, order)
    page = sorted(
        (
            image
            for image in indexed
            if matches_filters(image, filters)
            and (start is None or listing_key(image, order) > start)
        ),
        key=lambda image: listing_key(image, order),
    )
    return page[:limit]


def summarize_listing(
    indexed: Iterable[Image], filters: SearchFilters
) -> ListingSummary:
    """`count(*)`, as sem data, o `min` e o `max` da data, sobre a listagem."""
    listed = [image for image in indexed if matches_filters(image, filters)]
    dates = [image.captured_at for image in listed if image.captured_at is not None]
    return ListingSummary(
        total=len(listed),
        undated=len(listed) - len(dates),
        oldest=min(dates, default=None),
        newest=max(dates, default=None),
    )


def count_images_by_device(images: Iterable[Image]) -> dict[DeviceId, int]:
    """Agrupa as imagens por dispositivo, como faz um `GROUP BY device_id`.

    Compartilhada pelos dois doubles em processo pela razão de
    `cosine_search()` ser compartilhada: `test_image_counting_contract.py`
    prende as três implementações a uma só resposta, e duas contagens
    escritas à mão seriam duas chances de divergir do PostgreSQL e uma da
    outra.

    Dispositivos sem imagens simplesmente não aparecem, porque um `GROUP BY`
    também não produz linha para eles (RFC-031 §4.3).
    """
    counts: dict[DeviceId, int] = {}
    for image in images:
        counts[image.device_id] = counts.get(image.device_id, 0) + 1
    return counts


def change_signals_of(
    images: Iterable[Image],
    metadata: Mapping[uuid.UUID, IndexMetadata],
    device_id: DeviceId,
) -> dict[ImageId, ChangeSignals]:
    """O tamanho e o mtime guardados de cada imagem de um dispositivo (RFC-045 §5.2).

    Compartilhada pelos dois doubles em processo, pela razão das contagens
    acima: `test_change_signals_contract.py` prende eles e o PostgreSQL a
    uma só resposta. Uma linha ainda sem metadados dá dois `None`s, como as
    colunas seriam lidas de volta.
    """
    signals: dict[ImageId, ChangeSignals] = {}
    for image in images:
        if image.device_id != device_id:
            continue
        stored = metadata.get(image.id.value)
        signals[image.id] = ChangeSignals(
            file_size=stored.file_size if stored is not None else None,
            file_modified_at=(stored.file_modified_at if stored is not None else None),
        )
    return signals


def visible_images(
    images: Iterable[Image], marks: Mapping[uuid.UUID, datetime.datetime]
) -> list[Image]:
    """As imagens que nenhum scan marcou como ausentes (RFC-046 §6).

    Compartilhada pelos dois doubles em processo, para que o que eles
    escondem não possa divergir de um para o outro;
    `test_missing_images_contract.py` prende eles e o PostgreSQL a uma só
    resposta.
    """
    return [image for image in images if image.id.value not in marks]


def presence_of(
    images: Iterable[Image],
    marks: Mapping[uuid.UUID, datetime.datetime],
    device_id: DeviceId,
) -> Presences:
    """`list_presence()` sobre o armazenamento dos doubles: toda imagem do dispositivo.

    Tirada inteira antes de ser entregue, para que quem chama, se escrevesse
    enquanto itera -- o que a varredura não faz --, não visse as próprias
    escritas.
    """
    return iter(
        [
            ImagePresence(
                image_id=image.id,
                relative_path=str(image.relative_path),
                missing=image.id.value in marks,
            )
            for image in images
            if image.device_id == device_id
        ]
    )


def marks_after(
    marks: Mapping[uuid.UUID, datetime.datetime],
    images: Iterable[Image],
    missing: Collection[ImageId],
    present: Collection[ImageId],
    since: datetime.datetime,
) -> dict[uuid.UUID, datetime.datetime]:
    """As marcas depois de aplicado o `mark_presence()`, como um mapeamento novo.

    Um mapeamento novo, e não uma edição no lugar, para que uma recusa deixe
    o double como estava -- a transação que a implementação do PostgreSQL
    desfaz. Uma imagem já marcada mantém a data; um id sem imagem é pulado,
    como o `UPDATE` o pula.
    """
    missing_ids = {image_id.value for image_id in missing}
    present_ids = {image_id.value for image_id in present}
    if missing_ids & present_ids:
        raise ValueError("An image cannot be both missing and present")
    known = {image.id.value for image in images}
    updated = dict(marks)
    for value in missing_ids & known:
        updated.setdefault(value, since)
    for value in present_ids:
        updated.pop(value, None)
    return updated


def count_missing_images(
    images: Iterable[Image], marks: Mapping[uuid.UUID, datetime.datetime]
) -> dict[DeviceId, MissingImages]:
    """`count_missing_by_device()`: marcadas por dispositivo, e a marca mais antiga."""
    tallies: dict[DeviceId, MissingImages] = {}
    for image in images:
        since = marks.get(image.id.value)
        if since is None:
            continue
        current = tallies.get(image.device_id)
        tallies[image.device_id] = (
            MissingImages(count=1, since=since)
            if current is None
            else MissingImages(count=current.count + 1, since=min(current.since, since))
        )
    return tallies


def marked_on_device(
    images: Iterable[Image],
    marks: Mapping[uuid.UUID, datetime.datetime],
    device_id: DeviceId,
) -> set[uuid.UUID]:
    """Os ids que `purge_missing()` apaga: as imagens marcadas de um dispositivo."""
    return {
        image.id.value
        for image in images
        if image.device_id == device_id and image.id.value in marks
    }


def count_images_by_path_prefix(
    images: Iterable[Image], device_id: DeviceId, parent: JobScope
) -> dict[str, int]:
    """Agrupa as imagens de um dispositivo pela primeira parte do caminho sob `parent`.

    A gêmea em processo do agrupamento por `split_part(substr(...))` do
    `PostgresImageRepository`, e precisa concordar com ele nos dois casos de
    borda que a docstring da porta nomeia -- uma imagem direto em `parent`,
    que cai sob o próprio nome de arquivo, e uma pasta renomeada no disco,
    que cai sob o nome que as linhas ainda carregam. Os dois são descartados
    por quem chama, e não aqui.

    O separador acrescentado ao prefixo é todo o argumento de correção: sem
    ele, `2018` casaria com `2018b`, que é outra pasta (RFC-029 §10). O
    escopo do dispositivo inteiro é renderizado como `""` e, portanto, não
    acrescenta nada, o que está certo -- todo caminho está abaixo da raiz
    do dispositivo.
    """
    lead = f"{parent}/" if parent.parts else ""
    counts: dict[str, int] = {}
    for image in images:
        if image.device_id != device_id:
            continue
        path = str(image.relative_path)
        if not path.startswith(lead):
            continue
        head = path[len(lead) :].split("/", 1)[0]
        if not head:
            continue
        counts[head] = counts.get(head, 0) + 1
    return counts


def store_thumbnail(thumbnails: dict[uuid.UUID, str], record: IndexingRecord) -> None:
    """Aplica o local da thumbnail de um registro como `save_indexed()` o escreve.

    `None` remove o local guardado em vez de mantê-lo, porque o registro
    traz bytes novos e uma thumbnail dos antigos não retrata mais o arquivo
    -- a implementação do PostgreSQL escreve o NULL. Compartilhada pelos
    dois doubles em processo, para que não possam discordar nisso.
    """
    if record.thumbnail_path is None:
        thumbnails.pop(record.image.id.value, None)
    else:
        thumbnails[record.image.id.value] = record.thumbnail_path


def with_capture_date(image: Image, capture: CaptureDate) -> Image:
    """Devolve `image` com `capture`, para os dois campos da entidade congelada."""
    return dataclasses.replace(
        image, captured_at=capture.captured_at, capture_source=capture.source
    )


def with_position(image: Image, reading: PositionReading) -> Image:
    """Devolve `image` com `reading`, para os três campos da entidade congelada.

    Os três num só `replace()`, como o `UPDATE` do PostgreSQL os escreve num
    só comando: `Image` valida o par na construção, então um double que os
    preenchesse um de cada vez recusaria uma escrita válida.
    """
    return dataclasses.replace(
        image,
        latitude=reading.latitude,
        longitude=reading.longitude,
        position_source=reading.source,
    )


def _require_indexed_dimension(query: EmbeddingVector) -> None:
    """Recusa um vetor de consulta que não pode ser comparado com os guardados.

    Verificado contra a dimensão configurada, e não contra o que estiver
    guardado, para que o erro não dependa de o repositório ter algo: o
    PostgreSQL recusa um vetor de dimensão errada na coluna `vector(512)`
    exista ou não alguma linha, e um double que respondesse `[]` com o
    armazenamento vazio seria o mais permissivo dos dois justamente onde um
    teste tem menos chance de notar.
    """
    if len(query.values) != settings.embedding_dimension:
        raise EmbeddingDimensionMismatchError(
            f"Query embedding has {len(query.values)} dimensions, but the "
            f"index holds {settings.embedding_dimension}."
        )


def _cosine_similarity(left: EmbeddingVector, right: EmbeddingVector) -> float:
    """Devolve a similaridade de cosseno em [-1, 1], nunca uma aproximação truncada.

    A verificação do comprimento é a razão de isto ser uma função, e não uma
    expressão inline. `zip` para no operando mais curto, então comparar um
    vetor de 3 dimensões com um de 512 devolveria, sem ela, um número
    plausível calculado com três dimensões -- uma resposta errada idêntica a
    uma certa. `save_indexed_many()`, abaixo, documenta o mesmo tipo de
    risco para a atomicidade: um double não pode ser, em silêncio, mais
    tolerante que o banco de que faz as vezes.
    """
    if len(left.values) != len(right.values):
        raise EmbeddingDimensionMismatchError(
            f"Cannot compare a {len(left.values)}-dimensional vector with a "
            f"{len(right.values)}-dimensional one."
        )

    dot = sum(x * y for x, y in zip(left.values, right.values))
    left_norm = math.sqrt(sum(x * x for x in left.values))
    right_norm = math.sqrt(sum(y * y for y in right.values))
    return dot / (left_norm * right_norm)


class InMemoryImageRepository(ImageRepository):
    """Implementação do repositório sobre uma lista em memória."""

    def __init__(self) -> None:
        self._images: list[Image] = []
        self._metadata: dict[uuid.UUID, IndexMetadata] = {}
        # Separado de `_images` porque nem toda imagem tem um: `save()` cria
        # uma linha sem embedding, exatamente como a coluna do PostgreSQL
        # aceita nulo, e a busca precisa pular essas.
        self._embeddings: dict[uuid.UUID, EmbeddingVector] = {}
        # Separado de `_metadata` também, embora volte dentro de um
        # `IndexMetadata`: `update_index_metadata()` troca essa entrada
        # inteira pelo que quem chama montou, e quem chama para atualizar os
        # sinais de mudança nunca conhece a thumbnail. Guardada em
        # `_metadata`, uma atualização a apagaria; o `UPDATE` do PostgreSQL,
        # não.
        self._thumbnails: dict[uuid.UUID, str] = {}
        # `images.missing_since` (RFC-046): ausente da chave é presente.
        self._missing: dict[uuid.UUID, datetime.datetime] = {}

    def save(self, image: Image) -> None:
        self._images.append(image)

    def get(self, image_id: ImageId) -> Image | None:
        for image in self._images:
            if image.id == image_id:
                return image
        return None

    def exists(self, image_id: ImageId) -> bool:
        return any(image.id == image_id for image in self._images)

    def delete(self, image_id: ImageId) -> None:
        self._images = [image for image in self._images if image.id != image_id]
        self._metadata.pop(image_id.value, None)
        self._embeddings.pop(image_id.value, None)
        self._thumbnails.pop(image_id.value, None)
        self._missing.pop(image_id.value, None)

    def list(self) -> list[Image]:
        return list(self._images)

    def save_indexed(self, record: IndexingRecord) -> None:
        """Guarda a imagem, os metadados e -- desde o RFC-025 -- o embedding.

        O embedding era descartado aqui, o que não fazia mal enquanto nada
        lia um de volta e virou mentira no momento em que `search_similar()`
        passou a existir: um repositório que aceita um vetor e depois não
        acha nada não faz as vezes de um que acha.
        """
        self._images = [image for image in self._images if image.id != record.image.id]
        self._images.append(record.image)
        self._metadata[record.image.id.value] = IndexMetadata(
            file_size=record.file_size,
            file_modified_at=record.file_modified_at,
            content_hash=record.content_hash,
        )
        self._embeddings[record.image.id.value] = record.embedding
        store_thumbnail(self._thumbnails, record)
        self._missing.pop(record.image.id.value, None)

    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        """Persiste todos os registros, ou nenhum.

        Um dict e uma lista não têm transação para fazer commit, então a
        atomicidade tem de ser arranjada à mão: as escritas são aplicadas a
        cópias, que só entram no lugar depois que todas deram certo. Pular
        isso faria esta implementação, em silêncio, mais tolerante que a
        real, e o fallback linha a linha que ela existe para disparar ficaria
        sem teste em todo lugar, menos contra o PostgreSQL.
        """
        images = list(self._images)
        metadata = dict(self._metadata)
        embeddings = dict(self._embeddings)
        thumbnails = dict(self._thumbnails)
        missing = dict(self._missing)

        for record in records:
            images = [image for image in images if image.id != record.image.id]
            images.append(record.image)
            metadata[record.image.id.value] = IndexMetadata(
                file_size=record.file_size,
                file_modified_at=record.file_modified_at,
                content_hash=record.content_hash,
            )
            embeddings[record.image.id.value] = record.embedding
            store_thumbnail(thumbnails, record)
            missing.pop(record.image.id.value, None)

        self._images = images
        self._metadata = metadata
        self._embeddings = embeddings
        self._thumbnails = thumbnails
        self._missing = missing

    def search_similar(
        self,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> SearchHits:
        """Ordena cada embedding guardado contra `embedding`, em Python.

        Pontuar o armazenamento inteiro a cada chamada serve aqui e não
        serviria em produção: esta implementação existe para testes e para
        montar uma aplicação antes de haver banco, quando o armazenamento
        tem dezenas de imagens. O `PostgresImageRepository` é o caminho
        real, e ordena no banco justamente para que 100.000 vetores nunca
        entrem neste processo.

        Os filtros são aplicados *antes* da ordenação, como uma cláusula
        `WHERE`, e não como um corte do top-K depois. Filtrar depois
        devolveria menos de `limit` linhas sempre que as imagens excluídas
        ficassem bem colocadas -- que é exatamente o risco de recall do HNSW
        que o RFC-027 §9.1 descreve, e um double de teste não pode
        reproduzir um artefato do índice físico como se fosse contrato.
        """
        return cosine_search(
            embedding,
            (
                (image, self._embeddings[image.id.value])
                for image in self._indexed()
                if matches_filters(image, filters)
            ),
            limit,
        )

    def count_unknown_capture_date(self, filters: SearchFilters) -> int:
        return count_unknown_capture_date(self._indexed(), filters)

    def count_unknown_position(self, filters: SearchFilters) -> int:
        return count_unknown_position(self._indexed(), filters)

    def aggregate_positions(
        self,
        filters: SearchFilters,
        area: BoundingBox,
        precision: int,
        cell_limit: int | None,
    ) -> PositionCells:
        return aggregate_positions(
            self._indexed(), filters, area, precision, cell_limit
        )

    def list_by_filters(
        self,
        filters: SearchFilters,
        order: ListingOrder,
        limit: int,
        after: Image | None,
    ) -> ListedImages:
        return list_by_filters(self._indexed(), filters, order, limit, after)

    def summarize_listing(self, filters: SearchFilters) -> ListingSummary:
        return summarize_listing(self._indexed(), filters)

    def _indexed(self) -> Sequence[Image]:
        """O que uma busca poderia devolver: com embedding e sem marca (RFC-046)."""
        return [
            image
            for image in visible_images(self._images, self._missing)
            if image.id.value in self._embeddings
        ]

    def get_embedding(self, image_id: ImageId) -> EmbeddingVector | None:
        return self._embeddings.get(image_id.value)

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        """Devolve os sinais de mudança guardados mais a origem da data de captura.

        A origem da data de captura é lida do `Image` guardado, e não mantida
        também em `_metadata`. A entidade é onde a data de captura mora nesta
        implementação; uma segunda cópia aqui estaria livre para discordar
        dela -- por exemplo, depois que `update_index_metadata()` guardasse
        um `IndexMetadata` de quem chama que nunca teve origem nenhuma. A
        origem da posição (RFC-032) é lida da entidade pela mesma razão.
        """
        image = self.get(image_id)
        if image is None:
            return None
        stored = self._metadata.get(
            image_id.value, IndexMetadata(file_size=None, file_modified_at=None)
        )
        return dataclasses.replace(
            stored,
            capture_source=image.capture_source,
            position_source=image.position_source,
            thumbnail_path=self._thumbnails.get(image_id.value),
        )

    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        """Devolve os metadados de cada id fornecido que tenha linha.

        Composto a partir de `get_index_metadata()`, e não reimplementado,
        para que os dois nunca possam discordar. Não há o que agrupar em lote
        numa consulta a um dict: o método em lote existe para reduzir idas e
        voltas *de rede*, que esta implementação não faz.
        """
        found = {}
        for image_id in image_ids:
            metadata = self.get_index_metadata(image_id)
            if metadata is not None:
                found[image_id] = metadata
        return found

    def list_change_signals(self, device_id: DeviceId) -> dict[ImageId, ChangeSignals]:
        return change_signals_of(self._images, self._metadata, device_id)

    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        if not self.exists(image_id):
            return
        self._metadata[image_id.value] = metadata

    def update_capture_date(self, image_id: ImageId, capture: CaptureDate) -> None:
        self.update_capture_date_many({image_id: capture})

    def update_capture_date_many(self, captures: Mapping[ImageId, CaptureDate]) -> None:
        """Troca cada imagem nomeada por uma cópia com a sua data de captura.

        `Image` é congelada, então "atualizar uma coluna" é trocar a entidade
        por um `dataclasses.replace()` dela. Ids sem imagem são pulados, como
        são pelo `UPDATE` do PostgreSQL.
        """
        self._images = [
            (
                with_capture_date(image, captures[image.id])
                if image.id in captures
                else image
            )
            for image in self._images
        ]

    def update_position(self, image_id: ImageId, reading: PositionReading) -> None:
        self.update_position_many({image_id: reading})

    def update_position_many(self, readings: Mapping[ImageId, PositionReading]) -> None:
        """Troca cada imagem nomeada por uma cópia com a sua posição; pula o resto."""
        self._images = [
            with_position(image, readings[image.id]) if image.id in readings else image
            for image in self._images
        ]

    def update_thumbnail_path(self, image_id: ImageId, location: str) -> None:
        self.update_thumbnail_path_many({image_id: location})

    def update_thumbnail_path_many(self, locations: Mapping[ImageId, str]) -> None:
        """Registra cada local cuja imagem existe; pula o resto."""
        known = {image.id for image in self._images}
        for image_id, location in locations.items():
            if image_id in known:
                self._thumbnails[image_id.value] = location

    def list_presence(self, device_id: DeviceId) -> Presences:
        return presence_of(self._images, self._missing, device_id)

    def mark_presence(
        self,
        missing: Collection[ImageId],
        present: Collection[ImageId],
        since: datetime.datetime,
    ) -> None:
        self._missing = marks_after(
            self._missing, self._images, missing, present, since
        )

    def count_missing_by_device(self) -> dict[DeviceId, MissingImages]:
        return count_missing_images(self._images, self._missing)

    def purge_missing(self, device_id: DeviceId) -> PurgedImages:
        doomed = marked_on_device(self._images, self._missing, device_id)
        thumbnails = tuple(
            self._thumbnails[value] for value in doomed if value in self._thumbnails
        )
        self._images = [image for image in self._images if image.id.value not in doomed]
        for value in doomed:
            self._metadata.pop(value, None)
            self._embeddings.pop(value, None)
            self._thumbnails.pop(value, None)
            self._missing.pop(value, None)
        return PurgedImages(count=len(doomed), thumbnail_paths=thumbnails)

    def count_by_device(self) -> dict[DeviceId, int]:
        return count_images_by_device(visible_images(self._images, self._missing))

    def count_by_path_prefixes(
        self, device_id: DeviceId, parent: JobScope
    ) -> dict[str, int]:
        return count_images_by_path_prefix(
            visible_images(self._images, self._missing), device_id, parent
        )
