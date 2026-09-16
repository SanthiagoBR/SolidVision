"""create indexing jobs

RFC-029: the two tables behind the asynchronous indexing API --
`indexing_jobs` and its child `indexing_job_scopes`.

**The partial unique index is the point of this revision**, not an
afterthought in it:

    CREATE UNIQUE INDEX uq_one_active_job_per_device
        ON indexing_jobs (device_id)
        WHERE status IN ('pending', 'running');

It is what makes "one active job per device" true rather than customary.
Two concurrent `POST /api/v1/jobs` for the same disk race inside
PostgreSQL, where the race is, instead of inside a Python `SELECT` that
both of them would pass (RFC-029 section 9). It is written by hand with
`postgresql_where=` because autogenerate does not reliably reproduce a
partial index, and `downgrade()` drops it before the table so the order
is explicit rather than incidental.

`pending` is inside the predicate deliberately. A queued job has already
reserved the disk, and it keeps that reservation when the reaper puts an
abandoned job back in the queue -- which is what stops a job interrupted
at 60% from losing its place to whatever was requested next.

**No foreign key to `images`, in either table.** RFC-027 section 6.2 kept
image identity cheap to rewrite only while nothing referenced
`images.id`, and named this RFC as the one that might close that window.
It does not: a job refers to a device and to folders, its counters are
aggregates, and its checkpoint is a path (RFC-029 section 11).

**Timestamps are `TIMESTAMPTZ` here**, unlike `images.captured_at`, which
RFC-028 section 5 made zoneless because EXIF records a camera's local
wall clock. These are instants on the machine running the job, and an
instant without a zone is ambiguous.

Revision ID: e7a2c9b41f30
Revises: c5d1e8f24a90
Create Date: 2026-09-15 20:05:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e7a2c9b41f30"
down_revision: str | Sequence[str] | None = "c5d1e8f24a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_JOB_INDEX = "uq_one_active_job_per_device"
ACTIVE_JOB_PREDICATE = "status IN ('pending', 'running')"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "indexing_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Plain text rather than a PostgreSQL ENUM, following RFC-028's
        # `capture_source`: adding a state later should not cost a
        # migration, and a closed database type buys nothing here.
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # A running count while the scan is in flight, and a total only
        # once `discovery_complete` is true. Discovery is a generator, so
        # there is no denominator before then, and producing one would
        # mean reading the disk twice.
        sa.Column(
            "discovered_files", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "processed_images", sa.Integer(), nullable=False, server_default="0"
        ),
        # Without this column a job that skipped 39,000 of 40,000 files
        # reports processed=1000 against discovered=40000 and looks stuck
        # (RFC-029 section 5.1).
        sa.Column("skipped_images", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_images", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "discovery_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # The checkpoint, relative to the device's mount point. Absolute
        # would name a drive letter, which is the instability RFC-027
        # removed from this database.
        sa.Column("last_processed_relative_path", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Written on a timer rather than per batch: a re-scan of an
        # indexed disk produces no batches for minutes, and a heartbeat
        # tied to batches would have the reaper killing healthy jobs
        # (RFC-029 section 9.1).
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        # A flag rather than a `cancelling` status, so the route can write
        # it without racing the worker for the `status` column, and so the
        # index above keeps holding the device until the worker actually
        # lets go (RFC-029 section 8).
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Bounds the reaper's requeue loop. Without it, a file that crashes
        # the process outright would kill every worker that resumed onto
        # it, for ever.
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name="fk_indexing_jobs_device_id"
        ),
        sa.CheckConstraint("discovered_files >= 0", name="discovered_files_non_negative"),
        sa.CheckConstraint("processed_images >= 0", name="processed_images_non_negative"),
        sa.CheckConstraint("skipped_images >= 0", name="skipped_images_non_negative"),
        sa.CheckConstraint("failed_images >= 0", name="failed_images_non_negative"),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    op.create_index(
        ACTIVE_JOB_INDEX,
        "indexing_jobs",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_JOB_PREDICATE),
    )

    op.create_table(
        "indexing_job_scopes",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        # CASCADE here, unlike `images.device_id`, which refuses. A scope
        # has no meaning without its job; an image row is an embedding
        # that cost real inference time (RFC-027 section 6.3).
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["indexing_jobs.id"],
            name="fk_indexing_job_scopes_job_id",
            ondelete="CASCADE",
        ),
        # The pair is the key, which makes a duplicate scope row
        # impossible -- a cheap second guard behind `normalize_scopes()`.
        sa.PrimaryKeyConstraint("job_id", "relative_path"),
    )


def downgrade() -> None:
    """Downgrade schema.

    The index is dropped before its table rather than left to fall with
    it. PostgreSQL would drop it either way, but writing it out is what
    makes the reversal reviewable -- a partial index is the part of this
    revision most likely to be recreated by hand.
    """
    op.drop_table("indexing_job_scopes")
    op.drop_index(ACTIVE_JOB_INDEX, table_name="indexing_jobs")
    op.drop_table("indexing_jobs")
