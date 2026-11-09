"""Formatos de requisição e de resposta da API de jobs de indexação (RFC-029 §7).

A resposta publica o progresso de um job e nada sobre a máquina em que ele
roda: nenhum ponto de montagem, nenhuma letra de unidade, nenhum caminho
absoluto. Todo caminho aqui é relativo ao dispositivo, a forma que o RFC-027
tornou a única durável -- e também a única que não diz a um cliente onde
ficam os arquivos do servidor.
"""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.entities.indexing_job import IndexingJob, JobStatus


class CreateJobRequestSchema(BaseModel):
    """O que um cliente envia para pedir uma rodada de indexação.

    `scopes` são pastas relativas ao ponto de montagem do dispositivo, e uma
    lista vazia quer dizer o dispositivo inteiro -- que é o que zero linhas
    de escopo já querem dizer no schema (RFC-029 §5.2).

    São strings simples, e não um tipo validado, aqui de propósito. O que
    torna um escopo válido -- relativo, sem `..`, sem compartilhamento UNC, e
    uma pasta que de fato está naquele disco -- é uma regra de domínio mais
    uma pergunta ao sistema de arquivos, e responder a qualquer uma das duas
    na Apresentação poria regra de negócio na camada errada e produziria um
    422 onde cabe um 400 com uma mensagem legível.
    """

    device_id: UUID = Field(description="The device to index")
    scopes: list[str] = Field(
        default_factory=list,
        description=(
            "Folders to index, relative to the device root, e.g. "
            '["2018", "2019/janeiro"]. Omit or send an empty list to index '
            "the whole device. Overlapping folders are merged, and the "
            "response echoes back the folders that will actually be walked."
        ),
    )


class JobSchema(BaseModel):
    """Um job de indexação, como o vê um cliente que o consulta."""

    id: UUID = Field(description="Identifier of this job")
    device_id: UUID = Field(description="The device being indexed")
    status: JobStatus = Field(
        description=(
            "pending (queued), running, completed, failed, or cancelled. "
            "The last three are final."
        )
    )
    scopes: list[str] = Field(
        description=(
            "The folders this job will actually walk, after overlapping "
            "requests were merged. An empty list means the whole device."
        )
    )
    created_at: datetime.datetime | None = Field(description="When the job was queued")
    started_at: datetime.datetime | None = Field(
        description=(
            "When a worker first picked the job up. Preserved across a "
            "resume, so it is when the work began rather than when it was "
            "last resumed."
        )
    )
    finished_at: datetime.datetime | None = Field(
        description="When the job reached a final state; null until then"
    )
    discovered_files: int = Field(
        description=(
            "Supported files the scan has found so far. **A running count, "
            "not a total, until discovery_complete is true** -- render it as "
            "'scanning N files' rather than as a percentage until then."
        )
    )
    discovery_complete: bool = Field(
        description=(
            "Whether the scan has finished, which is what turns "
            "discovered_files into a denominator"
        )
    )
    processed_images: int = Field(description="Images embedded or re-embedded")
    skipped_images: int = Field(
        description=(
            "Images the incremental check passed over because nothing "
            "changed. Large on a re-scan, and the reason a progress bar "
            "computed from processed_images alone looks stuck."
        )
    )
    failed_images: int = Field(description="Files that could not be indexed")
    last_processed_relative_path: str | None = Field(
        description=(
            "The checkpoint: every file up to and including this one is "
            "durable. Relative to the device, never an absolute path."
        )
    )
    cancel_requested: bool = Field(
        description=(
            "Whether a stop has been asked for. A running job keeps working "
            "until the batch in flight finishes, so this can be true while "
            "status is still running."
        )
    )
    attempts: int = Field(
        description=(
            "How many times a worker was given this job and did not finish "
            "it -- a crashed executor, a reboot mid-run"
        )
    )
    error_message: str | None = Field(
        description="Why the job failed; null unless status is failed"
    )

    @classmethod
    def from_domain(cls, job: IndexingJob) -> JobSchema:
        """Monta um job do domínio para a resposta.

        Um classmethod aqui, e não `from_attributes`, seguindo
        `SearchResponseSchema.from_hits`: a entidade aninha os contadores em
        `IndexingProgress` e os escopos em value objects, e escrever o
        mapeamento por extenso é o que mantém o formato da resposta como uma
        decisão, e não um reflexo da cara que a entidade calhe de ter.
        """
        return cls(
            id=job.id.value,
            device_id=job.device_id.value,
            status=job.status,
            scopes=[str(scope) for scope in job.scopes],
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            discovered_files=job.progress.discovered_files,
            discovery_complete=job.progress.discovery_complete,
            processed_images=job.progress.processed_images,
            skipped_images=job.progress.skipped_images,
            failed_images=job.progress.failed_images,
            last_processed_relative_path=(
                str(job.last_processed_relative_path)
                if job.last_processed_relative_path is not None
                else None
            ),
            cancel_requested=job.cancel_requested,
            attempts=job.attempts,
            error_message=job.error_message,
        )


class JobListSchema(BaseModel):
    """Uma página de jobs, os mais novos primeiro."""

    jobs: list[JobSchema] = Field(description="Matching jobs, most recent first")

    @classmethod
    def from_domain(cls, jobs: list[IndexingJob]) -> JobListSchema:
        return cls(jobs=[JobSchema.from_domain(job) for job in jobs])
