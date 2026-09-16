"""Schema-level guarantees of the job tables, read from the metadata (RFC-029).

These assertions are about what the database actually holds, not about
what any Python object does with it. Each one guards a decision that would
be easy to undo by writing a plausible-looking column.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import DateTime, Index, Table

from app.infrastructure.database.models.indexing_job_model import (
    ACTIVE_JOB_INDEX,
    IndexingJobModel,
    IndexingJobScopeModel,
)

JOB_TABLES = [
    cast(Table, IndexingJobModel.__table__),
    cast(Table, IndexingJobScopeModel.__table__),
]


def test_neither_job_table_references_images() -> None:
    """RFC-029 section 11, and the window RFC-027 section 6.2 left open.

    Rewriting image identity stays cheap only while nothing references
    `images.id`. RFC-027 named this RFC as the one that might close that
    window; it does not, because a job refers to a device and to folders,
    its counters are aggregates, and its checkpoint is a path. An
    association table linking jobs to images would be 40,000 rows per job
    for a log nothing reads.

    The window closes in RFC-030, which addresses thumbnails by
    `images.id` -- deliberately, and not here.
    """
    for table in JOB_TABLES:
        referenced = {
            foreign_key.column.table.name
            for column in table.c
            for foreign_key in column.foreign_keys
        }
        assert "images" not in referenced, f"{table.name} has a key into images"


def test_no_job_table_carries_an_image_shaped_column() -> None:
    """A softer version of the same rule: no id-by-another-name either.

    A plain `image_id` column with no declared foreign key would reference
    `images` in every way that matters to the rewrite RFC-027 is
    protecting, while passing the test above.
    """
    for table in JOB_TABLES:
        columns = set(table.c.keys())
        assert "image_id" not in columns
        assert "image_ids" not in columns


def test_one_active_job_per_device_is_a_partial_unique_index() -> None:
    """RFC-029 section 9: the rule lives in the database, not in a convention.

    A plain unique index on `device_id` would forbid a device from ever
    having two jobs, history included. The predicate is what narrows it to
    the jobs that actually hold the disk.
    """
    table = cast(Table, IndexingJobModel.__table__)
    index = next(
        candidate for candidate in table.indexes if candidate.name == ACTIVE_JOB_INDEX
    )

    assert isinstance(index, Index)
    assert index.unique is True
    assert [column.name for column in index.columns] == ["device_id"]

    predicate = str(index.dialect_options["postgresql"]["where"])
    assert "pending" in predicate
    assert "running" in predicate


def test_a_queued_job_holds_the_device_too() -> None:
    """`pending` is inside the predicate, and that is deliberate.

    A queued job has already reserved the disk, and it keeps the
    reservation through the reaper's requeue -- which is what stops an
    interrupted job at 60% from losing its place to whatever was asked for
    next.
    """
    table = cast(Table, IndexingJobModel.__table__)
    index = next(
        candidate for candidate in table.indexes if candidate.name == ACTIVE_JOB_INDEX
    )

    assert "'pending'" in str(index.dialect_options["postgresql"]["where"])


def test_job_timestamps_are_zone_aware() -> None:
    """The opposite decision to `images.captured_at`, and both are right.

    A capture date is a camera's local wall clock with no zone (RFC-028
    section 5). These are instants on the machine running the job, and an
    instant without a zone is ambiguous. This test exists so that anyone
    "unifying" the two has to read both reasons first.
    """
    table = cast(Table, IndexingJobModel.__table__)

    for name in ("created_at", "started_at", "finished_at", "last_heartbeat_at"):
        column_type = table.c[name].type
        assert isinstance(column_type, DateTime)
        assert column_type.timezone is True, f"{name} lost its time zone"


def test_the_checkpoint_column_is_device_relative_by_name() -> None:
    """RFC-027 removed absolute paths; a checkpoint must not smuggle one back."""
    columns = set(cast(Table, IndexingJobModel.__table__).c.keys())

    assert "last_processed_relative_path" in columns
    assert "last_processed_path" not in columns
    assert "mount_point" not in columns
    assert "drive_letter" not in columns


def test_the_cancellation_flag_and_the_attempt_counter_exist() -> None:
    """Both were missing from the RFC's draft schema and both are load-bearing.

    Section 8 wrote `cancel_requested = true` against a table of section
    5.1 that had no such column; `attempts` is what bounds the reaper's
    requeue loop.
    """
    table = cast(Table, IndexingJobModel.__table__)

    assert table.c.cancel_requested.nullable is False
    assert table.c.attempts.nullable is False


def test_counters_start_at_zero_and_are_not_nullable() -> None:
    """A missing counter must not be renderable as a blank progress bar."""
    table = cast(Table, IndexingJobModel.__table__)

    for name in (
        "discovered_files",
        "processed_images",
        "skipped_images",
        "failed_images",
    ):
        assert table.c[name].nullable is False, name
        assert table.c[name].server_default is not None, name


def test_scopes_are_keyed_by_job_and_path() -> None:
    """The pair is the key, so a duplicate scope row is impossible."""
    table = cast(Table, IndexingJobScopeModel.__table__)

    assert [column.name for column in table.primary_key] == [
        "job_id",
        "relative_path",
    ]


def test_scopes_cascade_with_their_job() -> None:
    """The asymmetry with `images.device_id`, which refuses instead.

    A scope has no meaning without its job. An image row is an embedding
    that cost real inference time, which is why RFC-027 section 6.3 makes
    the device key refuse rather than cascade.
    """
    table = cast(Table, IndexingJobScopeModel.__table__)
    (foreign_key,) = list(table.c.job_id.foreign_keys)

    assert foreign_key.column.table.name == "indexing_jobs"
    assert foreign_key.ondelete == "CASCADE"


def test_the_job_device_key_does_not_cascade() -> None:
    """Deleting a disk must not silently erase the record of indexing it."""
    table = cast(Table, IndexingJobModel.__table__)
    (foreign_key,) = list(table.c.device_id.foreign_keys)

    assert foreign_key.column.table.name == "devices"
    assert foreign_key.ondelete is None
