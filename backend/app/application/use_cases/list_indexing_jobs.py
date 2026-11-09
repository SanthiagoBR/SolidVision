"""Listagem de jobs, para a UI mostrar o que aconteceu com um disco (RFC-029 §7.2)."""

from __future__ import annotations

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.device_id import DeviceId


class ListIndexingJobsUseCase:
    """Devolve jobs, do mais novo ao mais velho, filtrados por dispositivo ou status."""

    def __init__(self, job_repository: IndexingJobRepository) -> None:
        self._jobs = job_repository

    def execute(
        self,
        device_id: DeviceId | None = None,
        status: JobStatus | None = None,
    ) -> list[IndexingJob]:
        """Devolve os jobs que casam.

        Um id de dispositivo desconhecido não é rejeitado, pela razão que o
        RFC-027 §9 dá sobre os filtros da busca: isto estreita um conjunto,
        em vez de afirmar que o dispositivo nomeado existe, e então a lista
        de um disco que o sistema nunca viu volta, corretamente, vazia.
        Rejeitá-lo seria uma segunda consulta em todo pedido de lista para
        responder a uma pergunta que ninguém fez.

        Os dois filtros omitidos dão todos os jobs, nunca nenhum -- a mesma
        armadilha que o `device_id` da rota de busca teve de evitar.
        """
        return self._jobs.list(device_id=device_id, status=status)
