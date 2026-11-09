"""Implementação em PostgreSQL da porta do repositório de imagens."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Collection, Iterator, Mapping, Sequence
from typing import Any, cast

from sqlalchemy import (
    ColumnElement,
    Double,
    Integer,
    Numeric,
    Select,
    Table,
    and_,
    bindparam,
    case,
    delete,
    func,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.entities.image import Image
from app.domain.exceptions import (
    EmbeddingDimensionMismatchError,
    ImageAlreadyExistsError,
)
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.haversine import EARTH_RADIUS_M
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.change_signals import ChangeSignals
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
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
from app.domain.value_objects.position_source import PositionSource
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHit, SearchHits
from app.infrastructure.database.models.image_model import (
    EMBEDDING_DIMENSION,
    ImageModel,
)

PRESENCE_CHUNK = 10_000
"""Quantos ids um `UPDATE` de `mark_presence()` nomeia no máximo, e quantas
linhas `list_presence()` lê por fetch.

Bem abaixo dos 65.535 parâmetros que um comando pode levar, e grande o bastante
para que um disco de 116.396 linhas que perdeu todas sejam doze comandos.
"""

PGVECTOR_DEFAULT_EF_SEARCH = 40
"""`hnsw.ef_search` quando ninguém o define: o máximo de linhas de um scan HNSW.

Do README do pgvector: *"Results are limited by the size of the dynamic
candidate list (`hnsw.ef_search`), which is 40 by default"* (RFC-038 §2.2).
"""


class PostgresImageRepository(ImageRepository):
    """Implementação do repositório sobre o PostgreSQL, via SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, image: Image) -> None:
        """Persiste uma imagem, traduzindo colisões de localização num erro de domínio.

        A colisão que ele nomeia agora é `(device_id, relative_path)`, e não
        mais o `path` absoluto antigo, que é a constraint que enfim significa
        o que este método sempre afirmou: duas linhas para o mesmo arquivo no
        mesmo disco são recusadas, inclusive quando o disco foi montado com
        outra letra da segunda vez (RFC-027 §2.1).
        """
        model = ImageModel.from_domain(image)
        self._session.add(model)
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise ImageAlreadyExistsError() from exc

    def get(self, image_id: ImageId) -> Image | None:
        model = self._session.get(ImageModel, image_id.value)
        return model.to_domain() if model is not None else None

    def exists(self, image_id: ImageId) -> bool:
        statement = select(ImageModel.id).where(ImageModel.id == image_id.value)
        return self._session.execute(statement).scalar_one_or_none() is not None

    def delete(self, image_id: ImageId) -> None:
        model = self._session.get(ImageModel, image_id.value)
        if model is not None:
            self._session.delete(model)
            self._session.commit()

    def list(self) -> list[Image]:
        statement = select(ImageModel)
        models = self._session.execute(statement).scalars().all()
        return [model.to_domain() for model in models]

    def save_indexed(self, record: IndexingRecord) -> None:
        """Cria ou atualiza a linha de uma imagem junto com o embedding e os metadados.

        Ao contrário de `save()`, este é um upsert de verdade e não traduz
        `IntegrityError` em `ImageAlreadyExistsError` -- uma violação de
        constraint aqui (por exemplo, o CHECK de `file_size`) é um erro real,
        não um sinal de criação duplicada.

        O rollback na falha sustenta o resto; não é arrumação defensiva. Todo
        chamador deste método indexa muitos arquivos em sequência e isola as
        falhas por arquivo, então a sessão tem de sobreviver a uma linha
        recusada: sem o rollback, o SQLAlchemy deixa a transação num estado
        de rollback pendente e o *próximo* arquivo falha com
        `PendingRollbackError` em vez de dar certo, transformando uma linha
        ruim numa cascata. O fallback linha a linha do RFC-024 (§7.2) percorre
        exatamente esse caminho na mesma sessão.
        """
        try:
            self._stage_indexed(record)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        """Faz o upsert de muitas linhas com uma leitura, um flush e um commit.

        O commit é a parte barata, o oposto do que o RFC-024 supunha de
        início: o profiling de uma execução real mostrou um único commit de 45
        linhas custando 2,5 ms, contra ~48 ms por linha para todo o resto. O
        custo estava na metade da *leitura* -- veja o comentário abaixo --,
        então este método junta as leituras, em vez de só compartilhar um
        commit.

        Na falha, a sessão sofre rollback antes de o erro se propagar, para
        que a nova tentativa linha a linha de quem chama comece de uma sessão
        limpa, e não de uma envenenada por um lote aplicado pela metade.
        """
        if not records:
            return

        try:
            # Dois detalhes fazem disto uma escrita em massa, e não um loop
            # que apenas compartilha um commit, e os dois foram achados por
            # profiling (RFC-024 §7.2):
            #
            # `session.get()` por registro emitiria um SELECT por linha -- e,
            # pior, cada um desses SELECTs faz autoflush das linhas preparadas
            # até ali, então o ORM emite de todo jeito uma ida e volta de
            # INSERT por registro. Uma consulta `IN (...)` no começo elimina
            # os dois.
            #
            # `no_autoflush` então impede o loop de preparação de fazer flush
            # no meio por qualquer outro motivo, e o lote chega ao banco como
            # um flush seguido de um commit.
            with self._session.no_autoflush:
                staged = self._existing_models(
                    [record.image.id.value for record in records]
                )
                for record in records:
                    staged[record.image.id.value] = self._stage_indexed(
                        record, staged.get(record.image.id.value)
                    )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def _existing_models(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ImageModel]:
        """Carrega, numa consulta, os `ids` que já tiverem linha."""
        statement = select(ImageModel).where(ImageModel.id.in_(ids))
        return {
            model.id: model
            for model in self._session.execute(statement).scalars().all()
        }

    def _stage_indexed(
        self, record: IndexingRecord, model: ImageModel | None = None
    ) -> ImageModel:
        """Aplica um registro à sessão sem fazer commit.

        `model` é a linha já carregada, quando quem chama tem uma; passar
        `None` significa "busque-a", que é o que o caminho de um registro só
        faz.
        """
        if model is None:
            model = self._session.get(ImageModel, record.image.id.value)

        if model is None:
            model = ImageModel.from_domain(record.image)
            self._session.add(model)
        else:
            model.device_id = record.image.device_id.value
            model.relative_path = str(record.image.relative_path)
            model.filename = record.image.filename
            model.extension = record.image.extension
            model.captured_at = record.image.captured_at
            model.capture_source = _source_value(record.image.capture_source)
            model.latitude = record.image.latitude
            model.longitude = record.image.longitude
            model.position_source = _position_value(record.image.position_source)

        model.embedding = list(record.embedding.values)
        model.file_size = record.file_size
        model.file_modified_at = record.file_modified_at
        model.content_hash = record.content_hash
        model.thumbnail_path = record.thumbnail_path
        # Uma foto que acabou de ser indexada existe (RFC-046 §5.1).
        model.missing_since = None
        return model

    def search_similar(
        self,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> SearchHits:
        """Ordena as imagens indexadas contra `embedding` dentro do PostgreSQL.

        A ordenação é trabalho do banco e fica lá. Ler os vetores de volta
        para ordená-los no Python moveria 100.000 x 512 floats pela rede para
        responder a uma pergunta que o servidor responde com o índice HNSW
        que a migration do RFC-023 já construiu -- uns 200 MB por busca,
        para devolver dez linhas.

        Três detalhes sustentam o resto:

        `<=>` (`cosine_distance`) é o operador para o qual o índice foi
        criado (`vector_cosine_ops`). Ordenar por qualquer outra coisa -- L2,
        produto interno, ou uma expressão do lado do Python -- abre mão do
        índice em silêncio e muda a ordenação.

        A conversão de distância em similaridade acontece aqui, na borda. O
        pgvector devolve a *distância* de cosseno em [0, 2]; `1 - distance` é
        a similaridade de cosseno em [-1, 1]. Ela não é limitada nem
        reescalada: uma pontuação negativa significa que os vetores de fato
        se opõem, e nada acima desta camada deveria precisar saber que uma
        distância esteve envolvida.

        O id é o desempate. Sem ele, linhas igualmente distantes voltam na
        ordem que o plano produzir, que não é estável entre planos nem na
        fronteira entre seq scan e index scan, e o `limit` então cortaria uma
        delas, arbitrária.

        Só são selecionadas as colunas de que a entidade de domínio precisa.
        A coluna `embedding` fica de fora de propósito: quem chama não tem
        uso para ela, e hidratar linhas inteiras arrastaria de volta um vetor
        de 512 floats por resultado, à toa. `absolute_path` fica sem valor em
        todo resultado, porque o banco não sabe onde um dispositivo está
        montado e não pode fingir que sabe (RFC-027 §7).

        **O filtro de dispositivo é um `WHERE`, e o que o PostgreSQL faz com
        ele é uma medição, e não uma dedução.** O planner é livre para
        abandonar o índice HNSW e varrer o subconjunto sequencialmente, o que
        é rápido e exato para um filtro seletivo; ou para manter o índice e
        aplicar o predicado ao que a travessia devolveu, o que sai quase de
        graça para um pouco seletivo. O meio é o risco: um index scan explora
        no máximo `hnsw.ef_search` candidatos, então o pós-filtro pode
        descartar o bastante deles para devolver **menos que `limit`**
        linhas enquanto existem mais linhas que casam. Mitigações conhecidas,
        nenhuma escolhida aqui: `hnsw.iterative_scan`, aumentar `ef_search`
        quando há filtro, ou índices HNSW parciais por dispositivo.
        `experiments/rfc-027-devices/planner_check.py` mede os três regimes
        com `EXPLAIN ANALYZE`.

        **Um intervalo de data de captura é o mais difícil dos dois filtros**
        (RFC-028 §8.1). A seletividade dele é dada pelo intervalo que o
        usuário pedir, e não conhecida de antemão, e não há um conjunto
        finito de intervalos para construir um índice parcial para cada, então
        a mitigação que serve para dispositivos não existe para datas.
        `experiments/rfc-028-capture-date/planner_check.py` mede isso.

        **Um círculo é o pior dos três para o índice aproximado** (RFC-032
        §6.1): cardinalidade alta, como uma data, e *concentrado* -- no
        piloto, 5 células de ~100 m tinham 45% das posições --, então alguns
        quilômetros em volta de um lugar muito fotografado não são nada
        seletivos, e caem exatamente na faixa em que o RFC-028 mediu a busca
        devolvendo de 5 a 9 linhas de 10.
        `experiments/rfc-032-geolocation/planner_check.py` mede isso num
        corpus concentrado, e o RFC-032 §6.1 decidiu a mitigação contra um
        critério declarado antes do número: a configuração mais barata que
        devolvesse `limit` de `limit` em todo caso, com um p95 filtrado no
        máximo o dobro do sem filtro.

        **Nenhuma configuração o atendeu, então nenhuma é aplicada aqui** --
        a regra do próprio RFC para esse desfecho. Medido em 20.000 imagens:
        toda mitigação (`hnsw.iterative_scan = strict_order`, `ef_search` de
        100 a 400) deixou toda consulta completa, onde a linha de base voltou
        curta em 13 de 20 consultas com 20% de seletividade; mas a metade da
        latência falhou para toda configuração, a linha de base incluída,
        porque o planner responde a seletividades baixas com planos
        sequenciais *exatos* (23-65 ms no servidor com 5-10%, e até ~340 ms
        sob um plano genérico forçado), contra um p95 sem filtro de 3,0 ms.
        Esses planos não são afetados por nenhum ajuste do HNSW. O veredito e
        os números estão no RFC-032 §6.1; adotar `strict_order` mesmo assim
        seria mudar o critério, o que é decisão do RFC, não deste método.

        **O resultado é o melhor possível, sem garantia, quando o planner usa
        o índice.** O HNSW é um índice aproximado: um scan explora no máximo
        `hnsw.ef_search` (40 por padrão) candidatos, e os que forem tuplas
        mortas ainda não limpas pelo vacuum gastam esse orçamento e depois
        são filtrados. Então um index scan pode devolver *menos* resultados
        que `limit` enquanto existem mais linhas que casam, e o top-K dele
        não é garantidamente o top-K verdadeiro. Medido, e não teorizado: com
        um índice inchado de propósito, a consulta entregue devolveu 1 de 3
        linhas vivas (RFC-025 §7.3). Nos tamanhos que o planner responde com
        um seq scan, a ordenação é exata.

        **Acima de 40, a lista de candidatos é o `limit`** (RFC-038 §4.3):
        `ef_search` limita o que um scan pode devolver, ponto, então um
        `limit=100` sem filtro voltaria com 40 e nenhum erro, e uma página
        curta é como quem chama fica sabendo que não há mais nada.
        `set_config(..., true)` dura até o fim da transação, que é a da
        requisição -- `SET LOCAL` não aceita parâmetro. **Até 40 nada é
        enviado**, e a consulta é a que os RFCs 025 a 032 mediram (RFC-032
        §6.1, regra 4). É o piso que deixa o `limit` ser atendido, não um
        ajuste de recall: uma busca filtrada ainda pode voltar curta, pelo
        mecanismo do RFC-028 §8.1.

        **Qual plano responde depende da versão major do servidor**
        (`test_search_ef_search.py`). O desempate por `id` é uma segunda
        chave de ordenação que o índice HNSW não fornece; o PostgreSQL 17
        serve a primeira pelo índice e ordena os empates incrementalmente, e
        o 16 -- o runtime embarcado -- não consegue, e varre a tabela. Ali a
        ordenação é exata e o 40 nunca acontece.
        """
        self._require_indexed_dimension(embedding)
        if limit > PGVECTOR_DEFAULT_EF_SEARCH:
            self._session.execute(
                select(func.set_config("hnsw.ef_search", str(limit), True))
            )
        statement = self.search_statement(embedding, limit, filters)

        return [
            SearchHit(image=_image_of(row), similarity=1.0 - row.distance)
            for row in self._session.execute(statement)
        ]

    @classmethod
    def search_statement(
        cls,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> Select[Any]:
        """O `SELECT` exato que `search_similar()` roda, sem rodá-lo.

        Público, e `classmethod`, para um leitor além de `search_similar()`:
        `experiments/rfc-032-geolocation/planner_check.py`, que precisa dar
        `EXPLAIN` na consulta que a aplicação manda -- pré-filtro da caixa,
        haversine e tudo -- e não numa imitação copiada à mão que divergiria
        dela na primeira vez em que uma das duas mudasse.
        """
        distance = ImageModel.embedding.cosine_distance(list(embedding.values)).label(
            "distance"
        )
        statement = (
            select(*_ENTITY_COLUMNS, distance)
            .where(*_searchable())
            .order_by(distance, ImageModel.id)
            .limit(limit)
        )
        return cls._apply_filters(statement, filters)

    def list_by_filters(
        self,
        filters: SearchFilters,
        order: ListingOrder,
        limit: int,
        after: Image | None,
    ) -> ListedImages:
        """Uma página da listagem por data de captura, num `SELECT` (RFC-044 §5.2).

        O `WHERE` é o de `search_similar()` -- `embedding IS NOT NULL` e
        `_apply_filters()` --, e as colunas são as da entidade, a mesma lista:
        o `embedding` fica de fora, então os vetores no TOAST nunca são lidos
        (RFC-044 §2.3). Atrás de um `LIMIT`, o PostgreSQL ordena com um top-N
        heapsort que mantém só `limit` linhas na memória; isso é uma hipótese
        que o `EXPLAIN` da medição verifica, não uma promessa.

        O cursor é a própria chave de `after`, lida por quem chama a partir
        da chave primária -- nunca um caminho que um cliente mandou (RFC-030
        §5.1).
        """
        statement = self.listing_statement(filters, order, limit, after)
        return [_image_of(row) for row in self._session.execute(statement)]

    @classmethod
    def listing_statement(
        cls,
        filters: SearchFilters,
        order: ListingOrder,
        limit: int,
        after: Image | None = None,
    ) -> Select[Any]:
        """O `SELECT` exato que `list_by_filters()` roda, para a medição.

        Público pela razão de `search_statement()`:
        `experiments/rfc-044-browse/measure_browse.py` precisa dar `EXPLAIN`
        na consulta que a rota manda.

        `ORDER BY captured_at DESC NULLS LAST` (ou `ASC NULLS LAST`), depois
        o caminho e o id ascendentes. **O caminho é ordenado com `COLLATE
        "C"`, explicitamente**: ordem de bytes do UTF-8, que é a ordem de code
        point, que é como `str` compara nos doubles em memória. Os dois
        clusters são inicializados com o locale `C` (RFC-033a §5), e dizê-lo
        na consulta impede que a ordem dependa disso.
        """
        date = (
            ImageModel.captured_at.desc()
            if order is ListingOrder.NEWEST
            else ImageModel.captured_at.asc()
        )
        statement = (
            select(*_ENTITY_COLUMNS)
            .where(*_searchable())
            .order_by(date.nulls_last(), _path_in_c(), ImageModel.id)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(_after_clause(after, order))
        return cls._apply_filters(statement, filters)

    def summarize_listing(self, filters: SearchFilters) -> ListingSummary:
        """`count(*)`, as sem data, `min` e `max`, numa agregação (RFC-044 §5.3).

        O `WHERE` da listagem sem `ORDER BY` e sem `LIMIT`: uma passada pelas
        linhas que os filtros mantêm.
        """
        row = self._session.execute(self.summary_statement(filters)).one()
        return ListingSummary(
            total=int(row.total),
            undated=int(row.undated),
            oldest=row.oldest,
            newest=row.newest,
        )

    @classmethod
    def summary_statement(cls, filters: SearchFilters) -> Select[Any]:
        """O `SELECT` exato que `summarize_listing()` roda, para o mesmo leitor."""
        statement = select(
            func.count().label("total"),
            func.count().filter(ImageModel.captured_at.is_(None)).label("undated"),
            func.min(ImageModel.captured_at).label("oldest"),
            func.max(ImageModel.captured_at).label("newest"),
        )
        statement = statement.select_from(ImageModel).where(*_searchable())
        return cls._apply_filters(statement, filters)

    @staticmethod
    def _apply_filters(
        statement: Select[Any], filters: SearchFilters | None
    ) -> Select[Any]:
        """Acrescenta as cláusulas de restrição, ou nenhuma.

        Um `SearchFilters` ausente ou vazio não acrescenta nada, então o SQL
        emitido é, caractere por caractere, a consulta que o RFC-025 entregou,
        e os testes de contrato dele continuam descrevendo o comportamento
        sem filtro. A alternativa -- emitir sempre `device_id IN (...)` e
        deixar uma lista vazia significar "tudo" -- é o bug que este ramo
        existe para evitar: um `IN ()` vazio não casa com linha nenhuma,
        então um argumento com valor padrão devolveria nada, em silêncio.

        Cada campo acrescenta a sua própria cláusula, ou nenhuma,
        independentemente do outro. O mesmo raciocínio vale para o intervalo
        de datas: um ausente não é `[datetime.min, datetime.max)`, que
        excluiria toda imagem com `captured_at` NULL e mudaria a consulta sem
        filtro.

        O intervalo é `captured_at >= start AND captured_at < end`, e a regra
        do NULL do RFC-020 não precisa de cláusula própria aqui: uma
        comparação com NULL dá NULL, que o `WHERE` trata como falso, então
        uma imagem com data desconhecida nunca casa. As implementações em
        memória não ganham isso de graça e precisam dizer `is not None`
        explicitamente.

        Um círculo (RFC-032) acrescenta as suas cláusulas do mesmo jeito, e a
        mesma regra do NULL faz uma posição desconhecida falhar nelas de
        graça; veja `_circle_clauses()`.
        """
        if filters is None or filters.is_empty():
            return statement
        if filters.device_ids:
            statement = statement.where(_device_clause(filters))
        if filters.captured_between is not None:
            statement = statement.where(*_date_clauses(filters))
        if filters.taken_within is not None:
            statement = statement.where(*_circle_clauses(filters.taken_within))
        return statement

    def count_unknown_capture_date(self, filters: SearchFilters) -> int:
        """Conta, num `COUNT(*)`, o que um intervalo de datas escondeu por não ter data.

        Espelha o `WHERE` de `search_similar()`, menos a ordenação e menos a
        própria cláusula de data -- `embedding IS NOT NULL`, o conjunto de
        dispositivos, se houver, o círculo, se houver (RFC-032), e
        `captured_at IS NULL` --, para que "as fotos que o filtro escondeu" e
        "as fotos que a busca poderia ter mostrado" saiam da mesma definição
        de buscável.

        Nenhuma consulta sem um intervalo de datas, para que uma busca sem
        filtro nunca pague uma segunda ida e volta (RFC-028 §8).
        """
        if filters.captured_between is None:
            return 0

        statement = (
            select(func.count())
            .select_from(ImageModel)
            .where(*_searchable(), ImageModel.captured_at.is_(None))
        )
        if filters.device_ids:
            statement = statement.where(_device_clause(filters))
        if filters.taken_within is not None:
            statement = statement.where(*_circle_clauses(filters.taken_within))
        return int(self._session.execute(statement).scalar_one())

    def count_unknown_position(self, filters: SearchFilters) -> int:
        """Conta, num `COUNT(*)`, as imagens buscáveis que não têm posição.

        O `WHERE` de `search_similar()`, menos a ordenação e menos o círculo
        -- `embedding IS NOT NULL`, o conjunto de dispositivos, o intervalo
        de datas, e `latitude IS NULL` (RFC-032 §6.2). `latitude` sozinha
        basta: `ck_images_position_pairing` garante que `longitude` é NULL
        junto.

        Consulta tendo `filters` um círculo ou não, porque o mapa faz
        exatamente esta pergunta sem nenhum; não fazê-la para uma busca sem
        círculo é regra do `SearchImagesUseCase`.
        """
        statement = (
            select(func.count())
            .select_from(ImageModel)
            .where(*_searchable(), ImageModel.latitude.is_(None))
        )
        if filters.device_ids:
            statement = statement.where(_device_clause(filters))
        if filters.captured_between is not None:
            statement = statement.where(*_date_clauses(filters))
        return int(self._session.execute(statement).scalar_one())

    def aggregate_positions(
        self,
        filters: SearchFilters,
        area: BoundingBox,
        precision: int,
        cell_limit: int | None,
    ) -> PositionCells:
        """Um `GROUP BY` sobre coordenadas arredondadas (RFC-032 §7).

        `round(latitude::numeric, p)`: o PostgreSQL não tem
        `round(double precision, int)`, e o cast também é o que fixa a regra
        de arredondamento -- os 15 dígitos significativos do double,
        arredondados metade para longe do zero, em decimal -- que
        `round_to_cell()` reproduz para as implementações em memória.

        **A precisão é renderizada no SQL, e não passada como parâmetro.**
        Ela aparece na lista do select e no `GROUP BY`, e, com binding no
        servidor, dois parâmetros separados seriam duas expressões diferentes
        para o PostgreSQL, que então recusa a lista do select como "not in
        GROUP BY". `literal_execute` põe o inteiro no texto na hora da
        execução; ele é `int` pelo tipo, então nada que um cliente mande
        chega ao texto.

        A área são dois `BETWEEN`s nas colunas simples -- a mesma forma do
        pré-filtro de um círculo --, e as cláusulas de dispositivo e de data
        são as da própria busca, para que o universo do mapa não possa
        divergir do da busca. `filters.taken_within` não é aplicado (veja a
        porta).

        Se um índice atende a isto numa tabela grande é medido, e não
        suposto: `experiments/rfc-032-geolocation/planner_check.py`.
        """
        statement = self.aggregate_statement(filters, area, precision, cell_limit)
        return [
            PositionCell(
                latitude=float(row.cell_latitude),
                longitude=float(row.cell_longitude),
                count=int(row.photos),
            )
            for row in self._session.execute(statement)
        ]

    @staticmethod
    def aggregate_statement(
        filters: SearchFilters,
        area: BoundingBox,
        precision: int,
        cell_limit: int | None,
    ) -> Select[Any]:
        """O `SELECT` exato que `aggregate_positions()` roda, para o mesmo leitor.

        Público pela razão de `search_statement()`: a medição precisa dar
        `EXPLAIN` no que o endpoint manda.
        """
        digits = literal(int(precision), Integer, literal_execute=True)
        cell_latitude = func.round(sql_cast(ImageModel.latitude, Numeric), digits)
        cell_longitude = func.round(sql_cast(ImageModel.longitude, Numeric), digits)
        statement = (
            select(
                cell_latitude.label("cell_latitude"),
                cell_longitude.label("cell_longitude"),
                func.count().label("photos"),
            )
            .where(
                *_searchable(),
                ImageModel.latitude.between(area.min_latitude, area.max_latitude),
                ImageModel.longitude.between(area.min_longitude, area.max_longitude),
            )
            .group_by(cell_latitude, cell_longitude)
            .order_by(cell_latitude, cell_longitude)
        )
        if filters.device_ids:
            statement = statement.where(_device_clause(filters))
        if filters.captured_between is not None:
            statement = statement.where(*_date_clauses(filters))
        if cell_limit is not None:
            statement = statement.limit(cell_limit)
        return statement

    @staticmethod
    def _require_indexed_dimension(embedding: EmbeddingVector) -> None:
        """Recusa um vetor de consulta de largura errada antes que chegue ao servidor.

        O PostgreSQL também o recusa, mas não de modo confiável nem legível:
        a coluna `vector(512)` só reclama quando uma linha é de fato
        comparada, então a mesma chamada errada levanta erro contra uma
        tabela populada e devolve `[]` contra uma vazia. Verificar antes deixa
        a falha idêntica em todo lugar, e idêntica à das implementações em
        memória, que é o objetivo do teste de contrato compartilhado.
        """
        if len(embedding.values) != EMBEDDING_DIMENSION:
            raise EmbeddingDimensionMismatchError(
                f"Query embedding has {len(embedding.values)} dimensions, but "
                f"the images.embedding column holds {EMBEDDING_DIMENSION}."
            )

    def get_embedding(self, image_id: ImageId) -> EmbeddingVector | None:
        """`SELECT embedding` de uma linha: uns 2 KB, e nada mais."""
        statement = select(ImageModel.embedding).where(ImageModel.id == image_id.value)
        values = self._session.execute(statement).scalar_one_or_none()
        if values is None:
            return None
        return EmbeddingVector(values)

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        model = self._session.get(ImageModel, image_id.value)
        if model is None:
            return None
        return IndexMetadata(
            file_size=model.file_size,
            file_modified_at=model.file_modified_at,
            content_hash=model.content_hash,
            capture_source=_source_member(model.capture_source),
            position_source=_position_member(model.position_source),
            thumbnail_path=model.thumbnail_path,
        )

    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        """Lê os metadados de muitos ids numa ida e volta.

        Seleciona as colunas de metadados pelo nome, em vez de carregar
        linhas inteiras de `ImageModel`. Não é arrumação prematura:
        `embedding` é um vetor de 512 floats, então hidratar linhas inteiras
        arrastaria uns dois kilobytes por imagem pela rede para responder a
        uma pergunta decidida por dois escalares -- justamente no caminho que
        existe para tornar barato varrer de novo uma coleção grande que não
        mudou.

        `capture_source` é uma delas desde o RFC-028, para a escrita
        condicional da data de captura, e custa uma string curta por linha
        numa consulta que já rodava de todo jeito. `thumbnail_path` entrou no
        RFC-030 para o backfill das thumbnails, nos mesmos termos, e
        `position_source` no RFC-032, para a escrita condicional da posição.
        """
        if not image_ids:
            return {}

        statement = select(
            ImageModel.id,
            ImageModel.file_size,
            ImageModel.file_modified_at,
            ImageModel.content_hash,
            ImageModel.capture_source,
            ImageModel.position_source,
            ImageModel.thumbnail_path,
        ).where(ImageModel.id.in_([image_id.value for image_id in image_ids]))

        return {
            ImageId(row.id): IndexMetadata(
                file_size=row.file_size,
                file_modified_at=row.file_modified_at,
                content_hash=row.content_hash,
                capture_source=_source_member(row.capture_source),
                position_source=_position_member(row.position_source),
                thumbnail_path=row.thumbnail_path,
            )
            for row in self._session.execute(statement)
        }

    def list_change_signals(self, device_id: DeviceId) -> dict[ImageId, ChangeSignals]:
        """Três colunas de cada linha do dispositivo, numa consulta (RFC-045 §5.2).

        Colunas nomeadas, e não linhas de `ImageModel`, pela razão que o
        prefetch dá: hidratar o embedding de 116.396 linhas para comparar
        dois escalares leria o índice inteiro para responder a uma pergunta
        sobre os metadados dele. Sem `ORDER BY`: quem chama procura ids, e
        uma ordem custaria uma ordenação que ninguém lê.
        """
        statement = select(
            ImageModel.id, ImageModel.file_size, ImageModel.file_modified_at
        ).where(ImageModel.device_id == device_id.value)

        return {
            ImageId(row.id): ChangeSignals(
                file_size=row.file_size, file_modified_at=row.file_modified_at
            )
            for row in self._session.execute(statement)
        }

    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        """Atualiza os metadados de arquivo de uma linha, sem tocar no embedding.

        Um UPDATE direcionado, em vez de ler-modificar-escrever, porque todo o
        ponto deste caminho é que nenhum embedding precisa ser produzido nem
        movido; carregar a linha buscaria o vetor de 512 floats que este
        método existe para não tocar.
        """
        statement = (
            update(ImageModel)
            .where(ImageModel.id == image_id.value)
            .values(
                file_size=metadata.file_size,
                file_modified_at=metadata.file_modified_at,
                content_hash=metadata.content_hash,
            )
        )
        self._session.execute(statement)
        self._session.commit()

    def update_capture_date(self, image_id: ImageId, capture: CaptureDate) -> None:
        """Escreve a data de captura de uma linha com um UPDATE direcionado.

        Como `update_index_metadata()`, e pela mesma razão: carregar a linha
        antes buscaria o embedding que este caminho existe para deixar em
        paz. Um id sem linha não casa com nada e não muda nada.
        """
        self.update_capture_date_many({image_id: capture})

    def update_capture_date_many(self, captures: Mapping[ImageId, CaptureDate]) -> None:
        """Escreve muitas datas de captura num UPDATE executemany e num commit.

        Um `UPDATE ... WHERE id = :target_id` do Core executado com uma lista
        de conjuntos de parâmetros, em vez do bulk update por chave primária
        do ORM. A forma do ORM verifica que cada chave casou com uma linha, e
        o contrato da porta é o oposto: um id sem linha é pulado em silêncio,
        como no método de uma linha só.

        Sofre rollback na falha antes de o erro se propagar, para que quem
        chama possa recorrer às escritas linha a linha numa sessão limpa.
        """
        if not captures:
            return

        table = cast(Table, ImageModel.__table__)
        statement = (
            update(table)
            .where(table.c.id == bindparam("target_id"))
            .values(
                captured_at=bindparam("new_captured_at"),
                capture_source=bindparam("new_capture_source"),
            )
        )
        parameters = [
            {
                "target_id": image_id.value,
                "new_captured_at": capture.captured_at,
                "new_capture_source": capture.source.value,
            }
            for image_id, capture in captures.items()
        ]
        try:
            self._session.execute(statement, parameters)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def update_position(self, image_id: ImageId, reading: PositionReading) -> None:
        """Escreve a posição de uma linha com um UPDATE direcionado (RFC-032 §8)."""
        self.update_position_many({image_id: reading})

    def update_position_many(self, readings: Mapping[ImageId, PositionReading]) -> None:
        """Escreve muitas posições num UPDATE executemany e num commit.

        A forma de `update_capture_date_many()`, pelas razões dele: um
        `UPDATE ... WHERE id = :target_id` do Core, para que um id sem linha
        seja pulado e não recusado; nenhuma leitura da linha -- que
        arrastaria o embedding junto --; e um rollback antes de relançar,
        para que o fallback linha a linha de quem chama comece de uma sessão
        limpa.

        As três colunas mudam num só comando, então o CHECK do par nunca vê
        uma linha no meio da escrita com meia posição.
        """
        if not readings:
            return

        table = cast(Table, ImageModel.__table__)
        statement = (
            update(table)
            .where(table.c.id == bindparam("target_id"))
            .values(
                latitude=bindparam("new_latitude"),
                longitude=bindparam("new_longitude"),
                position_source=bindparam("new_position_source"),
            )
        )
        parameters = [
            {
                "target_id": image_id.value,
                "new_latitude": reading.latitude,
                "new_longitude": reading.longitude,
                "new_position_source": reading.source.value,
            }
            for image_id, reading in readings.items()
        ]
        try:
            self._session.execute(statement, parameters)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def update_thumbnail_path(self, image_id: ImageId, location: str) -> None:
        """Registra o local da thumbnail de uma linha com um UPDATE direcionado."""
        self.update_thumbnail_path_many({image_id: location})

    def update_thumbnail_path_many(self, locations: Mapping[ImageId, str]) -> None:
        """Registra muitos locais de thumbnail num UPDATE executemany.

        A forma de `update_capture_date_many()`, pelas razões dele: um
        `UPDATE` do Core, para que um id sem linha seja pulado e não
        recusado; nenhuma leitura da linha -- que arrastaria o embedding
        junto --; e um rollback antes de relançar, para que o fallback linha
        a linha de quem chama comece de uma sessão limpa.
        """
        if not locations:
            return

        table = cast(Table, ImageModel.__table__)
        statement = (
            update(table)
            .where(table.c.id == bindparam("target_id"))
            .values(thumbnail_path=bindparam("new_thumbnail_path"))
        )
        parameters = [
            {"target_id": image_id.value, "new_thumbnail_path": location}
            for image_id, location in locations.items()
        ]
        try:
            self._session.execute(statement, parameters)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def list_presence(self, device_id: DeviceId) -> Presences:
        """Três colunas de cada linha do dispositivo, numa consulta (RFC-046 §5.5).

        Colunas nomeadas, pela razão que `list_change_signals()` dá: o
        embedding de 116.396 linhas fica onde está. A marca volta como um
        booleano calculado pelo servidor; a varredura precisa saber se, não
        quando.

        **Lida por um cursor no servidor**, `PRESENCE_CHUNK` linhas por vez
        (`yield_per`), e entregue conforme chega: a varredura guarda só as
        linhas que marca ou desmarca.
        """
        statement = (
            select(
                ImageModel.id,
                ImageModel.relative_path,
                ImageModel.missing_since.is_not(None).label("missing"),
            )
            .where(ImageModel.device_id == device_id.value)
            .execution_options(yield_per=PRESENCE_CHUNK)
        )
        for row in self._session.execute(statement):
            yield ImagePresence(
                image_id=ImageId(row.id),
                relative_path=row.relative_path,
                missing=bool(row.missing),
            )

    def mark_presence(
        self,
        missing: Collection[ImageId],
        present: Collection[ImageId],
        since: datetime.datetime,
    ) -> None:
        """Dois `UPDATE`s por id, em blocos, e um commit (RFC-046 §5.5).

        **`UPDATE`s pelo ORM, e não pelo Core.** As sessões são criadas com
        `expire_on_commit=False`, então uma linha que esta sessão carregou
        antes mantém o `missing_since` antigo no identity map; um
        `save_indexed()` posterior que o pusesse em `None` sobre um `None`
        velho não seria mudança nenhuma para o ORM, e a linha continuaria
        marcada, embora acabasse de ser indexada. A forma do ORM leva junto
        os objetos que estão na sessão.

        Só as linhas ainda não marcadas são marcadas -- `missing_since IS
        NULL` no `WHERE` --, então uma linha que faltou em dois scans mantém a
        primeira data.

        **Em blocos de `PRESENCE_CHUNK` ids.** Um disco reorganizado pode
        perder todas as suas linhas de uma vez, e um `IN` de 116.396 valores
        são mais parâmetros do que um comando pode levar (65.535).
        """
        missing_ids = [image_id.value for image_id in missing]
        present_ids = [image_id.value for image_id in present]
        if set(missing_ids) & set(present_ids):
            raise ValueError("An image cannot be both missing and present")
        if not missing_ids and not present_ids:
            return

        try:
            for chunk in _chunks(missing_ids, PRESENCE_CHUNK):
                self._session.execute(
                    update(ImageModel)
                    .where(ImageModel.id.in_(chunk), ImageModel.missing_since.is_(None))
                    .values(missing_since=since)
                )
            for chunk in _chunks(present_ids, PRESENCE_CHUNK):
                self._session.execute(
                    update(ImageModel)
                    .where(
                        ImageModel.id.in_(chunk), ImageModel.missing_since.is_not(None)
                    )
                    .values(missing_since=None)
                )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def count_missing_by_device(self) -> dict[DeviceId, MissingImages]:
        """Um `GROUP BY device_id` sobre as linhas marcadas (RFC-046 §5.8)."""
        statement = (
            select(
                ImageModel.device_id,
                func.count(),
                func.min(ImageModel.missing_since),
            )
            .where(ImageModel.missing_since.is_not(None))
            .group_by(ImageModel.device_id)
        )
        return {
            DeviceId(device_id): MissingImages(count=int(total), since=oldest)
            for device_id, total, oldest in self._session.execute(statement).all()
            # Só há grupo para linhas marcadas, então o `min` nunca é NULL.
            if oldest is not None
        }

    def purge_missing(self, device_id: DeviceId) -> PurgedImages:
        """`DELETE ... RETURNING thumbnail_path`, e um commit (RFC-046 §5.7).

        Pelo ORM, pela razão de `mark_presence()`: os objetos das linhas
        apagadas saem da sessão junto com elas, para que um `get()` na mesma
        sessão não devolva uma linha que já não existe.
        """
        statement = (
            delete(ImageModel)
            .where(
                ImageModel.device_id == device_id.value,
                ImageModel.missing_since.is_not(None),
            )
            .returning(ImageModel.thumbnail_path)
        )
        try:
            locations = list(self._session.execute(statement).scalars())
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return PurgedImages(
            count=len(locations),
            thumbnail_paths=tuple(
                location for location in locations if location is not None
            ),
        )

    def count_by_device(self) -> dict[DeviceId, int]:
        """Um `GROUP BY device_id` para todos os dispositivos de uma vez (RFC-031 §4.3).

        Um `COUNT(*)` por dispositivo seriam N idas e voltas atrás de uma
        chamada que parece uma só, e `GET /api/v1/devices` mostra cada disco
        na barra lateral -- então N é quantos discos o usuário tiver, a cada
        carregamento da página.

        Dispositivos sem linhas não produzem grupo e, portanto, ficam de fora
        do resultado, que é o contrato: quem chama o lê com
        `.get(device_id, 0)`.
        """
        statement = (
            select(ImageModel.device_id, func.count())
            .where(_present())
            .group_by(ImageModel.device_id)
        )
        return {
            DeviceId(device_id): int(total)
            for device_id, total in self._session.execute(statement).all()
        }

    def count_by_path_prefixes(
        self, device_id: DeviceId, parent: JobScope
    ) -> dict[str, int]:
        r"""Uma consulta agrupada para cada subpasta de `parent` (RFC-031 §8.2).

        A razão inteira de este método existir, em vez de um `COUNT(*)` por
        pasta: uma listagem de quarenta pastas tem de custar uma consulta, e
        não quarenta.

        A chave do agrupamento é `split_part(substr(relative_path, cut), '/',
        1)` -- tudo depois do prefixo do pai, até o próximo separador. `cut`
        é a posição logo depois da `/` que segue o prefixo, e `1` para o
        escopo do dispositivo inteiro, onde a primeira parte de
        `relative_path` já é a resposta.

        **O `LIKE` é ancorado à esquerda de propósito, e a `/` final sustenta
        o resto.** `'2018b' LIKE '2018%'` é verdadeiro e `2018b` é outra
        pasta; `'2018b/x.jpg' LIKE '2018/%'` é falso, que é o comportamento
        que o RFC-029 §10 exige e que `JobScope` impõe uma camada acima.
        Usa-se `startswith(..., autoescape=True)`, e não um padrão montado à
        mão, para que uma pasta cujo nome contém `%` ou `_` case
        literalmente, e não como curinga.

        Se um índice atende a isto é medido, e não suposto -- veja
        `experiments/rfc-031-devices-api/measure_folder_listing.log` para o
        `EXPLAIN` e a collation do cluster, que decidem se uma B-tree em
        `(device_id, relative_path)` serve, afinal, para um `LIKE` ancorado à
        esquerda.

        Os dois tipos de chave que quem chama precisa descartar -- um arquivo
        direto em `parent`, e uma pasta renomeada no disco desde que foi
        indexada -- estão documentados na porta e de propósito não são
        filtrados aqui: esta consulta não distingue uma pasta de um arquivo
        sem tocar no disco, e quem chama já tem os nomes que o sistema de
        arquivos informou.
        """
        prefix = str(parent)
        cut = len(prefix) + 2 if parent.parts else 1
        folder = func.split_part(func.substr(ImageModel.relative_path, cut), "/", 1)
        statement = select(folder, func.count()).where(
            ImageModel.device_id == device_id.value, _present()
        )
        if parent.parts:
            statement = statement.where(
                ImageModel.relative_path.startswith(f"{prefix}/", autoescape=True)
            )
        statement = statement.group_by(folder)
        return {
            str(name): int(total)
            for name, total in self._session.execute(statement).all()
        }


_ENTITY_COLUMNS = (
    ImageModel.id,
    ImageModel.device_id,
    ImageModel.relative_path,
    ImageModel.filename,
    ImageModel.extension,
    ImageModel.captured_at,
    ImageModel.capture_source,
    ImageModel.latitude,
    ImageModel.longitude,
    ImageModel.position_source,
)
"""As colunas de que um `Image` é montado: todas, menos o `embedding`.

Compartilhadas pela busca e pela listagem, para que uma foto achada por uma
consulta e a mesma foto listada por data tenham os mesmos campos.
`absolute_path` não é coluna: o banco não sabe onde um dispositivo está
montado (RFC-027 §7).
"""


def _image_of(row: Any) -> Image:
    """Um `Image` a partir de uma linha de `_ENTITY_COLUMNS`."""
    return Image(
        id=ImageId(row.id),
        device_id=DeviceId(row.device_id),
        relative_path=ImagePath(row.relative_path),
        filename=row.filename,
        extension=row.extension,
        captured_at=row.captured_at,
        capture_source=_source_member(row.capture_source),
        latitude=row.latitude,
        longitude=row.longitude,
        position_source=_position_member(row.position_source),
    )


def _path_in_c() -> ColumnElement[str]:
    """`relative_path COLLATE "C"`: comparado por code point, como o Python compara."""
    return ImageModel.relative_path.collate("C")


def _after_clause(after: Image, order: ListingOrder) -> ColumnElement[bool]:
    """Cada linha cuja chave de listagem vem depois da de `after` (RFC-044 §5.2).

    Com a chave de `after` igual a `(c, p, i)`:

        after dated     captured_at < c                       (> c for OLDEST)
                        OR (captured_at = c AND (path, id) > (p, i))
                        OR captured_at IS NULL
        after undated   captured_at IS NULL AND (path, id) > (p, i)

    As sem data vêm depois de toda linha datada, nas duas direções, então,
    depois de um cursor datado, todas elas ainda estão à frente, e, depois
    de um sem data, só as sem data com caminho e id maiores. `(path, id) >
    (p, i)` é uma comparação de linhas, que o PostgreSQL avalia
    lexicograficamente -- o caminho `COLLATE "C"`, o id byte a byte, como o
    `ORDER BY` os ordena.
    """
    rest = tuple_(_path_in_c(), ImageModel.id) > tuple_(
        literal(str(after.relative_path)), literal(after.id.value, ImageModel.id.type)
    )
    captured = after.captured_at
    if captured is None:
        return and_(ImageModel.captured_at.is_(None), rest)
    beyond = (
        ImageModel.captured_at < captured
        if order is ListingOrder.NEWEST
        else ImageModel.captured_at > captured
    )
    return or_(
        beyond,
        and_(ImageModel.captured_at == captured, rest),
        ImageModel.captured_at.is_(None),
    )


def _present() -> ColumnElement[bool]:
    """`missing_since IS NULL`: o arquivo da linha estava no último scan (RFC-046)."""
    return ImageModel.missing_since.is_(None)


def _searchable() -> tuple[ColumnElement[bool], ...]:
    """O que uma linha precisa para ser mostrada: um vetor, e o arquivo no disco.

    O único lugar em que as duas condições são escritas (RFC-046 §6). As seis
    leituras que servem a interface -- a busca, a listagem e o resumo dela, as
    duas contagens de fotos excluídas e o mapa -- chamam esta função, para que
    uma leitura nova não possa lembrar de uma condição e esquecer a outra. As
    duas contagens, por dispositivo e por pasta, usam só `_present()`: contam
    linhas com ou sem vetor, como sempre contaram.
    """
    return (ImageModel.embedding.is_not(None), _present())


def _chunks(values: list[uuid.UUID], size: int) -> Iterator[list[uuid.UUID]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _device_clause(filters: SearchFilters) -> ColumnElement[bool]:
    """O predicado do conjunto de dispositivos, da busca, das contagens e do mapa."""
    return ImageModel.device_id.in_(
        [device_id.value for device_id in filters.device_ids]
    )


def _date_clauses(filters: SearchFilters) -> tuple[ColumnElement[bool], ...]:
    """O intervalo semiaberto de data de captura, da busca, das contagens e do mapa."""
    date_range = filters.captured_between
    if date_range is None:
        return ()
    return (
        ImageModel.captured_at >= date_range.start,
        ImageModel.captured_at < date_range.end,
    )


def _circle_clauses(circle: GeoCircle) -> tuple[ColumnElement[bool], ...]:
    """Um círculo em SQL: uma caixa descartável, depois a distância exata (RFC-032 §6).

    **A última cláusula é a que responde.** A caixa --
    `latitude BETWEEN ... AND longitude BETWEEN ...` -- existe só para que um
    índice *possa* ser usado; a distância haversine contra `radius_m` é o
    filtro. Uma caixa sozinha seria um quadrado, devolvendo cantos a 1,41
    vez o raio pedido.

    **A caixa é omitida quando estaria errada** -- um círculo sobre um polo,
    ou cruzando o meridiano 180, para o qual `GeoCircle.bounding_box()`
    devolve `None` --, e então só a cláusula exata é emitida: mais lenta, e
    certa.

    Sem `IS NOT NULL`: uma latitude NULL torna NULL as comparações da caixa
    e a distância, o que o `WHERE` lê como falso, então uma imagem de
    posição desconhecida nunca casa, por maior que seja o círculo (RFC-020).
    Isso só vale porque cada passo da distância propaga NULL -- veja o
    limite em `_distance_m()`, onde o `least()` óbvio não propagava.
    """
    clauses: list[ColumnElement[bool]] = []
    box = circle.bounding_box()
    if box is not None:
        clauses.append(ImageModel.latitude.between(box.min_latitude, box.max_latitude))
        clauses.append(
            ImageModel.longitude.between(box.min_longitude, box.max_longitude)
        )
    clauses.append(_distance_m(circle) <= circle.radius_m)
    return tuple(clauses)


def _distance_m(circle: GeoCircle) -> ColumnElement[float]:
    """A distância haversine de `circle.center` a cada linha, em metros.

    Termo a termo a fórmula de `app.domain.services.haversine`, com o mesmo
    `EARTH_RADIUS_M` e a mesma ordem de operações -- as diferenças tomadas
    antes de `radians()`, o limite em 1 antes do `asin` --, para que um
    ponto perto da borda de um círculo caia do mesmo lado aqui e nas
    implementações em memória. Aritmética simples sobre duas colunas; nenhuma
    extensão (RFC-032 §5.1).

    "Mesmo lado" até o último bit de `sin` e `cos`, que vêm da biblioteca de
    matemática do servidor do banco, e não da do Python: duas bibliotecas
    podem arredondar o mesmo argumento com uma unidade de diferença. Um ponto
    a um nanômetro da borda não é algo que um teste de contrato consiga
    fixar, e nenhum tenta.

    Toda função é tipada `Double` de propósito. Sem tipo, o SQLAlchemy trata
    `radians(...) / 2` como uma divisão de desconhecidos e renderiza o `2`
    como `CAST(... AS NUMERIC)`; o PostgreSQL o converteria de volta, mas o
    SQL deixaria de dizer o que o Python diz -- aritmética de float, do
    começo ao fim.
    """
    center = circle.center

    def double(name: str, *arguments: Any) -> ColumnElement[float]:
        expression: ColumnElement[float] = getattr(func, name)(*arguments, type_=Double)
        return expression

    def squared_sine_of_half(angle: ColumnElement[float]) -> ColumnElement[float]:
        sine = double("sin", angle / 2.0)
        return sine * sine

    latitude_delta = double("radians", ImageModel.latitude - center.latitude)
    longitude_delta = double("radians", ImageModel.longitude - center.longitude)
    h = squared_sine_of_half(latitude_delta) + double(
        "cos", double("radians", literal(center.latitude, Double))
    ) * double("cos", double("radians", ImageModel.latitude)) * squared_sine_of_half(
        longitude_delta
    )
    # O limite é um CASE e tem de continuar sendo. `least(1, sqrt(h))` se lê
    # igual e é um bug: o LEAST do PostgreSQL *ignora* argumentos NULL, então
    # uma linha sem posição ganhava `least(1, NULL) = 1` e uma distância de
    # pi * R -- uns 20.015 km -- em vez de NULL. Qualquer círculo sem caixa
    # de pré-filtro e com um raio desse tamanho casava então com toda foto
    # sem posição. `test_an_unknown_position_never_matches` pegou isso.
    root = double("sqrt", h)
    clamped: ColumnElement[float] = case(
        (root > literal(1.0, Double), literal(1.0, Double)), else_=root
    )
    return literal(2 * EARTH_RADIUS_M, Double) * double("asin", clamped)


def _source_value(source: CaptureSource | None) -> str | None:
    return source.value if source is not None else None


def _source_member(value: str | None) -> CaptureSource | None:
    """Lê de volta um `capture_source` guardado, mantendo NULL como `None`.

    Nunca mapeia NULL para `CaptureSource.UNKNOWN`: NULL é "nunca
    examinada", e o scan só escreve linhas nesse estado (RFC-028 §4.2).
    """
    return CaptureSource(value) if value is not None else None


def _position_value(source: PositionSource | None) -> str | None:
    return source.value if source is not None else None


def _position_member(value: str | None) -> PositionSource | None:
    """Relê um `position_source` guardado, mantendo NULL como `None` (RFC-032 §4.4)."""
    return PositionSource(value) if value is not None else None
