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


def _script_directory() -> ScriptDirectory:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config)


def _revision_after_rfc_018() -> Script:
    script = _script_directory()
    for revision in script.walk_revisions():
        if revision.down_revision == RFC_018_REVISION:
            return revision
    raise AssertionError(f"No revision found with down_revision {RFC_018_REVISION!r}")


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

    rfc_020 = _revision_after_rfc_018()
    assert chain == [rfc_020.revision, RFC_018_REVISION, RFC_017B_REVISION]


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
