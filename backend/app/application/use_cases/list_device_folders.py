r"""Um nível da árvore de um dispositivo, e o que sabemos de cada pasta (RFC-031 §8).

A tela em que o usuário escolhe o que indexar. Devolve as pastas diretamente
dentro de um caminho, cada uma com a contagem exata de imagens indexadas e uma
de cinco palavras descrevendo o seu estado -- e devolve **o que o cliente vai
mandar de volta**, porque o que isto lista vira `scopes[]` no próximo
`POST /api/v1/jobs`.

Essa última frase é a restrição em torno da qual o módulo inteiro é
construído, e ela tem duas consequências:

* **a mesma validação que o `POST /jobs` aplica, aplicada aqui.** `JobScope`
  recusa `..`, uma letra de unidade e um compartilhamento UNC sem tocar no
  disco; `DeviceLocator.resolve_scope()` então confirma que o resultado está
  de fato dentro do dispositivo depois de resolvidas as junctions NTFS. Uma
  pasta que pudesse ser listada mas não virar escopo seria um beco sem saída
  na interface (RFC-031 §8);
* **os caminhos ecoados são a grafia do sistema de arquivos, nunca a do
  cliente.** `resolve_scope()` devolve a canônica, que é metade do motivo de
  ele existir -- o Windows não diferencia maiúsculas de minúsculas, enquanto
  `normalize_scopes()` compara as partes exatamente, então um cliente que
  pediu `FOTOS/2018` e recebeu de volta a própria grafia a mandaria ao
  `POST /jobs` e a veria reescrita em outra coisa.

**Um nível por requisição, nem a árvore inteira nem uma lista plana.** Um
primeiro nível plano transforma "indexar o casamento" em "indexar todo o
2018", que são cinco horas de inferência para chegar a trezentas fotos; a
árvore inteira custa o tamanho do disco a cada vez que a tela abre, para
desenhar quatro linhas (RFC-031 §8.1).

**Nenhuma porcentagem em lugar nenhum, e nenhuma contagem de arquivos no
disco.** Não há denominador por pasta, e obter um significa percorrer a
subárvore, que é a leitura que o RFC-029 §7.2 mediu como dominante num disco
mecânico a frio. `indexed_images` é, em vez disso, um fato exato sobre a nossa
própria tabela: *"1.204 indexadas"* é verdade, *"30% indexada"* não tem do que
ser 30% (RFC-031 §2.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import DeviceNotConnectedError, DeviceNotFoundError
from app.domain.exceptions.job_errors import InvalidJobScopeError
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.folder_state import FolderState
from app.domain.value_objects.job_scope import JobScope


@dataclass(frozen=True)
class FolderListing:
    """Uma pasta, como uma linha no seletor."""

    scope: JobScope
    """A pasta como escopo relativo ao dispositivo, na grafia do sistema de arquivos.

    Um `JobScope`, e não uma string, porque é isso que o cliente vai mandar de
    volta, e porque é o que `contains()` compara -- por partes, não por texto,
    então `2018` não é prefixo de `2018b`.
    """

    has_children: bool
    """Se esta pasta tem pastas dentro dela.

    Lido do disco pelo localizador, que paga um `readdir` a mais por linha
    para isso. Sem ele, a interface desenha uma seta de expandir numa folha e
    o usuário descobre o erro clicando (RFC-031 §4.4 do build prompt).
    """

    indexed_images: int
    """Linhas de `images` em qualquer ponto abaixo desta pasta. Exato, não razão."""

    state: FolderState
    job: IndexingJob | None
    """O job de que o estado foi lido, ou `None` quando nenhum job o explica.

    Viaja com a linha porque os estados interessantes são aqueles em que o job
    traz o detalhe: o `last_processed_relative_path` de um job cancelado é
    *onde ele parou*, que é o que o RFC-031 §12 põe no lugar da porcentagem
    inventada do mockup.
    """

    @property
    def name(self) -> str:
        """O nome da própria pasta -- a última parte do seu escopo."""
        return self.scope.parts[-1]


@dataclass(frozen=True)
class DeviceFolders:
    """Um nível da árvore de um dispositivo: onde estamos, e o que há aqui."""

    device: Device
    scope: JobScope
    """A pasta listada, na grafia canônica. Vazio é a raiz do dispositivo."""

    folders: list[FolderListing]

    @property
    def parent(self) -> JobScope:
        """Um nível acima; o pai da raiz é a raiz.

        Derivado de `parts`, e não aparando texto, pelo mesmo motivo de
        `JobScope` guardar partes: cirurgia de string num caminho é como
        `2018` vira prefixo de `2018b`.
        """
        return JobScope(self.scope.parts[:-1])


class ListDeviceFoldersUseCase:
    """Lista um nível das pastas de um dispositivo, cada uma com o seu estado."""

    def __init__(
        self,
        device_repository: DeviceRepository,
        image_repository: ImageRepository,
        job_repository: IndexingJobRepository,
        device_locator: DeviceLocator,
    ) -> None:
        self._devices = device_repository
        self._images = image_repository
        self._jobs = job_repository
        self._locator = device_locator

    def execute(self, device_id: DeviceId, path: str = "") -> DeviceFolders:
        """Lista as pastas dentro de `path` em `device_id`.

        As quatro recusas, nesta ordem, cada uma com um erro de domínio que a
        camada HTTP já sabe responder:

        * um dispositivo que ninguém nunca viu -- 404;
        * um dispositivo que não está plugado *agora* -- 409, e a mensagem
          nomeia o disco. Ao contrário da rota de renomear, esta precisa de
          bytes, e *qual disco plugar* é mais útil do que ouvir que a pasta
          não está lá;
        * um escopo que escapa do dispositivo, ou que não é uma pasta dele --
          400, recusado por `JobScope` antes de tocar no disco e depois pelo
          localizador, com as junctions resolvidas;
        * uma pasta que sumiu entre ser resolvida e ser lida -- 400 também,
          pela mesma regra: não é uma pasta deste dispositivo.

        `path` ausente, vazio ou `.` listam todos a raiz do dispositivo,
        porque é o que `JobScope("")` já decide -- `PurePosixPath` descarta o
        `.` na normalização. De propósito, não há um segundo tratamento disso
        aqui.

        Custo: um `SELECT` para o dispositivo, três enumerações dos volumes
        montados (conexão, resolução, listagem -- 0,19 ms cada), um `readdir`
        por pasta mais um para o próprio nível, **uma** consulta de contagem
        agrupada para todas as pastas de uma vez, e um `SELECT` para o
        histórico de jobs.
        """
        device = self._devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"No device with id {device_id}.")

        if self._locator.mount_point(device) is None:
            raise DeviceNotConnectedError(
                f"Device {device.label!r} is not connected. Plug it in and "
                "try again."
            )

        resolved = self._locator.resolve_scope(device, JobScope(path))
        if resolved is None:
            raise InvalidJobScopeError(
                f"{path!r} is not a folder on device {device.label!r}."
            )

        entries = self._locator.list_folders(device, resolved)
        if entries is None:
            raise InvalidJobScopeError(
                f"{path!r} is not a folder on device {device.label!r}."
            )

        indexed = self._images.count_by_path_prefixes(device.id, resolved)
        history = self._jobs.list(device_id=device.id)

        folders = [
            self._describe(
                scope=JobScope(resolved.parts + (entry.name,)),
                has_children=entry.has_children,
                # `.get(..., 0)` em vez de um acesso direto: uma pasta sem
                # imagem indexada não tem grupo na contagem, e ler o
                # mapeamento diretamente descartaria a pasta ou levantaria
                # exceção. Zero é uma resposta de verdade aqui, e é a
                # resposta para a maioria das pastas de um disco novo.
                indexed_images=indexed.get(entry.name, 0),
                history=history,
            )
            for entry in entries
        ]
        # Ordenado por nome **aqui**, na camada que sabe que isto vai ser
        # exibido. `scandir` não tem ordem a prometer, nem a porta; esta
        # também não é a ordem do scan, que `FilesystemImageProvider`
        # estabelece com a comparação de caminhos da própria plataforma, e
        # as duas não podem ser confundidas.
        folders.sort(key=lambda folder: folder.name)
        return DeviceFolders(device=device, scope=resolved, folders=folders)

    def _describe(
        self,
        scope: JobScope,
        has_children: bool,
        indexed_images: int,
        history: list[IndexingJob],
    ) -> FolderListing:
        state, job = _state_of(scope, history, indexed_images)
        return FolderListing(
            scope=scope,
            has_children=has_children,
            indexed_images=indexed_images,
            state=state,
            job=job,
        )


def _state_of(
    folder: JobScope, history: list[IndexingJob], indexed_images: int
) -> tuple[FolderState, IndexingJob | None]:
    """Lê o estado de uma pasta no histórico de jobs do dispositivo (RFC-031 §8.2).

    Percorre do mais novo para o mais antigo -- o que
    `IndexingJobRepository.list()` garante por contrato -- e para no primeiro
    job que cobre a pasta de algum modo. Isso é correto, e não só
    conveniente: no máximo um job por dispositivo está ativo (o índice único
    parcial do RFC-029 §9), e um job só pode ser criado quando nenhum está
    ativo, então um job ativo é sempre o mais novo. Não há caso em que um job
    em execução mais antigo deva passar à frente de um terminado mais novo.

    "Cobrir" é uma relação de prefixo **nas duas direções**, e as duas
    direções significam coisas diferentes:

    * um job sobre `2018` contém `2018/janeiro` -- cobertura total, então o
      resultado do próprio job é o estado da pasta;
    * um job sobre `2018/janeiro/casamento` fica *dentro* de `2018/janeiro` --
      cobertura parcial, então mesmo um job `completed` deixa a pasta
      `partial`, que é literalmente o que ela é.

    Toda comparação passa por `JobScope.contains()`, que compara partes.
    Fazê-la em texto tornaria `2018` prefixo de `2018b`, que é outra pasta.

    `scopes == ()` significa o dispositivo inteiro e contém tudo, o que sai da
    mesma regra de prefixo: uma tupla vazia é prefixo de toda tupla.

    **Nenhum `list_covering()` foi acrescentado à porta de jobs para isto.** O
    RFC-031 §13 propôs um; escrevê-lo significaria reproduzir esta relação em
    SQL, nas duas direções, em duas implementações, com a comparação feita em
    texto -- que é exatamente o erro que `JobScope` existe para evitar. A
    regra já está escrita, já está testada e já é domínio. O custo declarado
    é que o histórico de um disco cresce sem limite, e esta lista cresce com
    ele; a mitigação, quando isso começar a importar, é um `limit` na porta,
    medido antes, e não um casamento de prefixo em SQL (RFC-031 §4.9 do build
    prompt).
    """
    for job in history:
        fully = _covers_fully(job, folder)
        if not (fully or _covers_partially(job, folder)):
            continue
        if job.status is JobStatus.RUNNING:
            return FolderState.INDEXING, job
        if job.status is JobStatus.PENDING:
            return FolderState.QUEUED, job
        if job.status is JobStatus.COMPLETED:
            return (FolderState.INDEXED if fully else FolderState.PARTIAL), job
        # `cancelled` ou `failed`: parte foi percorrida, e nada é dado como
        # terminado. O job volta com a linha porque o seu checkpoint é *onde
        # ele parou*, que é a coisa verdadeira e útil que o "Parcial · 30%"
        # do mockup tentava dizer.
        return FolderState.PARTIAL, job

    # Nenhum job do histórico cobre esta pasta. `never_indexed` exige as duas
    # metades do RFC-031 §8.2 -- nenhum job que cubra **e** nenhuma imagem
    # indexada --, então linhas sem job que as explique são `partial`: elas
    # existem, a busca já pode devolvê-las, e dizer "nunca indexada" sobre
    # elas seria falso.
    if indexed_images == 0:
        return FolderState.NEVER_INDEXED, None
    return FolderState.PARTIAL, None


def _covers_fully(job: IndexingJob, folder: JobScope) -> bool:
    """Se todo arquivo sob `folder` está dentro de um dos escopos do job."""
    if not job.scopes:
        return True
    return any(scope.contains(folder) for scope in job.scopes)


def _covers_partially(job: IndexingJob, folder: JobScope) -> bool:
    """Se um dos escopos do job está dentro de `folder` -- parte dela, não toda."""
    return any(folder.contains(scope) for scope in job.scopes)
