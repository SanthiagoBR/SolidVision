"""SQLAlchemy models for indexing jobs and their scopes (RFC-029).

**Neither table has a foreign key to `images`, and neither may grow one.**
RFC-027 section 6.2 justified its own priority by noting that rewriting
image identity stays cheap only while nothing references `images.id`, and
named this RFC as the one that might close that window. It does not: a
job refers to a device and to a set of folders, its counters are
aggregates, and its checkpoint is a path. An association table linking
jobs to images would be 40,000 rows per job for a log with no reader
(RFC-029 section 11). `test_indexing_job_model.py` reads the SQLAlchemy
metadata and fails if such a key appears.

**The timestamps here are `TIMESTAMPTZ`, next to an `images.captured_at`
that is not, and both are right.** A capture date is the camera's local
wall-clock reading with no zone at all, so RFC-028 section 5 stores it
zoneless. Everything below is an instant on the machine running the job
-- when it was queued, when a worker last proved it was alive -- and an
instant without a zone is ambiguous. Anyone tempted to make the two
uniform should read both comments first.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.persistence.base import Base

ACTIVE_JOB_INDEX = "uq_one_active_job_per_device"
"""The partial unique index of RFC-029 section 9, named once.

Named as a module constant because the PostgreSQL repository has to
recognise this exact string in an `IntegrityError` to translate it into
`DeviceBusyError`. Matching on the message text instead would break the
day PostgreSQL rewords it or the server runs in another locale, and
treating *any* `IntegrityError` as "device busy" would report a bad
foreign key as a busy disk.
"""


class IndexingJobModel(Base):
    """SQLAlchemy representation of an `IndexingJob` domain entity."""

    __tablename__ = "indexing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("devices.id", name="fk_indexing_jobs_device_id"),
        nullable=False,
    )
    """Which disk this job is for.

    A device rather than the `collection_id` of `ARCHITECTURE.md` section
    15: `Collection` is still a placeholder (RFC-027 section 10), and a
    device is what the system actually has.
    """

    status: Mapped[str] = mapped_column(String, nullable=False)
    """`pending` / `running` / `completed` / `failed` / `cancelled`.

    Plain text rather than a PostgreSQL `ENUM`, following RFC-028's
    `capture_source`: a closed database type buys nothing here and would
    make adding a state cost a migration.
    """

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discovered_files: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    """A running count, and **not** a denominator until `discovery_complete`.

    Discovery is a generator (RFC-021), so the total does not exist until
    the scan ends. Counting ahead of time would mean walking the disk
    twice, which on the cold mechanical disk this product exists for is
    the dominant cost -- so the client is given the honest pair instead.
    """

    processed_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    skipped_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    """Files the incremental decision passed over (RFC-029 section 5.1).

    Without this column a job that skipped 39,000 of 40,000 files reports
    `processed=1000` against `discovered=40000` and looks stuck. It is the
    only thing that makes the progress bar honest.
    """

    failed_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    discovery_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_processed_relative_path: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    """The checkpoint, relative to the device's mount point.

    Relative for the reason every path in this database is relative since
    RFC-027: an absolute checkpoint would name a drive letter, and a drive
    letter is assigned by mount order. Compared as a path and never as a
    string -- see `FilesystemImageProvider`.
    """

    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    last_heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """The evidence that somebody is still working on this job.

    Written on a timer rather than per batch. A re-scan of an indexed disk
    produces no batches at all for minutes at a time, and a heartbeat tied
    to batches would have the reaper killing the healthiest possible run
    (RFC-029 section 9.1).
    """

    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    scopes: Mapped[list[IndexingJobScopeModel]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    """The folders to walk; zero rows means the whole device.

    `selectin` rather than lazy loading because every read of a job reads
    its scopes -- the API echoes them, and the executor walks them -- so
    the alternative is one extra query per job in every list response.
    """

    __table_args__ = (
        Index(
            ACTIVE_JOB_INDEX,
            "device_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        CheckConstraint("discovered_files >= 0", name="discovered_files_non_negative"),
        CheckConstraint("processed_images >= 0", name="processed_images_non_negative"),
        CheckConstraint("skipped_images >= 0", name="skipped_images_non_negative"),
        CheckConstraint("failed_images >= 0", name="failed_images_non_negative"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )
    """One active job per device, enforced by the database (RFC-029 section 9).

    A partial unique index, not a plain one: a device is expected to have
    many finished jobs and at most one that still holds the disk. Two
    concurrent creations race inside PostgreSQL, where the race actually
    is, instead of inside a Python `SELECT` that both of them would pass.

    `pending` is inside the predicate on purpose. A queued job has already
    reserved the disk, and it keeps that reservation through a reaper's
    requeue -- which is what stops an interrupted job at 60% from losing
    its place to whatever was asked for next.
    """

    @classmethod
    def from_domain(cls, job: IndexingJob) -> IndexingJobModel:
        """Create an ORM model from a domain entity."""
        model = cls(id=job.id.value)
        model.apply(job)
        return model

    def apply(self, job: IndexingJob) -> None:
        """Copy every mutable field of `job` onto this row.

        Shared by insert and update so the two cannot disagree about what
        a job is. `id` is deliberately absent: a job's identity never
        moves, and an update that rewrote it would create a second row.

        Scopes are rewritten only when they differ, because they never
        change after creation and `delete-orphan` would otherwise issue a
        delete and an insert on every heartbeat.
        """
        self.device_id = job.device_id.value
        self.status = job.status.value
        self.created_at = job.created_at or datetime.datetime.now(tz=datetime.UTC)
        self.started_at = job.started_at
        self.finished_at = job.finished_at
        self.discovered_files = job.progress.discovered_files
        self.processed_images = job.progress.processed_images
        self.skipped_images = job.progress.skipped_images
        self.failed_images = job.progress.failed_images
        self.discovery_complete = job.progress.discovery_complete
        self.last_processed_relative_path = (
            str(job.last_processed_relative_path)
            if job.last_processed_relative_path is not None
            else None
        )
        self.error_message = job.error_message
        self.last_heartbeat_at = job.last_heartbeat_at
        self.cancel_requested = job.cancel_requested
        self.attempts = job.attempts

        wanted = [str(scope) for scope in job.scopes]
        if [scope.relative_path for scope in self.scopes] != wanted:
            self.scopes = [IndexingJobScopeModel(relative_path=path) for path in wanted]

    def to_domain(self) -> IndexingJob:
        """Create a domain entity from this ORM model."""
        return IndexingJob(
            id=JobId(self.id),
            device_id=DeviceId(self.device_id),
            status=JobStatus(self.status),
            scopes=tuple(JobScope(scope.relative_path) for scope in self.scopes),
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            progress=IndexingProgress(
                discovered_files=self.discovered_files,
                processed_images=self.processed_images,
                skipped_images=self.skipped_images,
                failed_images=self.failed_images,
                discovery_complete=self.discovery_complete,
            ),
            last_processed_relative_path=(
                ImagePath(self.last_processed_relative_path)
                if self.last_processed_relative_path
                else None
            ),
            error_message=self.error_message,
            last_heartbeat_at=self.last_heartbeat_at,
            cancel_requested=self.cancel_requested,
            attempts=self.attempts,
        )


class IndexingJobScopeModel(Base):
    """One folder a job is restricted to, device-relative (RFC-029 section 5.2).

    A child table rather than an array or a JSON column on the job row.
    The question actually asked of scopes is *"is this folder covered by
    a running job?"*, which is a `WHERE` here and a scan in memory in any
    of the other shapes.

    The primary key is the pair, which makes a duplicate scope row
    impossible -- a second guard behind `normalize_scopes()`, and a
    cheap one, since the pair is what any lookup would key on anyway.
    """

    __tablename__ = "indexing_job_scopes"

    job_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey(
            "indexing_jobs.id",
            name="fk_indexing_job_scopes_job_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    """Where the folder sits within the device, never an absolute path.

    `ON DELETE CASCADE` on the key above, unlike `images.device_id`, which
    refuses. The asymmetry is deliberate: deleting a job should take its
    scopes, because a scope has no meaning without its job, whereas an
    image row is an embedding that cost real inference time (RFC-027
    section 6.3).
    """

    job: Mapped[IndexingJobModel] = relationship(back_populates="scopes")
