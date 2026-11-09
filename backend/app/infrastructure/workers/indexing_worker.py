"""Worker de indexação do sistema de arquivos, que orquestra descoberta e indexação.

Também o ponto de entrada de CLI do worker prometido pelo `AI_Context.md`:

    python -m app.infrastructure.workers.indexing_worker --root PATH

O `AI_Context.md` escreve esse caminho como
`infrastructure.workers.indexing_worker`, sem o prefixo `app.`. Essa forma nunca
foi executável -- o pacote é `app.infrastructure`, e o `pyproject.toml` põe
`backend/` (não `backend/app/`) no `pythonpath` --, então o caminho documentado
foi corrigido para bater com o código, em vez de o código ser remodelado para
bater com a documentação (RFC-024 §12).
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    IndexingSummary,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.register_device import RegisterDeviceUseCase
from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.entities.indexing_job import IndexingJob
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.filesystem.volume_catalog import (
    MountedVolumeCatalog,
    as_mounted_volume,
)
from app.infrastructure.filesystem.volume_identity_provider import (
    ResolvedVolume,
    VolumeIdentityProvider,
)
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


class IndexingWorker:
    """Descobre arquivos de imagem e conduz a indexação incremental de todos eles.

    Um componente de orquestração com dependências injetadas: não constrói o
    próprio provedor de sistema de arquivos nem o caso de uso, e nunca toca
    diretamente o SQLAlchemy, o pgvector ou os detalhes internos do modelo de
    embedding.

    O RFC-024 levou o `try/except` por arquivo que morava em `run()` para o
    caso de uso, porque é no lote que o isolamento de fato corre risco -- um
    lote que falha como unidade precisa ser tentado de novo uma imagem por vez,
    e só o componente que monta os lotes pode fazer isso. O que fica aqui é a
    guarda em volta da construção da própria entidade de domínio, que acontece
    antes de um arquivo virar candidato, e o log de tudo o que a execução
    reporta de volta.

    O RFC-027 acrescentou `device` e `mount_point`, os dois resolvidos pela
    raiz de composição *antes* de o scan começar, em vez de consultados por
    arquivo. Resolver uma vez só não é otimização: se o disco fosse
    identificado por arquivo, um volume que sumisse no meio da execução
    produziria metade de um scan atribuída a um dispositivo e metade atribuída
    a nada, e os ids gerados nas duas metades discordariam sobre os mesmos
    arquivos.

    `mount_point` é passado junto com o dispositivo em vez de lido dele
    porque um `Device` de propósito não tem esse campo -- onde um volume está
    montado é um fato deste instante, e persisti-lo é o defeito que o RFC-027
    existe para remover (RFC-027 §4).
    """

    def __init__(
        self,
        filesystem_provider: FilesystemImageProvider,
        index_or_update_images_use_case: IndexOrUpdateImagesUseCase,
        device: Device,
        mount_point: Path,
    ) -> None:
        self._filesystem_provider = filesystem_provider
        self._index_or_update_images_use_case = index_or_update_images_use_case
        self._device = device
        self._mount_point = mount_point

    def run(self) -> IndexingSummary:
        """Descobre e indexa de forma incremental cada arquivo de imagem suportado.

        Devolve os contadores e os tempos da execução para que quem chama --
        um teste, um benchmark, um futuro agendador -- possa verificá-los em
        vez de interpretar a saída do log. As falhas vão para o log aqui, e não
        dentro do caso de uso, o que mantém a fábrica de logging um assunto da
        Infraestrutura.
        """
        logger.info("Indexing started")

        undiscoverable: list[IndexingFailure] = []
        summary = self._index_or_update_images_use_case.execute(
            self._candidates(undiscoverable)
        )

        summary.discovered += len(undiscoverable)
        summary.failures.extend(undiscoverable)

        for fallback in summary.inference_fallbacks:
            logger.warning(
                "Inference batch of %d failed and was retried one image at a "
                "time: %s",
                len(fallback.paths),
                fallback.error,
            )

        for fallback in summary.persistence_fallbacks:
            logger.warning(
                "Bulk write of %d rows failed and degraded to per-row: %s",
                len(fallback.paths),
                fallback.error,
            )

        for failure in summary.failures:
            logger.error(
                "Failed to index %s",
                failure.path,
                exc_info=failure.error,
            )

        logger.info("%s", summary.format_report())
        return summary

    def _candidates(
        self, undiscoverable: list[IndexingFailure]
    ) -> Iterator[IndexCandidate]:
        """Entrega os arquivos descobertos como candidatos da camada de Aplicação.

        Um gerador, não uma lista: o caso de uso o consome de forma preguiçosa,
        então descoberta, hash, inferência e persistência se intercalam e o
        pipeline nunca segura a coleção inteira em memória.

        Construir `ImagePath`/`ImageId` pode rejeitar um caminho de saída, e um
        arquivo assim não pode encerrar a execução. Essas falhas são coletadas
        em vez de levantadas, porque levantar exceção de dentro de um gerador o
        fecharia e truncaria o scan em silêncio naquele arquivo. Desde o
        RFC-027, a mesma guarda cobre também `relative_to()`, que levanta
        exceção para um arquivo descoberto que, por algum motivo, não está sob
        o ponto de montagem que a execução resolveu.

        Os dois caminhos vão para a entidade, e não são redundantes.
        `relative_path` é a metade durável, que é persistida e entra no hash do
        id; `absolute_path` é onde o arquivo está enquanto este disco está
        conectado, para que o hasher e o modelo possam abri-lo, e nunca é
        gravado no banco (RFC-027 §5.2).

        A data de captura que o scan leu também vai para a entidade (RFC-028
        §6), para que o caso de uso possa gravá-la numa linha que ele pula -- e
        a posição também (RFC-032 §8), pelo mesmo motivo.
        """
        yield from discovered_candidates(
            self._filesystem_provider, self._device, self._mount_point, undiscoverable
        )


def listed_identity(
    device_id: DeviceId, relative: str | Path
) -> tuple[ImagePath, ImageId]:
    """O caminho gravado e o id de um arquivo listado num dispositivo (RFC-046 §4).

    **A única tradução de um arquivo listado para um `ImageId`**, chamada por
    `discovered_candidates()` para cada arquivo que ela entrega à indexação e
    pela varredura do job runner para cada arquivo da listagem de um disco
    inteiro. A varredura marca uma linha como ausente quando o id dela não está
    entre os da listagem, então uma segunda cópia disto -- uma que
    normalizasse um separador ou uma caixa de outro jeito -- marcaria como
    ausente uma foto que está no disco. `relative` é relativo ao ponto de
    montagem, como caminho ou como o texto posix de `RootListing`.
    """
    relative_path = ImagePath(relative)
    return relative_path, compute_image_id(device_id, relative_path)


def discovered_candidates(
    filesystem_provider: FilesystemImageProvider,
    device: Device,
    mount_point: Path,
    undiscoverable: list[IndexingFailure],
) -> Iterator[IndexCandidate]:
    """Faz dos arquivos descobertos candidatos de um dispositivo; ver `_candidates()`.

    Uma função de módulo em vez de um método privado para que `exif_backfill`
    gere os mesmos ids a partir dos mesmos arquivos descobertos. O backfill
    grava em linhas que o worker de indexação criou, então uma segunda cópia
    desta tradução seria uma segunda chance de calcular um `ImageId` diferente
    para o mesmo arquivo -- e de não preencher nada.
    """
    for discovered in filesystem_provider.discover():
        logger.info("Discovered file: %s", discovered.path)
        try:
            absolute_path = ImagePath(str(discovered.path))
            relative_path, image_id = listed_identity(
                device.id, discovered.path.relative_to(mount_point)
            )
            capture = discovered.capture_date
            position = discovered.position
            image = Image(
                id=image_id,
                device_id=device.id,
                relative_path=relative_path,
                filename=discovered.filename,
                extension=discovered.extension,
                absolute_path=absolute_path,
                captured_at=capture.captured_at if capture else None,
                capture_source=capture.source if capture else None,
                latitude=position.latitude if position else None,
                longitude=position.longitude if position else None,
                position_source=position.source if position else None,
            )
        except Exception as exc:
            undiscoverable.append(IndexingFailure(path=str(discovered.path), error=exc))
            continue

        yield IndexCandidate(
            image=image,
            file_size=discovered.file_size,
            file_modified_at=discovered.file_modified_at,
        )


def register_device(
    volume_provider: VolumeIdentityProvider,
    device_repository: DeviceRepository,
    root: Path,
    label: str,
    persist: bool = True,
) -> tuple[Device, ResolvedVolume]:
    """Identifica o volume que contém `root` e garante a linha do dispositivo dele.

    Roda uma vez por invocação, antes de um único arquivo ser descoberto. Essa
    ordem é o que o RFC-027 §14 quer dizer com "resolve o dispositivo antes do
    scan", e não é mera arrumação: todo `ImageId` que a execução gera é
    `uuid5` sobre `f"{device_id}/{relative_path}"`, então o dispositivo
    precisa estar fixado antes de o primeiro id poder ser calculado, e a chave
    estrangeira em `images.device_id` obriga a linha dele a estar commitada
    antes de a primeira imagem ser gravada.

    Devolve também o `ResolvedVolume`, porque a execução precisa do ponto de
    montagem para transformar os caminhos absolutos descobertos em caminhos
    relativos ao dispositivo -- e o ponto de montagem é exatamente o que um
    `Device` se recusa a carregar.

    Um dispositivo já conhecido mantém o `first_seen_at` e, a menos que
    `--label` tenha sido dado, o rótulo. O `last_seen_at` anda a cada
    execução, o que faz dele histórico, e não estado.

    `persist=False` calcula o dispositivo sem gravá-lo, que é do que o
    `device_reconcile --dry-run` precisa: um dry run que criasse uma linha não
    seria um dry run, e o id do dispositivo é derivado, não alocado, então
    nada precisa ser gravado para que ele seja conhecido.

    **Desde o RFC-031, a própria regra de mesclagem mora na camada de
    Aplicação**, em `RegisterDeviceUseCase`, porque uma rota HTTP também
    precisa dela, e uma rota não pode alcançar `app.infrastructure.workers`. O
    que sobra aqui é a metade que de fato pertence abaixo da linha: perguntar à
    plataforma *qual volume contém este caminho*, uma pergunta que só um
    `--root` tem e que a rota substitui por uma consulta numa enumeração.

    **A assinatura não mudou, e isso é essencial.** Seis chamadores passam
    `root` e `label` e leem a tupla de volta -- `indexing_worker.main()`,
    `device_reconcile` (com `persist=False`), os dois backfills,
    `dataset_tools/seed_demo.py` e o script de medição do RFC-029 -- e o
    `test_cli_job_equivalence.py` é a rede embaixo da mudança. Uma refatoração
    que exigisse tocar em qualquer um deles seria a refatoração errada.

    Compartilhada em vez de escrita duas vezes, pelo motivo de sempre: as
    regras sobre quais campos sobrevivem numa linha existente são do tipo que
    se desencontra em duas cópias, e todo chamador gera ids de imagem a partir
    do dispositivo que ela produz.
    """
    volume = volume_provider.resolve(root)
    device = RegisterDeviceUseCase(
        volume_catalog=MountedVolumeCatalog(volume_provider),
        device_repository=device_repository,
    ).register(as_mounted_volume(volume), label=label, persist=persist)
    return device, volume


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.workers.indexing_worker",
        description="Index every supported image under ROOT into PostgreSQL.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help=(
            "Directory to scan. Required on purpose: there is no default "
            "and no setting to take one from, so that running this "
            "command can never start indexing a real photo collection "
            "nobody asked it to touch (same reasoning as "
            "dataset_tools/seed_demo.py)."
        ),
    )
    parser.add_argument(
        "--label",
        default="",
        help=(
            "User-facing name for the disk ROOT lives on -- 'HD2'. Used only "
            "when this volume is being registered for the first time; a "
            "device already known keeps the label it has, because renaming a "
            "disk is a decision for the user rather than a side effect of "
            "indexing it again. Falls back to the volume's own filesystem "
            "label, which is frequently 'Untitled' or empty -- hence the "
            "option."
        ),
    )
    return parser


def scope_for(root: Path, mount_point: Path) -> JobScope:
    """Transforma a raiz de um scan no escopo relativo ao dispositivo que diz o mesmo.

    **A correção de que o RFC-029 §12 precisava.** O rascunho dizia que a CLI
    vira "um job com escopo vazio, o dispositivo inteiro" -- mas
    `--root D:\fotos\2018` nunca indexou um disco inteiro, indexa uma pasta.
    Um escopo vazio teria transformado uma refatoração numa mudança de
    comportamento medida em horas.

    O vazio só sai quando a raiz *é* o ponto de montagem, o único caso em que
    "esta pasta" e "este disco" são o mesmo pedido.
    """
    relative = root.relative_to(mount_point)
    return JobScope(relative)


def follow(
    jobs: IndexingJobRepository,
    job_id: JobId,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> IndexingJob | None:
    """Acompanha um job que outro está rodando, até ele parar de andar.

    Só se chega aqui quando um executor separado reivindicou o job primeiro.
    Isso não é falha nem motivo para sair: o usuário pediu que uma pasta fosse
    indexada, ela está sendo indexada, e o comando reporta o mesmo desfecho de
    um jeito ou de outro (RFC-029 §12).
    """
    while True:
        job = jobs.get(job_id)
        if job is None or job.status.is_terminal:
            return job
        sleep(poll_interval)


def run(root: Path, label: str) -> None:
    """Cria um job para `root` e o roda neste mesmo processo.

    A raiz de composição -- o único lugar autorizado a conhecer todas as
    classes concretas de uma vez. Duas decisões aqui são correções do RFC-029,
    e as duas tratam de não deixar este comando pior do que era:

    * `--root` vira um **escopo**, não o dispositivo inteiro (`scope_for()`);
    * o job é executado **aqui**, reivindicado com o mesmo `UPDATE` atômico
      que o laço de polling usa. O RFC-029 §12 aceitou que "a CLI passa a
      exigir o worker rodando"; isso faria de `indexing_worker --root PATH`
      um comando que espera para sempre em silêncio sempre que ninguém
      iniciou um executor. Não exige um segundo processo, e continua havendo
      exatamente um caminho de código de indexação -- isto chama
      `JobRunner.run_job()`, que é o que o executor chama.

    Se um executor *está* rodando e reivindica o job primeiro, isto o
    acompanha por polling e reporta o mesmo desfecho.

    Extraída de `main()` para que o subcomando `index` do `app.cli` possa
    chamar a mesma composição depois de interpretar os próprios argumentos,
    sem uma segunda cópia desta fiação.

    Os imports dos provedores ficam dentro da função, e não no nível do
    módulo, para que importar `IndexingWorker` nunca arraste a Apresentação,
    e por ela o adaptador CLIP, para um grafo de imports da Infraestrutura. A
    maior parte da suíte de testes importa este módulo; pagar um import do
    torch por uma CLI que não está rodando seria um custo real, não hipotético.

    A presença dos modelos é verificada antes de qualquer coisa ser registrada
    ou enfileirada (RFC-035 §6.1): sem eles não há nada que este comando possa
    fazer, e ele diz isso numa linha (`main()`) em vez de deixar para trás um
    job que nenhum modelo vai rodar.
    """
    from app.infrastructure.ai.model_runtime import default_runtime

    default_runtime().check_presence()

    from app.application.use_cases.create_indexing_job import CreateIndexingJobUseCase
    from app.infrastructure.config.settings import settings
    from app.infrastructure.filesystem.mounted_device_locator import (
        MountedDeviceLocator,
    )
    from app.infrastructure.filesystem.volume_identity_provider import (
        WindowsVolumeIdentityProvider,
    )
    from app.infrastructure.persistence.postgres_device_repository import (
        PostgresDeviceRepository,
    )
    from app.infrastructure.persistence.postgres_indexing_job_repository import (
        PostgresIndexingJobRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal
    from app.infrastructure.workers.job_runner import build_runner

    job_session = SessionLocal()
    image_session = SessionLocal()
    try:
        volume_provider = WindowsVolumeIdentityProvider()
        devices = PostgresDeviceRepository(job_session)
        device, volume = register_device(
            volume_provider=volume_provider,
            device_repository=devices,
            root=root,
            label=label,
        )
        scope = scope_for(root, volume.mount_point)
        logger.info(
            "Indexing %s on device %s (%s), mounted at %s",
            scope or "the whole device",
            device.label,
            device.id,
            volume.mount_point,
        )

        jobs = PostgresIndexingJobRepository(job_session)
        job = CreateIndexingJobUseCase(
            job_repository=jobs,
            device_repository=devices,
            device_locator=MountedDeviceLocator(volume_provider),
        ).execute(device.id, () if scope.is_whole_device else (str(scope),))
        logger.info("Created indexing job %s", job.id)

        runner = build_runner(job_session, image_session)
        runner.warm_up_now()
        finished = runner.run_job(job.id)
        if finished is None:
            logger.info("Another executor claimed job %s; following it", job.id)
            finished = follow(jobs, job.id, settings.job_poll_interval)

        if finished is None:
            logger.error("Job %s disappeared before it finished", job.id)
            return
        logger.info(
            "Job %s finished as %s: %d indexed, %d skipped, %d failed",
            finished.id,
            finished.status.value,
            finished.progress.processed_images,
            finished.progress.skipped_images,
            finished.progress.failed_images,
        )
        if finished.error_message:
            logger.error("%s", finished.error_message)
    finally:
        image_session.close()
        job_session.close()


def main() -> None:
    """Interpreta `sys.argv` e roda o comando, como ponto de entrada independente.

        python -m app.infrastructure.workers.indexing_worker --root PATH

    O subcomando `index` do `app.cli` interpreta os próprios argumentos do
    mesmo jeito e chama `run()` direto; esta função existe para que o módulo
    continue funcionando sozinho como script.

    Um modelo que não pode ser carregado é uma linha no stderr (RFC-035 §6.5),
    não um traceback, e o código de saída 78: rodar o comando de novo não vai
    adiantar (RFC-036 §6.1).
    """
    from app.infrastructure.ai.model_runtime import ModelUnavailableError
    from app.infrastructure.process.supervision import refuse_to_start

    args = _build_arg_parser().parse_args()
    try:
        run(args.root.resolve(), args.label)
    except ModelUnavailableError as error:
        refuse_to_start(str(error))


if __name__ == "__main__":
    main()
