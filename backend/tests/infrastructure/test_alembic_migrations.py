"""Tests for the RFC-020 Alembic migration and the overall revision chain."""

from __future__ import annotations

import inspect
from pathlib import Path

from alembic.config import Config
from alembic.script import Script, ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

RFC_017B_REVISION = "9d29f1a527fe"
RFC_018_REVISION = "999b801e80f4"
RFC_020_REVISION = "cbd5647f61b7"
RFC_023_REVISION = "db526438ced5"
RFC_024_REVISION = "26058b9e1d9a"
RFC_027_DEVICES_REVISION = "a7f3c1d20b64"
RFC_027_OWNERSHIP_REVISION = "b8e4d2a13c75"
RFC_028_REVISION = "c5d1e8f24a90"
RFC_029_REVISION = "e7a2c9b41f30"
RFC_030_REVISION = "f4b9e2d7c615"


def _script_directory() -> ScriptDirectory:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config)


def _revision_after(down_revision: str) -> Script:
    script = _script_directory()
    for revision in script.walk_revisions():
        if revision.down_revision == down_revision:
            return revision
    raise AssertionError(f"No revision found with down_revision {down_revision!r}")


def _revision_after_rfc_018() -> Script:
    return _revision_after(RFC_018_REVISION)


def _revision_after_rfc_020() -> Script:
    return _revision_after(RFC_020_REVISION)


def _revision_after_rfc_023() -> Script:
    return _revision_after(RFC_023_REVISION)


def _statements_only(source: str) -> str:
    """Strip `#` comments so an assertion reads code rather than commentary.

    These migrations carry long explanations of what they deliberately do
    *not* do, so a plain substring search over the source answers a
    different question from the one the test is asking.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )


def test_alembic_has_exactly_one_head() -> None:
    script = _script_directory()

    assert len(script.get_heads()) == 1


def test_history_is_linear_from_base_to_head() -> None:
    script = _script_directory()
    (head_id,) = script.get_heads()

    chain = []
    revision: Script | None = script.get_revision(head_id)
    while revision is not None:
        chain.append(revision.revision)
        down_revision = revision.down_revision
        revision = (
            script.get_revision(down_revision)
            if isinstance(down_revision, str)
            else None
        )

    assert chain == [
        RFC_030_REVISION,
        RFC_029_REVISION,
        RFC_028_REVISION,
        RFC_027_OWNERSHIP_REVISION,
        RFC_027_DEVICES_REVISION,
        RFC_024_REVISION,
        RFC_023_REVISION,
        RFC_020_REVISION,
        RFC_018_REVISION,
        RFC_017B_REVISION,
    ]


def test_rfc_020_migration_down_revision_is_rfc_018() -> None:
    rfc_020 = _revision_after_rfc_018()

    assert rfc_020.down_revision == RFC_018_REVISION


def test_rfc_020_migration_adds_expected_columns_and_constraint() -> None:
    rfc_020 = _revision_after_rfc_018()
    upgrade_source = inspect.getsource(rfc_020.module.upgrade)

    assert 'sa.Column("file_size"' in upgrade_source
    assert 'sa.Column("file_modified_at"' in upgrade_source
    assert "nullable=True" in upgrade_source
    assert "file_size_non_negative" in upgrade_source
    assert "file_size >= 0" in upgrade_source


def test_rfc_020_migration_does_not_touch_unrelated_schema() -> None:
    rfc_020 = _revision_after_rfc_018()
    upgrade_source = inspect.getsource(rfc_020.module.upgrade)

    assert "create_table" not in upgrade_source
    assert "drop_table" not in upgrade_source
    assert "embedding" not in upgrade_source
    assert "ix_images_embedding_hnsw" not in upgrade_source


def test_rfc_020_migration_downgrade_reverses_only_new_changes() -> None:
    rfc_020 = _revision_after_rfc_018()
    downgrade_source = inspect.getsource(rfc_020.module.downgrade)

    assert 'op.drop_constraint("file_size_non_negative"' in downgrade_source
    assert 'op.drop_column("images", "file_modified_at")' in downgrade_source
    assert 'op.drop_column("images", "file_size")' in downgrade_source
    assert "drop_table" not in downgrade_source
    assert "embedding" not in downgrade_source
    assert "ix_images_embedding_hnsw" not in downgrade_source


def test_rfc_023_migration_down_revision_is_rfc_020() -> None:
    rfc_023 = _revision_after_rfc_020()

    assert rfc_023.down_revision == RFC_020_REVISION


def test_rfc_023_migration_rebuilds_the_embedding_column_at_512() -> None:
    """The 1152 -> 512 change is a drop/recreate, not an in-place ALTER.

    pgvector encodes dimensionality in the column's type modifier, and the
    HNSW index depends on the column, so `ALTER COLUMN ... TYPE` is the
    error-prone path. With no production embeddings to preserve there is
    nothing to gain from it (RFC-023 section 12).
    """
    upgrade_source = inspect.getsource(_revision_after_rfc_020().module.upgrade)

    assert 'op.drop_index("ix_images_embedding_hnsw"' in upgrade_source
    assert 'op.drop_column("images", "embedding")' in upgrade_source
    assert "Vector(CURRENT_DIMENSION)" in upgrade_source
    assert "alter_column" not in upgrade_source


def test_rfc_023_migration_recreates_the_hnsw_cosine_index() -> None:
    upgrade_source = inspect.getsource(_revision_after_rfc_020().module.upgrade)

    assert 'postgresql_using="hnsw"' in upgrade_source
    assert '"embedding": "vector_cosine_ops"' in upgrade_source


def test_rfc_023_migration_declares_the_expected_dimensions() -> None:
    module = _revision_after_rfc_020().module

    assert module.CURRENT_DIMENSION == 512
    assert module.PREVIOUS_DIMENSION == 1152


def test_rfc_023_migration_downgrade_restores_1152_and_the_index() -> None:
    downgrade_source = inspect.getsource(_revision_after_rfc_020().module.downgrade)

    assert "Vector(PREVIOUS_DIMENSION)" in downgrade_source
    assert "op.create_index(" in downgrade_source
    assert '"ix_images_embedding_hnsw"' in downgrade_source
    assert '"embedding": "vector_cosine_ops"' in downgrade_source


def test_rfc_023_migration_does_not_touch_unrelated_schema() -> None:
    upgrade_source = inspect.getsource(_revision_after_rfc_020().module.upgrade)

    assert "create_table" not in upgrade_source
    assert "drop_table" not in upgrade_source
    assert "file_size" not in upgrade_source
    assert "file_modified_at" not in upgrade_source


def test_rfc_024_migration_down_revision_is_rfc_023() -> None:
    rfc_024 = _revision_after_rfc_023()

    assert rfc_024.down_revision == RFC_023_REVISION


def test_rfc_024_migration_adds_a_nullable_content_hash_column() -> None:
    """Nullable and un-backfilled: NULL means "unknown", never "matches"."""
    module = _revision_after_rfc_023().module
    upgrade_source = inspect.getsource(module.upgrade)

    assert 'sa.Column("content_hash"' in upgrade_source
    assert "nullable=True" in upgrade_source
    assert module.SHA256_HEX_LENGTH == 64


def test_rfc_024_migration_leaves_content_hash_unindexed_and_not_unique() -> None:
    """Identity is path-derived (RFC-022 7.1); the hash detects change only.

    A unique constraint or an index here would be the first step towards
    the hash acquiring identity semantics, which would break the
    byte-identical-twins guarantee.
    """
    upgrade_source = inspect.getsource(_revision_after_rfc_023().module.upgrade)

    assert "unique" not in upgrade_source.lower()
    assert "create_index" not in upgrade_source


def test_rfc_024_migration_does_not_touch_unrelated_schema() -> None:
    upgrade_source = inspect.getsource(_revision_after_rfc_023().module.upgrade)

    assert "create_table" not in upgrade_source
    assert "drop_table" not in upgrade_source
    assert "embedding" not in upgrade_source
    assert "ix_images_embedding_hnsw" not in upgrade_source
    assert "file_size" not in upgrade_source
    assert "file_modified_at" not in upgrade_source


def test_rfc_024_migration_downgrade_drops_only_the_new_column() -> None:
    downgrade_source = inspect.getsource(_revision_after_rfc_023().module.downgrade)

    assert 'op.drop_column("images", "content_hash")' in downgrade_source
    assert "drop_table" not in downgrade_source
    assert "embedding" not in downgrade_source
    assert "ix_images_embedding_hnsw" not in downgrade_source


def test_rfc_028_migration_follows_rfc_027() -> None:
    assert _revision_after(RFC_027_OWNERSHIP_REVISION).revision == RFC_028_REVISION


def test_rfc_028_migration_adds_a_zoneless_timestamp_and_a_plain_string() -> None:
    """RFC-028 sections 4.2 and 5, checked in the migration as well as the model.

    The model and the migration are two places that can disagree, and
    only the migration decides what the database actually holds.
    """
    upgrade_source = inspect.getsource(
        _revision_after(RFC_027_OWNERSHIP_REVISION).module.upgrade
    )

    assert 'sa.Column("captured_at", sa.DateTime(timezone=False)' in upgrade_source
    assert 'sa.Column("capture_source", sa.String()' in upgrade_source
    assert "nullable=True" in upgrade_source
    assert "Enum" not in upgrade_source
    assert "timezone=True" not in upgrade_source


def test_rfc_028_migration_does_not_touch_unrelated_schema() -> None:
    upgrade_source = inspect.getsource(
        _revision_after(RFC_027_OWNERSHIP_REVISION).module.upgrade
    )

    assert "create_table" not in upgrade_source
    assert "drop_table" not in upgrade_source
    assert "embedding" not in upgrade_source
    assert "file_modified_at" not in upgrade_source
    assert "content_hash" not in upgrade_source


def test_rfc_028_migration_downgrade_drops_only_the_new_columns() -> None:
    downgrade_source = inspect.getsource(
        _revision_after(RFC_027_OWNERSHIP_REVISION).module.downgrade
    )

    assert 'op.drop_column("images", "capture_source")' in downgrade_source
    assert 'op.drop_column("images", "captured_at")' in downgrade_source
    assert "embedding" not in downgrade_source
    assert "drop_table" not in downgrade_source


def test_earlier_migrations_were_not_rewritten() -> None:
    """Each RFC adds a revision; none may edit an already-applied one.

    The RFC-018 migration is at risk from RFC-023, since it owns the
    original 1152-dimension column that RFC replaced. The RFC-023 migration
    is at risk from RFC-024 for the same reason -- it is the newest applied
    revision, and therefore the tempting place to "just add a column".
    """
    script = _script_directory()
    rfc_018_source = inspect.getsource(script.get_revision(RFC_018_REVISION).module)

    assert "Vector(1152)" in rfc_018_source
    assert "512" not in rfc_018_source

    rfc_023_source = inspect.getsource(script.get_revision(RFC_023_REVISION).module)

    assert "content_hash" not in rfc_023_source
    assert "Vector(CURRENT_DIMENSION)" in rfc_023_source


def test_rfc_029_migration_follows_rfc_028() -> None:
    assert _revision_after(RFC_028_REVISION).revision == RFC_029_REVISION


def test_rfc_029_migration_creates_both_job_tables() -> None:
    upgrade_source = inspect.getsource(_revision_after(RFC_028_REVISION).module.upgrade)

    assert '"indexing_jobs",' in upgrade_source
    assert '"indexing_job_scopes",' in upgrade_source
    assert upgrade_source.count("op.create_table(") == 2


def test_rfc_029_migration_writes_the_partial_unique_index_by_hand() -> None:
    """RFC-029 section 9, and the reason it is not left to autogenerate.

    A plain unique index on `device_id` would forbid a disk from ever
    having two jobs, history included. The `WHERE` is what narrows it to
    the jobs that actually hold the disk, and autogenerate does not
    reliably reproduce a partial index -- so it is written out and read
    back here.
    """
    module = _revision_after(RFC_028_REVISION).module
    upgrade_source = inspect.getsource(module.upgrade)

    assert module.ACTIVE_JOB_INDEX == "uq_one_active_job_per_device"
    assert "postgresql_where=sa.text(ACTIVE_JOB_PREDICATE)" in upgrade_source
    assert "unique=True" in upgrade_source
    assert "pending" in module.ACTIVE_JOB_PREDICATE
    assert "running" in module.ACTIVE_JOB_PREDICATE


def test_rfc_029_migration_adds_the_columns_the_draft_schema_omitted() -> None:
    """`cancel_requested` and `attempts` (RFC-029 sections 8 and 9.1).

    Section 8 wrote `cancel_requested = true` against a section 5.1 table
    that had no such column; `attempts` is what bounds the reaper's
    requeue loop so a process-killing file cannot loop for ever.
    """
    upgrade_source = inspect.getsource(_revision_after(RFC_028_REVISION).module.upgrade)

    assert '"cancel_requested",' in upgrade_source
    assert '"attempts",' in upgrade_source
    assert '"skipped_images",' in upgrade_source
    assert '"discovery_complete",' in upgrade_source
    assert '"last_processed_relative_path",' in upgrade_source


def test_rfc_029_migration_creates_no_key_into_images() -> None:
    """RFC-027 section 6.2's window stays open; RFC-030 is what closes it."""
    upgrade_source = inspect.getsource(_revision_after(RFC_028_REVISION).module.upgrade)

    assert '["images.id"]' not in upgrade_source
    assert '"image_id"' not in upgrade_source


def test_rfc_029_migration_does_not_touch_unrelated_schema() -> None:
    """Read from the code, not the prose.

    This revision explains at length why a scope cascades where an image
    row must not, so the word "embedding" appears in its comments. The
    question being asked is about the statements it executes.
    """
    upgrade_source = _statements_only(
        inspect.getsource(_revision_after(RFC_028_REVISION).module.upgrade)
    )

    assert "drop_table" not in upgrade_source
    assert "add_column" not in upgrade_source
    assert "embedding" not in upgrade_source
    assert "captured_at" not in upgrade_source
    # The `images` *table*, not the counters whose names end in it.
    assert '"images"' not in upgrade_source
    assert "images.id" not in upgrade_source


def test_rfc_029_migration_downgrade_drops_the_index_before_the_table() -> None:
    """Explicit rather than incidental: PostgreSQL would drop it either way.

    Writing it out is what makes the reversal reviewable, and a partial
    index is the part of this revision most likely to be recreated by
    hand.
    """
    downgrade_source = inspect.getsource(
        _revision_after(RFC_028_REVISION).module.downgrade
    )

    index_position = downgrade_source.index("drop_index")
    table_position = downgrade_source.index('drop_table("indexing_jobs")')

    assert index_position < table_position
    assert 'drop_table("indexing_job_scopes")' in downgrade_source
    assert "images" not in downgrade_source


def test_rfc_030_migration_follows_rfc_029() -> None:
    assert _revision_after(RFC_029_REVISION).revision == RFC_030_REVISION


def test_rfc_030_migration_adds_one_nullable_string_column() -> None:
    """NULL is "no thumbnail", which every row indexed before RFC-030 is."""
    upgrade_source = _statements_only(
        inspect.getsource(_revision_after(RFC_029_REVISION).module.upgrade)
    )

    assert upgrade_source.count("op.add_column(") == 1
    assert 'sa.Column("thumbnail_path", sa.String(), nullable=True)' in upgrade_source


def test_rfc_030_migration_does_not_touch_unrelated_schema() -> None:
    """No index, no constraint, no key, and nothing but `images`.

    `images.id` is referred to by the *files* this column names, not by a
    foreign key -- which is how RFC-030 section 7.3 closes RFC-027's window
    without adding one.
    """
    upgrade_source = _statements_only(
        inspect.getsource(_revision_after(RFC_029_REVISION).module.upgrade)
    )

    assert "create_index" not in upgrade_source
    assert "create_unique_constraint" not in upgrade_source
    assert "create_foreign_key" not in upgrade_source
    assert "drop_" not in upgrade_source
    assert "embedding" not in upgrade_source
    assert "indexing_jobs" not in upgrade_source


def test_rfc_030_migration_downgrade_drops_only_the_new_column() -> None:
    downgrade_source = _statements_only(
        inspect.getsource(_revision_after(RFC_029_REVISION).module.downgrade)
    )

    assert downgrade_source.count("op.drop_column(") == 1
    assert 'op.drop_column("images", "thumbnail_path")' in downgrade_source
    assert "drop_table" not in downgrade_source
