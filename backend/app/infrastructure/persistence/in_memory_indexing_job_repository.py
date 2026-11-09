"""Repositório em memória de jobs, preso ao contrato do PostgreSQL (RFC-029).

**Este double mente por omissão, a menos que seja construído com cuidado**, e os
dois pontos em que mentiria são as duas propriedades em que o RFC inteiro se
apoia. Se aceitasse um segundo job ativo para um dispositivo, todo teste da
Application acima dele seria evidência sobre o double, e não sobre o sistema;
se a reivindicação dele não fosse atômica, o único comportamento de que o loop
de polling depende ficaria sem teste em todo lugar, menos contra um banco real.

Então as duas são emuladas, e não puladas: a regra do job ativo é verificada
no insert, e toda mutação é feita sob um lock, para que duas threads chamando
`claim_next()` não saiam as duas com o mesmo job.
"""

from __future__ import annotations

import dataclasses
import datetime
import threading
import uuid

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import DeviceBusyError
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId


class InMemoryIndexingJobRepository(IndexingJobRepository):
    """Implementação do repositório sobre um dict e um lock."""

    def __init__(self) -> None:
        self._jobs: dict[uuid.UUID, IndexingJob] = {}
        self._lock = threading.Lock()
        """Faz as vezes dos locks de linha que o PostgreSQL fornece.

        `claim_next()` é uma leitura e uma escrita que não podem se intercalar
        com outra reivindicação. No PostgreSQL isso é `FOR UPDATE SKIP LOCKED`
        dentro de um único `UPDATE`; aqui é este lock, mantido durante a
        operação inteira. Sem ele, o double entregaria o mesmo job a dois
        chamadores e o teste de contrato passaria só contra o PostgreSQL --
        exatamente o tipo de diferença que um teste de contrato existe para
        pegar.
        """

    def create(self, job: IndexingJob) -> IndexingJob:
        """Insere, recusando um dispositivo que já tem um job pendente ou rodando.

        A emulação do índice único parcial do RFC-029 §9. É um
        verifica-depois-age, o que a implementação do PostgreSQL é proibida de
        ser -- e aqui não há problema justamente porque o lock faz da
        verificação e da ação um passo só, que é a propriedade que o índice dá
        lá e que um `SELECT` sozinho não dá.
        """
        with self._lock:
            if any(
                existing.device_id == job.device_id and existing.status.is_active
                for existing in self._jobs.values()
            ):
                raise DeviceBusyError(
                    f"Device {job.device_id} already has an active indexing job."
                )
            stored = (
                job
                if job.created_at is not None
                else dataclasses.replace(
                    job, created_at=datetime.datetime.now(tz=datetime.UTC)
                )
            )
            self._jobs[stored.id.value] = stored
            return stored

    def get(self, job_id: JobId) -> IndexingJob | None:
        return self._jobs.get(job_id.value)

    def record_progress(
        self,
        job_id: JobId,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob | None:
        """Escreve as colunas de progresso de um job rodando e o lê de volta.

        Aplica a atualização à linha como ela está guardada *agora*, não a uma
        cópia que quem chama tem em mãos, e é isso que faz do
        `cancel_requested` devolvido o valor da rota, e não o valor velho do
        worker.
        """
        with self._lock:
            current = self._jobs.get(job_id.value)
            if current is None or current.status is not JobStatus.RUNNING:
                return None
            updated = current.with_progress(progress, checkpoint, heartbeat_at)
            self._jobs[job_id.value] = updated
            return updated

    def save_if_status(
        self, job: IndexingJob, expected: JobStatus
    ) -> IndexingJob | None:
        """Escreve só enquanto a linha guardada ainda estiver em `expected`."""
        with self._lock:
            current = self._jobs.get(job.id.value)
            if current is None or current.status is not expected:
                return None
            self._jobs[job.id.value] = job
            return job

    def claim_next(self, now: datetime.datetime) -> IndexingJob | None:
        """Pega o job mais antigo da fila, sob o lock, ou devolve `None`."""
        with self._lock:
            pending = [
                job for job in self._jobs.values() if job.status is JobStatus.PENDING
            ]
            if not pending:
                return None
            oldest = min(pending, key=_created_at_key)
            return self._claim_locked(oldest, now)

    def claim(self, job_id: JobId, now: datetime.datetime) -> IndexingJob | None:
        with self._lock:
            job = self._jobs.get(job_id.value)
            if job is None or job.status is not JobStatus.PENDING:
                return None
            return self._claim_locked(job, now)

    def _claim_locked(self, job: IndexingJob, now: datetime.datetime) -> IndexingJob:
        """Aplica a transição de reivindicação e a guarda; o lock já está tomado."""
        claimed = job.claim(now)
        self._jobs[claimed.id.value] = claimed
        return claimed

    def list_stale(self, heartbeat_before: datetime.datetime) -> list[IndexingJob]:
        """Devolve os jobs rodando cujo heartbeat não se moveu recentemente.

        Um job rodando sem heartbeat nenhum não é devolvido. `claim()` grava
        um, então essa linha não pode existir -- e tratá-la como parada
        seria encerrar um job sem evidência nenhuma.
        """
        return [
            job
            for job in self._jobs.values()
            if job.status is JobStatus.RUNNING
            and job.last_heartbeat_at is not None
            and job.last_heartbeat_at < heartbeat_before
        ]

    def list(
        self,
        device_id: DeviceId | None = None,
        status: JobStatus | None = None,
    ) -> list[IndexingJob]:
        """Devolve os jobs que casam, mais novos primeiro, como exige o contrato."""
        jobs = [
            job
            for job in self._jobs.values()
            if (device_id is None or job.device_id == device_id)
            and (status is None or job.status is status)
        ]
        return sorted(jobs, key=_created_at_key, reverse=True)


def _created_at_key(job: IndexingJob) -> datetime.datetime:
    """Ordena pela criação, pondo primeiro um job sem timestamp.

    `created_at` é gravado por `create()`, então `None` só chega aqui para
    uma entidade que alguém montou à mão e guardou direto. Pô-la na frente,
    em vez de levantar erro, mantém o double usável em testes que não se
    importam com o tempo, sem inventar um timestamp que a linha não tem.
    """
    return job.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC)
