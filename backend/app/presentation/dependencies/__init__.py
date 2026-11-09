"""Funções de injeção de dependência da camada de apresentação.

A raiz de composição: o único módulo autorizado a conhecer todas as
classes concretas ao mesmo tempo. Ele importa o SQLAlchemy e o adaptador
CLIP de propósito -- é isso que compor a Infraestrutura quer dizer -- e
fica, deliberadamente, fora da fronteira de imports que
`tests/test_ai_layer_boundaries.py` impõe sobre `app/presentation/api/`,
que cobre os routers, não a ligação entre eles. O adaptador CLIP é
importado por `get_embedding_model()`, na primeira chamada, para que
importar a API não importe nenhum `torch` (RFC-036 §5.3).
"""

from __future__ import annotations

import threading

from fastapi import Depends
from sqlalchemy.orm import Session

from app.application.use_cases.aggregate_positions import AggregatePositionsUseCase
from app.application.use_cases.browse_images import BrowseImagesUseCase
from app.application.use_cases.cancel_indexing_job import CancelIndexingJobUseCase
from app.application.use_cases.create_indexing_job import CreateIndexingJobUseCase
from app.application.use_cases.describe_devices import DescribeDevicesUseCase
from app.application.use_cases.geocode_address import GeocodeAddressUseCase
from app.application.use_cases.get_image_details import GetImageDetailsUseCase
from app.application.use_cases.get_indexing_job import GetIndexingJobUseCase
from app.application.use_cases.get_thumbnail import GetThumbnailUseCase
from app.application.use_cases.index_image import IndexImageUseCase
from app.application.use_cases.list_device_folders import ListDeviceFoldersUseCase
from app.application.use_cases.list_devices import ListDevicesUseCase
from app.application.use_cases.list_indexing_jobs import ListIndexingJobsUseCase
from app.application.use_cases.list_mounted_volumes import ListMountedVolumesUseCase
from app.application.use_cases.purge_missing_images import PurgeMissingImagesUseCase
from app.application.use_cases.register_device import RegisterDeviceUseCase
from app.application.use_cases.rename_device import RenameDeviceUseCase
from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.application.use_cases.reveal_image import RevealImageUseCase
from app.application.use_cases.search_images import (
    FindSimilarImagesUseCase,
    SearchImagesUseCase,
)
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.services.file_revealer_port import FileRevealerPort
from app.domain.services.geocoder_port import GeocoderPort
from app.domain.services.thumbnail_store_port import ThumbnailStorePort
from app.domain.services.volume_catalog import VolumeCatalog
from app.infrastructure.ai.model_runtime import CLIP, TRANSLATOR, default_runtime
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.file_revealer import WindowsFileRevealer
from app.infrastructure.filesystem.mounted_device_locator import MountedDeviceLocator
from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore
from app.infrastructure.filesystem.volume_catalog import MountedVolumeCatalog
from app.infrastructure.filesystem.volume_identity_provider import (
    WindowsVolumeIdentityProvider,
)
from app.infrastructure.geocoding.nominatim_geocoder import NominatimGeocoder
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.persistence.postgres_indexing_job_repository import (
    PostgresIndexingJobRepository,
)
from app.infrastructure.persistence.session import get_db


def get_image_repository(
    session: Session = Depends(get_db),
) -> ImageRepository:
    """Devolve um repositório sobre o PostgreSQL, ligado à sessão da requisição.

    A sessão vem de `get_db`, que abre uma e a fecha num `finally`, então
    o FastAPI libera a conexão quando a resposta é enviada. Até o RFC-026
    esta função chamava `SessionLocal()` ela mesma e nada nunca fechava o
    resultado -- inofensivo enquanto os únicos chamadores eram testes, e
    um vazamento real no momento em que toda requisição HTTP passou a
    executá-la.

    O que vazava é pior que um número de conexões. Uma sessão que executou
    um `SELECT` segura uma conexão do pool dentro de uma transação aberta;
    o pool padrão tem 5 conexões com 10 de overflow, então requisições que
    chegam mais rápido do que o coletor de lixo recupera sessões entram na
    fila e depois estouram o tempo, de forma não determinística. E um
    backend `idle in transaction` bloqueia o autovacuum, que é o mecanismo
    por trás da falha que o RFC-025 §7.3 mediu por acidente: entradas
    mortas no índice fizeram um scan HNSW devolver 1 de 3 linhas vivas. O
    vazamento e aquele colapso de recall são o mesmo bug visto das duas
    pontas.

    Um repositório novo por chamada continua correto -- uma `Session` do
    SQLAlchemy não é segura para compartilhar entre requisições
    concorrentes -- e continua sendo o ponto que os testes substituem.
    `InMemoryImageRepository` continua disponível em
    `app.infrastructure.persistence.in_memory_image_repository` para os
    testes unitários da camada de Aplicação que não podem tocar num banco.
    """
    return PostgresImageRepository(session)


_embedding_model: EmbeddingModelPort | None = None
_embedding_model_lock = threading.Lock()


def get_embedding_model() -> EmbeddingModelPort:
    """Devolve o adaptador CLIP único do processo, construído no primeiro uso.

    **Este provedor não recebe parâmetros, e não pode passar a receber.**
    `IndexingWorker.main()` o importa e o chama como uma função simples,
    sem argumentos, então acrescentar um default `Depends(...)` entregaria
    à CLI um objeto `Depends` onde ela espera um modelo, e a falha viria
    em tempo de execução, longe da edição que a causou. Nada se perde com
    a restrição: um provedor sem argumentos já funciona como dependência
    do FastAPI, e é assim que os provedores de casos de uso abaixo o
    consomem.

    Preguiçoso de propósito, ao contrário do singleton ansioso, no nível
    do módulo, que o RFC-016 estabeleceu para `InMemoryImageRepository`.
    Importar este módulo é algo que a maior parte da suíte de testes faz
    transitivamente (qualquer import de `app.presentation.api` chega
    aqui), e um `ClipEmbeddingModel()` ansioso na importação seria uma
    mina no dia em que o construtor começasse a fazer trabalho de verdade.

    **Os módulos dos adaptadores são importados aqui, na primeira chamada,
    e não no topo deste módulo** (RFC-036 §5.3). Eles importam `torch` e
    `transformers`, 13 a 18 s num disco rígido (RFC-034 §9), e a porta da
    API espera tudo o que o seu grafo de imports importa. Nada mais do que
    a API serve precisa de um modelo.

    **Construído uma vez, sob um lock**, que é o carregar-uma-vez,
    reusar-sempre do RFC-023 §10. Antes era um `lru_cache`, que roda a
    função de novo para um segundo chamador que chega antes de o primeiro
    retornar -- inofensivo enquanto construir não custava nada, e um
    segundo adaptador, carregando o checkpoint uma segunda vez, agora que
    a primeira chamada importa `torch` com a API já respondendo: a thread
    de aquecimento e a primeira busca construiriam um cada. A construção
    em si continua de graça: o adaptador adia os dois modelos até uma
    chamada `encode_*` de fato.

    Cada adaptador recebe o diretório do seu modelo na árvore de
    `settings.model_runtime_directory`, como uma função que confere os
    arquivos contra o lockfile e é chamada no primeiro carregamento
    (RFC-035 §5.1). Um dispositivo para os dois, resolvido uma vez.

    `FakeEmbeddingModel` continua intocado em
    `app.infrastructure.ai.fake_embedding_model` e continua sendo o dublê
    de teste de tudo o que não pode carregar um modelo real.
    """
    global _embedding_model
    if _embedding_model is None:
        with _embedding_model_lock:
            if _embedding_model is None:
                _embedding_model = _build_embedding_model()
    return _embedding_model


def _build_embedding_model() -> EmbeddingModelPort:
    from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
    from app.infrastructure.ai.device import resolve_device
    from app.infrastructure.ai.query_adapter import QueryAdapter
    from app.infrastructure.ai.query_translator import MarianQueryTranslator

    runtime = default_runtime()
    device = resolve_device()
    return ClipEmbeddingModel(
        runtime.loader(CLIP),
        translator=MarianQueryTranslator(runtime.loader(TRANSLATOR), device=device),
        device=device,
        query_adapter=QueryAdapter.load,
    )


def get_index_image_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    embedding_model: EmbeddingModelPort = Depends(get_embedding_model),
) -> IndexImageUseCase:
    """Devolve um caso de uso de indexação composto com as dependências comuns.

    Injetado em vez de se compor sozinho, pelo mesmo motivo do provedor de
    busca abaixo: o repositório precisa vir da sessão da requisição, e todo
    nível do grafo deve poder ser substituído nos testes. A indexação não
    tem rota HTTP -- a CLI do worker é a única porta de entrada
    (`AI_Context.md`: a indexação é executada pelo Worker, nunca pelo
    FastAPI) --, mas deixar este provedor chamando `get_image_repository()`
    diretamente traria de volta a sessão nunca fechada no momento em que
    algo o resolvesse.
    """
    return IndexImageUseCase(
        repository=repository,
        embedding_model=embedding_model,
    )


def get_search_images_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    embedding_model: EmbeddingModelPort = Depends(get_embedding_model),
) -> SearchImagesUseCase:
    """Devolve um caso de uso de busca composto com as dependências comuns.

    `settings.top_k_results` é lido aqui e injetado, nunca importado pelo
    caso de uso: a camada de Aplicação não pode depender da configuração da
    Infraestrutura (`test_application_architecture.py` garante isso), então
    esta é a camada que transforma uma configuração num argumento -- o
    mesmo arranjo que `IndexOrUpdateImagesUseCase` usa para `batch_size`.

    `settings.min_radius_m` chega do mesmo jeito (RFC-032 §6): o piso de um
    círculo "perto daqui" é política da Aplicação, injetada aqui e nunca
    lida pelo caso de uso.
    """
    return SearchImagesUseCase(
        repository=repository,
        embedding_model=embedding_model,
        default_limit=settings.top_k_results,
        min_radius_m=settings.min_radius_m,
    )


def get_find_similar_images_use_case(
    repository: ImageRepository = Depends(get_image_repository),
) -> FindSimilarImagesUseCase:
    """Devolve o caso de uso por trás de `GET /api/v1/images/{id}/similar` (RFC-038).

    **Sem modelo de embedding**, como o provedor do mapa abaixo: o vetor da
    foto é o guardado, e depender de `get_embedding_model()` importaria
    `torch` -- 13 a 18 s num disco rígido -- para uma rota que nunca
    codifica nada, e a faria esperar por um modelo que ela não usa, ou
    falhar com ele. `test_similar_api.py` troca esse provedor por um que
    falha se chamado. O tamanho da página e o piso do raio chegam como na
    busca.
    """
    return FindSimilarImagesUseCase(
        repository=repository,
        default_limit=settings.top_k_results,
        min_radius_m=settings.min_radius_m,
    )


def get_browse_images_use_case(
    repository: ImageRepository = Depends(get_image_repository),
) -> BrowseImagesUseCase:
    """Devolve o caso de uso por trás de `GET /api/v1/images/browse` (RFC-044).

    **Sem modelo de embedding**, como os de `/similar` e do mapa: uma
    listagem por data nunca codifica nada, e compor o CLIP importaria
    `torch` -- 13 a 18 s num disco rígido (RFC-034 §9) -- para uma rota que
    é o único jeito de ver as fotos de um lugar com a busca `unavailable`.
    `test_browse_api.py` troca o provedor do modelo por um que falha se
    chamado. O tamanho da página e o piso do raio chegam como na busca.
    """
    return BrowseImagesUseCase(
        repository=repository,
        default_limit=settings.top_k_results,
        min_radius_m=settings.min_radius_m,
    )


def get_aggregate_positions_use_case(
    repository: ImageRepository = Depends(get_image_repository),
) -> AggregatePositionsUseCase:
    """Devolve o caso de uso por trás de `GET /api/v1/images/map` (RFC-032 §7).

    Sem modelo de embedding: o mapa ignora o texto da consulta por desenho,
    então compor o CLIP aqui carregaria um modelo que a rota nunca pode
    usar. `settings.max_map_cells` é injetado, como `min_radius_m` acima.
    """
    return AggregatePositionsUseCase(
        repository=repository, max_cells=settings.max_map_cells
    )


_geocoder: GeocoderPort | None = None
_geocoder_lock = threading.Lock()


def get_geocoder() -> GeocoderPort:
    """O geocoder único do processo: um relógio para a regra de uma requisição
    por segundo, e um cache, em vez de um por requisição que esquece o
    anterior."""
    global _geocoder
    if _geocoder is None:
        with _geocoder_lock:
            if _geocoder is None:
                _geocoder = NominatimGeocoder(
                    settings.address_search_url, settings.project_version
                )
    return _geocoder


def get_geocode_address_use_case(
    geocoder: GeocoderPort = Depends(get_geocoder),
) -> GeocodeAddressUseCase:
    return GeocodeAddressUseCase(geocoder=geocoder)


def get_device_repository(
    session: Session = Depends(get_db),
) -> DeviceRepository:
    """Devolve um repositório de dispositivos ligado à sessão da requisição."""
    return PostgresDeviceRepository(session)


def get_indexing_job_repository(
    session: Session = Depends(get_db),
) -> IndexingJobRepository:
    """Devolve um repositório de jobs ligado à sessão da requisição (RFC-029)."""
    return PostgresIndexingJobRepository(session)


def get_device_locator() -> DeviceLocator:
    """Devolve o localizador que responde "este disco está plugado, e onde".

    **Construído por requisição, e não pode ir para cache.** A resposta
    muda quando o usuário puxa um cabo e nada avisa este processo (RFC-027
    §7), então um cache aqui -- do tipo que `get_embedding_model` mantém
    legitimamente -- entregaria a um job uma letra de unidade que agora
    pertence a outro disco. O adaptador é uma casca fina sobre uma
    enumeração, então construir um não custa nada.

    Esse é também o motivo de a API rodar como processo no host e não num
    contêiner: `WindowsVolumeIdentityProvider` se recusa a ser construído
    fora de `win32`, e um contêiner não conseguiria responder a essa
    pergunta de jeito nenhum (RFC-029 §6).
    """
    return MountedDeviceLocator(WindowsVolumeIdentityProvider())


def get_create_indexing_job_use_case(
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
    device_repository: DeviceRepository = Depends(get_device_repository),
    device_locator: DeviceLocator = Depends(get_device_locator),
) -> CreateIndexingJobUseCase:
    """Devolve o caso de uso que valida um pedido de indexação e o enfileira."""
    return CreateIndexingJobUseCase(
        job_repository=job_repository,
        device_repository=device_repository,
        device_locator=device_locator,
    )


def get_cancel_indexing_job_use_case(
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
) -> CancelIndexingJobUseCase:
    return CancelIndexingJobUseCase(job_repository=job_repository)


def get_indexing_job_use_case(
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
) -> GetIndexingJobUseCase:
    return GetIndexingJobUseCase(job_repository=job_repository)


def get_list_indexing_jobs_use_case(
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
) -> ListIndexingJobsUseCase:
    return ListIndexingJobsUseCase(job_repository=job_repository)


def get_resolve_image_location_use_case(
    device_repository: DeviceRepository = Depends(get_device_repository),
    device_locator: DeviceLocator = Depends(get_device_locator),
) -> ResolveImageLocationUseCase:
    """Devolve o caso de uso que diz onde as imagens estão agora (RFC-030).

    Por requisição, como o localizador abaixo dele, e pelo mesmo motivo: a
    resposta só é verdadeira até alguém puxar um cabo.
    """
    return ResolveImageLocationUseCase(
        device_repository=device_repository,
        device_locator=device_locator,
    )


def get_image_details_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    location_resolver: ResolveImageLocationUseCase = Depends(
        get_resolve_image_location_use_case
    ),
) -> GetImageDetailsUseCase:
    """Devolve o caso de uso por trás de `GET /api/v1/images/{id}`.

    Os repositórios de imagens e de dispositivos compartilham a sessão
    única da requisição: o FastAPI resolve `get_db` uma vez por requisição,
    quantos provedores dependam dele.
    """
    return GetImageDetailsUseCase(
        repository=repository, location_resolver=location_resolver
    )


def get_thumbnail_store() -> ThumbnailStorePort:
    """Devolve o cache de thumbnails em disco, em `settings.thumbnail_directory`.

    Construí-lo não toca em nada -- o diretório é criado na primeira
    escrita, que a API nunca faz --, então um novo por requisição custa um
    `Path.resolve()`.
    """
    return FilesystemThumbnailStore(settings.thumbnail_directory)


def get_thumbnail_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    thumbnail_store: ThumbnailStorePort = Depends(get_thumbnail_store),
) -> GetThumbnailUseCase:
    """Devolve o caso de uso por trás de `GET /api/v1/images/{id}/thumbnail`.

    Sem localizador de dispositivo: uma thumbnail é servida do cache do
    aplicativo esteja ou não plugado o disco da foto (RFC-030 §4.1).
    """
    return GetThumbnailUseCase(repository=repository, thumbnail_store=thumbnail_store)


def get_file_revealer() -> FileRevealerPort:
    """Devolve o adaptador do Windows Explorer, o único que o RFC-030 entrega."""
    return WindowsFileRevealer()


def get_reveal_image_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    location_resolver: ResolveImageLocationUseCase = Depends(
        get_resolve_image_location_use_case
    ),
    file_revealer: FileRevealerPort = Depends(get_file_revealer),
) -> RevealImageUseCase:
    """Devolve o caso de uso por trás de `POST /api/v1/images/{id}/reveal`.

    Só é resolvido depois que as duas guardas passaram: a rota as declara
    no seu decorator, que o FastAPI resolve antes deste.
    """
    return RevealImageUseCase(
        repository=repository,
        location_resolver=location_resolver,
        file_revealer=file_revealer,
    )


def get_volume_catalog() -> VolumeCatalog:
    """Devolve o catálogo que responde "o que está plugado nesta máquina".

    **Construído por requisição, e não pode ir para cache.** A regra é a de
    `get_device_locator`, herdada palavra por palavra: a resposta muda
    quando o usuário puxa um cabo e nada avisa este processo (RFC-027 §7),
    então um cache aqui -- do tipo que `get_embedding_model` mantém
    legitimamente -- daria um disco como conectado minutos depois de ele
    sair. O adaptador é uma casca sobre uma enumeração que ele ainda não
    fez, então construir um custa uma referência.

    Separado de `get_device_locator()`, embora os dois envolvam o mesmo
    provedor, porque as duas portas respondem a perguntas diferentes: ao
    localizador se pergunta sobre um `Device` que o sistema conhece, e a
    este, sobre a máquina, incluindo volumes que ainda não são um
    dispositivo (RFC-031 §4.2).
    """
    return MountedVolumeCatalog(WindowsVolumeIdentityProvider())


def get_describe_devices_use_case(
    image_repository: ImageRepository = Depends(get_image_repository),
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
    volume_catalog: VolumeCatalog = Depends(get_volume_catalog),
) -> DescribeDevicesUseCase:
    """Devolve o caso de uso que resolve o que só é verdade agora (RFC-031 §4).

    Por requisição, como o catálogo abaixo dele, e pelo mesmo motivo: a
    resposta só é verdadeira até alguém puxar um cabo.
    """
    return DescribeDevicesUseCase(
        image_repository=image_repository,
        job_repository=job_repository,
        volume_catalog=volume_catalog,
    )


def get_list_devices_use_case(
    device_repository: DeviceRepository = Depends(get_device_repository),
    describer: DescribeDevicesUseCase = Depends(get_describe_devices_use_case),
) -> ListDevicesUseCase:
    """Devolve o caso de uso por trás de `GET /api/v1/devices`.

    Todo repositório do grafo compartilha a sessão única da requisição: o
    FastAPI resolve `get_db` uma vez por requisição, quantos provedores
    dependam dele, então as linhas de dispositivos, a contagem agrupada de
    imagens e as duas consultas de jobs são trabalho de uma conexão só.
    """
    return ListDevicesUseCase(device_repository=device_repository, describer=describer)


def get_list_mounted_volumes_use_case(
    volume_catalog: VolumeCatalog = Depends(get_volume_catalog),
    device_repository: DeviceRepository = Depends(get_device_repository),
) -> ListMountedVolumesUseCase:
    """Devolve o caso de uso por trás de `GET /api/v1/volumes` (RFC-031 §5)."""
    return ListMountedVolumesUseCase(
        volume_catalog=volume_catalog, device_repository=device_repository
    )


def get_register_device_use_case(
    volume_catalog: VolumeCatalog = Depends(get_volume_catalog),
    device_repository: DeviceRepository = Depends(get_device_repository),
) -> RegisterDeviceUseCase:
    """Devolve o caso de uso por trás de `POST /api/v1/devices` (RFC-031 §6).

    A mesma classe que a CLI alcança por
    `indexing_worker.register_device()`, composta de outro jeito: lá ela
    recebe um volume que a plataforma resolveu a partir de um caminho
    `--root`; aqui, ela procura uma identidade numa enumeração. Uma regra
    de mesclagem, dois caminhos de entrada (RFC-031 §4.2).
    """
    return RegisterDeviceUseCase(
        volume_catalog=volume_catalog, device_repository=device_repository
    )


def get_rename_device_use_case(
    device_repository: DeviceRepository = Depends(get_device_repository),
    describer: DescribeDevicesUseCase = Depends(get_describe_devices_use_case),
) -> RenameDeviceUseCase:
    """Devolve o caso de uso por trás de `PATCH /api/v1/devices/{id}` (RFC-031 §7).

    O describer está aqui para *informar* o estado do disco, não para
    exigi-lo: renomear funciona com o disco na gaveta, e o RFC-027 §2.3 diz
    por quê.
    """
    return RenameDeviceUseCase(device_repository=device_repository, describer=describer)


def get_purge_missing_images_use_case(
    image_repository: ImageRepository = Depends(get_image_repository),
    device_repository: DeviceRepository = Depends(get_device_repository),
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
    thumbnail_store: ThumbnailStorePort = Depends(get_thumbnail_store),
) -> PurgeMissingImagesUseCase:
    """Devolve o caso de uso de `DELETE /devices/{id}/missing-images` (RFC-046).

    Sem localizador e sem catálogo: a purga não precisa de nada do disco.
    """
    return PurgeMissingImagesUseCase(
        image_repository=image_repository,
        device_repository=device_repository,
        job_repository=job_repository,
        thumbnail_store=thumbnail_store,
    )


def get_list_device_folders_use_case(
    device_repository: DeviceRepository = Depends(get_device_repository),
    image_repository: ImageRepository = Depends(get_image_repository),
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
    device_locator: DeviceLocator = Depends(get_device_locator),
) -> ListDeviceFoldersUseCase:
    """Devolve o caso de uso por trás de `GET /api/v1/devices/{id}/folders`.

    O `DeviceLocator`, e não o `VolumeCatalog`, e a diferença é a razão de
    existirem duas portas: toda pergunta que esta rota faz é sobre um
    dispositivo que o sistema já conhece -- onde ele está, se este escopo
    está nele, o que há dentro desse escopo.
    """
    return ListDeviceFoldersUseCase(
        device_repository=device_repository,
        image_repository=image_repository,
        job_repository=job_repository,
        device_locator=device_locator,
    )


__all__ = [
    "get_aggregate_positions_use_case",
    "get_browse_images_use_case",
    "get_cancel_indexing_job_use_case",
    "get_create_indexing_job_use_case",
    "get_describe_devices_use_case",
    "get_device_locator",
    "get_device_repository",
    "get_embedding_model",
    "get_file_revealer",
    "get_find_similar_images_use_case",
    "get_geocode_address_use_case",
    "get_geocoder",
    "get_image_details_use_case",
    "get_image_repository",
    "get_index_image_use_case",
    "get_indexing_job_repository",
    "get_indexing_job_use_case",
    "get_list_device_folders_use_case",
    "get_list_devices_use_case",
    "get_list_indexing_jobs_use_case",
    "get_list_mounted_volumes_use_case",
    "get_purge_missing_images_use_case",
    "get_register_device_use_case",
    "get_rename_device_use_case",
    "get_resolve_image_location_use_case",
    "get_reveal_image_use_case",
    "get_search_images_use_case",
    "get_thumbnail_store",
    "get_thumbnail_use_case",
    "get_volume_catalog",
]
