"""Request and response shapes for the indexing-job API (RFC-029 section 7).

The response publishes a job's progress and nothing about the machine it
runs on: no mount point, no drive letter, no absolute path. Every path
here is device-relative, which is the form RFC-027 made the only durable
one -- and also the only one that does not tell a client where the
server's files live.
"""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.entities.indexing_job import IndexingJob, JobStatus


class CreateJobRequestSchema(BaseModel):
    """What a client sends to ask for an indexing run.

    `scopes` are folders relative to the device's mount point, and an
    empty list means the whole device -- which is what zero scope rows
    already mean in the schema (RFC-029 section 5.2).

    They are plain strings rather than a validated type here on purpose.
    What makes a scope legal -- relative, no `..`, no UNC share, and a
    folder that is actually on that disk -- is a domain rule plus a
    filesystem question, and answering either in Presentation would put
    business logic in the wrong layer and produce a 422 where a 400 with
    a readable message belongs.
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
    """One indexing job, as a client polling it sees it."""

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
        """Shape a domain job for the wire.

        A classmethod here rather than `from_attributes`, following
        `SearchResponseSchema.from_hits`: the entity nests its counters in
        `IndexingProgress` and its scopes in value objects, and spelling
        the mapping out is what keeps the wire shape a decision rather
        than a reflection of whatever the entity happens to look like.
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
    """A page of jobs, newest first."""

    jobs: list[JobSchema] = Field(description="Matching jobs, most recent first")

    @classmethod
    def from_domain(cls, jobs: list[IndexingJob]) -> JobListSchema:
        return cls(jobs=[JobSchema.from_domain(job) for job in jobs])
