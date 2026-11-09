"""Anexar a cada dispositivo o que só é verdade agora (RFC-031 §4).

Quatro rotas precisam da mesma decoração -- `GET /devices` para todos os
discos, `POST /devices` e `PATCH /devices/{id}` para o que acabaram de tocar --,
e nenhuma delas pode calculá-la:

* não a rota, porque "este disco está plugado, quantas fotos dele estão
  indexadas, e há algo rodando nele" é exatamente o tipo de regra que o
  `AI_Context.md` mantém fora da apresentação;
* não `ListDevicesUseCase`, porque decorar um dispositivo não é listar
  dispositivos. Colocar isso lá dentro deixaria as rotas de cadastro e de
  renomear duplicando a lógica ou respondendo `connected: false` para um
  disco que tinham acabado de achar montado -- o que não é simplificação, é
  dado errado.

Então é um pequeno caso de uso próprio, no modelo de
`ResolveImageLocationUseCase`, até o par `execute()` / `execute_one()`: a forma
plural é a real, e a singular é o seu caso de um elemento, para que quem chama
com um dispositivo não possa, sem querer, seguir um caminho mais barato e
diferente.

**Nada aqui é persistido e nada aqui vai para cache.** `mount_point` vive no
resumo devolvido pela duração de uma requisição. A resposta está errada desde o
momento em que o usuário puxa o cabo, e nada avisa este processo quando isso
acontece (RFC-027 §7).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.repositories.image_repository import ImageRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.volume_catalog import VolumeCatalog
from app.domain.value_objects.device_id import DeviceId

ACTIVE_STATUSES: tuple[JobStatus, ...] = (JobStatus.RUNNING, JobStatus.PENDING)
"""Exatamente os estados que `JobStatus.is_active` nomeia, como plano de consulta.

Dois `SELECT`s que não crescem com o número de dispositivos, em vez de um filtro
por dispositivo. Listados aqui, e não derivados no lugar, para que
`test_describe_devices.py` possa afirmar que esta tupla *é*
`{status for status in JobStatus if status.is_active}` -- o conjunto é
compartilhado com o índice único parcial do RFC-029 §9, e um estado acrescentado
a um e não ao outro é uma divergência que nada mais pegaria.

Em execução primeiro. O índice permite no máximo um job ativo por dispositivo,
então a ordem não pode importar hoje; ela fica escrita como o desempate honesto,
em vez de ficar a cargo de qual consulta rodou primeiro.
"""


@dataclass(frozen=True)
class DeviceSummary:
    """Um disco, e tudo o que era verdade sobre ele quando perguntamos."""

    device: Device

    mount_point: Path | None
    """Onde o disco está conectado agora, ou `None` para um que está na gaveta.

    Resolvido a cada requisição e nunca guardado -- a distinção que o RFC-027
    §4 faz, e o motivo de `Device` não ter esse campo.
    """

    indexed_images: int
    """Quantas linhas `images` tem para este dispositivo.

    Desde o RFC-046, só as **visíveis**: uma linha que o último scan não achou
    não é contada, então o numerador da porcentagem é o que a busca consegue
    achar (§5.8). As marcadas são `missing_images`.

    Uma das três partes do "% indexado". A API devolve as partes e nunca a
    porcentagem: um campo `percent_indexed` poderia ser consumido sem
    `last_scan_at` ao lado, e o primeiro cliente a exibi-lo sozinho traria de
    volta o número confiantemente errado que o ARCHITECTURE.md §15 proíbe
    (RFC-031 §4.2).
    """

    active_job: IndexingJob | None
    """O job `pending` ou `running` que segura este disco, se houver."""

    missing_images: int = 0
    """Quantas linhas os últimos scans do disco inteiro não acharam (RFC-046)."""

    missing_since: datetime.datetime | None = None
    """A mais antiga das marcas: o scan que o cartão nomeia. `None` sem marcas."""

    @property
    def connected(self) -> bool:
        """Se o disco estava montado quando este resumo foi montado.

        Derivado de `mount_point` em vez de guardado ao lado dele, para que os
        dois não possam discordar: "conectado sem estar em lugar nenhum" não é
        um estado que este tipo consegue representar.
        """
        return self.mount_point is not None


class DescribeDevicesUseCase:
    """Resolve, para um conjunto de dispositivos, tudo o que só é verdade agora."""

    def __init__(
        self,
        image_repository: ImageRepository,
        job_repository: IndexingJobRepository,
        volume_catalog: VolumeCatalog,
    ) -> None:
        self._images = image_repository
        self._jobs = job_repository
        self._catalog = volume_catalog

    def execute(self, devices: Sequence[Device]) -> list[DeviceSummary]:
        """Devolve um resumo por dispositivo, na ordem recebida.

        **O sistema de arquivos é enumerado exatamente uma vez, seja qual for
        N**, e cada uma das consultas também. Esse limite é o desenho, não uma
        escolha de ajuste (RFC-031 §4.1): uma lista de dispositivos tem, por
        definição, N dispositivos *distintos*, então o agrupamento que o
        RFC-030 abençoou uma camada acima -- `ResolveImageLocationUseCase`
        perguntando ao localizador uma vez por disco distinto numa página de
        busca -- degenera aqui em N enumerações do mesmo sistema de arquivos.
        Por isso `DeviceLocator` nem é usado: todo método dele recebe um
        `Device`.

        A mesma armadilha está sob as outras colunas, e é mais fácil de não
        ver. Um `COUNT(*)` por dispositivo são N consultas, e
        `IndexingJobRepository.list(device_id=...)` por dispositivo são mais N.
        As duas são respondidas pedindo tudo uma vez e agrupando em memória --
        e assim também as fotos ausentes do RFC-046, mais uma leitura agrupada,
        com a contagem e a marca mais antiga juntas.

        A ordem e o tamanho de `devices` são preservados exatamente, para que
        quem os ordenou mantenha a sua ordem -- e para que quem tem um
        dispositivo receba um resumo de volta.
        """
        mounted = self._catalog.mount_points()
        indexed = self._images.count_by_device()
        missing = self._images.count_missing_by_device()
        active = self._active_jobs()

        summaries = []
        for device in devices:
            marked = missing.get(device.id)
            summaries.append(
                DeviceSummary(
                    device=device,
                    mount_point=mounted.get(device.volume_identity),
                    indexed_images=indexed.get(device.id, 0),
                    active_job=active.get(device.id),
                    missing_images=marked.count if marked is not None else 0,
                    missing_since=marked.since if marked is not None else None,
                )
            )
        return summaries

    def execute_one(self, device: Device) -> DeviceSummary:
        """Descreve um único dispositivo; o caso de um elemento de `execute()`."""
        (summary,) = self.execute([device])
        return summary

    def _active_jobs(self) -> dict[DeviceId, IndexingJob]:
        """Mapeia cada dispositivo ocupado ao job que o segura, em duas consultas.

        O índice único parcial do RFC-029 §9 permite no máximo um job ativo por
        dispositivo, então agrupar em memória não tem ambiguidade a resolver, e
        `setdefault()` nunca chega a descartar nada -- ele está ali para que um
        estado acrescentado a `is_active` no futuro degrade para "o primeiro
        encontrado", e não para uma queda.

        **Nenhum método foi acrescentado a `IndexingJobRepository` para isto.**
        O filtro de status que ele já publica responde à pergunta; uma porta
        cresce por uma pergunta que não consegue responder, não por uma que
        poderia ser formulada de um jeito mais elegante.
        """
        active: dict[DeviceId, IndexingJob] = {}
        for status in ACTIVE_STATUSES:
            for job in self._jobs.list(status=status):
                active.setdefault(job.device_id, job)
        return active
