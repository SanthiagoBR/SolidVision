"""Ler o progresso de um job, que é como um cliente acompanha uma execução longa.

Polling em vez de um canal de push (RFC-029 §7.2): o cliente é uma interface
local pedindo um número que muda a cada poucos segundos, e uma conexão
persistente acrescentaria gerenciamento de conexão, reconexão e um caminho
assíncrono pela API para economizar requisições que quase não custam nada em
localhost.
"""

from __future__ import annotations

from app.domain.entities.indexing_job import IndexingJob
from app.domain.exceptions import JobNotFoundError
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.job_id import JobId


class GetIndexingJobUseCase:
    """Devolve um job, ou diz claramente que esse job não existe."""

    def __init__(self, job_repository: IndexingJobRepository) -> None:
        self._jobs = job_repository

    def execute(self, job_id: JobId) -> IndexingJob:
        """Devolve o job, levantando exceção quando o id não nomeia nada.

        Um job inexistente é um erro, e não uma resposta vazia, e é essa
        distinção que permite a um cliente diferenciar "ainda não terminou" de
        "esse id nunca foi um job" durante o polling.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"No indexing job with id {job_id}.")
        return job
