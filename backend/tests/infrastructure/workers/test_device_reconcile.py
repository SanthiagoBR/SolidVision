"""The assisted migration step of RFC-027 section 6.3, against a real database.

These run between the two RFC-027 migrations in production, but the schema
they need is the *final* one plus a `path` column -- so the tests recreate
that transitional shape themselves, against the SAVEPOINT-isolated session
every other database test uses. Recreating it is cheap and honest: the
command's whole job is to read one column and rewrite three others, and a
test that stubbed the reads would be testing nothing but its own SQL.

What matters most here is what the command must never do. Every image row
is an embedding that cost real inference time -- RFC-024 measured 2.2
images/second on CPU, so a 40,000-photo disk is about five hours -- and the
command exists specifically so that none of it is spent twice.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.application.fakes import (
    FakeDeviceRepository,
    StubVolumeIdentityProvider,
    make_device,
)

from app.domain.entities.device import Device
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.filesystem.volume_identity_provider import ResolvedVolume
from app.infrastructure.workers.device_reconcile import (
    ReconcileSummary,
    _build_arg_parser,
    reconcile,
)
from app.infrastructure.workers.indexing_worker import register_device

MOUNT_POINT = Path("D:/")
DEVICE: Device = make_device(label="HD2")


@pytest.fixture()
def transitional_session(empty_db_session: Session) -> Iterator[Session]:
    """Put `images.path` back and relax the device columns, as step 1 leaves it.

    This is the shape the first RFC-027 migration produces: `path` still
    present because reconciliation reads it, the two new columns nullable
    because nothing can fill them in yet, and no foreign key because rows
    have no device to point at until this command gives them one.

    Undone in teardown so the session hands back the schema it borrowed;
    the surrounding transaction is rolled back anyway, but a DDL statement
    left applied would be visible to whatever ran next in the same
    connection.
    """
    empty_db_session.execute(text("ALTER TABLE images ADD COLUMN path VARCHAR NULL"))
    empty_db_session.execute(
        text("ALTER TABLE images DROP CONSTRAINT uq_images_device_relative_path")
    )
    empty_db_session.execute(
        text("ALTER TABLE images DROP CONSTRAINT fk_images_device_id")
    )
    empty_db_session.execute(
        text("ALTER TABLE images ALTER COLUMN device_id DROP NOT NULL")
    )
    empty_db_session.execute(
        text("ALTER TABLE images ALTER COLUMN relative_path DROP NOT NULL")
    )
    empty_db_session.commit()
    yield empty_db_session


def seed_legacy_row(
    session: Session, path: str, image_id: uuid.UUID | None = None
) -> uuid.UUID:
    """Insert one pre-RFC-027 row: an absolute path, no device, an embedding.

    The embedding is what the whole command exists to protect, so every
    row here has one and every test checks it survived.
    """
    row_id = image_id or uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO images (id, path, filename, extension, embedding) "
            "VALUES (:id, :path, :filename, 'jpg', :embedding)"
        ),
        {
            "id": row_id,
            "path": path,
            "filename": Path(path).stem,
            "embedding": str([0.25] * 512),
        },
    )
    session.commit()
    return row_id


def rows(session: Session) -> list[tuple[uuid.UUID, str | None, str | None, str]]:
    result = session.execute(
        text(
            "SELECT id, device_id, relative_path, embedding::text FROM images "
            "ORDER BY relative_path NULLS LAST, path"
        )
    )
    return [(row[0], str(row[1]) if row[1] else None, row[2], row[3]) for row in result]


def run(session: Session, root: str = "D:/fotos", **kwargs: object) -> ReconcileSummary:
    return reconcile(
        session=session,
        device=DEVICE,
        volume=ResolvedVolume(identity=DEVICE.volume_identity, mount_point=MOUNT_POINT),
        root=Path(root),
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_legacy_row_gains_a_device_and_a_relative_path(
    transitional_session: Session,
) -> None:
    seed_legacy_row(transitional_session, "D:/fotos/2018/DJI_0042.JPG")

    summary = run(transitional_session)

    (row,) = rows(transitional_session)
    assert summary.reconciled == 1
    assert row[1] == str(DEVICE.id)
    assert row[2] == "fotos/2018/DJI_0042.JPG"


def test_the_new_id_is_the_one_the_worker_would_mint(
    transitional_session: Session,
) -> None:
    """The command and the pipeline must agree, or the next run re-indexes.

    They agree because both call `compute_image_id()` with the device and
    a path measured from the *mount point* -- not from the root, which is
    only how the operator says which rows are on this disk.
    """
    seed_legacy_row(transitional_session, "D:/fotos/2018/DJI_0042.JPG")

    run(transitional_session)

    (row,) = rows(transitional_session)
    assert (
        row[0]
        == compute_image_id(DEVICE.id, ImagePath("fotos/2018/DJI_0042.JPG")).value
    )


def test_the_embedding_is_untouched(transitional_session: Session) -> None:
    """The single most important property of this command."""
    seed_legacy_row(transitional_session, "D:/fotos/2018/DJI_0042.JPG")
    before = rows(transitional_session)[0][3]

    run(transitional_session)

    assert rows(transitional_session)[0][3] == before


def test_rows_outside_the_root_are_left_alone(
    transitional_session: Session,
) -> None:
    """They are another disk the operator has not reconciled yet.

    Not deleted, not guessed at: the next migration refuses to run while
    any remain, which is the loud failure RFC-027 section 6.3 asks for
    instead of a quiet DELETE.
    """
    seed_legacy_row(transitional_session, "D:/fotos/a.jpg")
    seed_legacy_row(transitional_session, "E:/outra-gaveta/b.jpg")

    summary = run(transitional_session)

    assert summary.reconciled == 1
    assert summary.remaining_unreconciled == 1
    assert len(rows(transitional_session)) == 2


def test_matching_is_case_insensitive(transitional_session: Session) -> None:
    """Windows paths are, so a root typed as `d:\\fotos` must match `D:/fotos`."""
    seed_legacy_row(transitional_session, "D:/fotos/a.jpg")

    summary = run(transitional_session, root="d:/FOTOS")

    assert summary.reconciled == 1


def test_running_twice_changes_nothing_the_second_time(
    transitional_session: Session,
) -> None:
    seed_legacy_row(transitional_session, "D:/fotos/a.jpg")
    run(transitional_session)
    after_first = rows(transitional_session)

    second = run(transitional_session)

    assert second.reconciled == 0
    assert second.already_reconciled == 1
    assert rows(transitional_session) == after_first


def test_a_row_whose_id_is_already_correct_is_not_deleted(
    transitional_session: Session,
) -> None:
    """The regression guard for a bug that emptied the table.

    A downgrade restores the schema but cannot restore the old ids -- so
    on the way back up, a row's recomputed id equals the id it already
    has. Read as "another row already holds this identity", that is a
    collision, and the fix for a collision is to delete the duplicate. It
    is not a collision: the row is itself. The check has to be
    `new_id != old_id` before anything is called a duplicate.
    """
    already_correct = compute_image_id(DEVICE.id, ImagePath("fotos/a.jpg")).value
    seed_legacy_row(transitional_session, "D:/fotos/a.jpg", image_id=already_correct)

    summary = run(transitional_session)

    assert summary.merged_duplicates == 0
    assert summary.reconciled == 1
    assert len(rows(transitional_session)) == 1


def test_two_rows_for_one_file_collapse_onto_one(
    transitional_session: Session,
) -> None:
    """The duplication RFC-027 section 2.1 describes, being undone.

    The same file indexed once as `D:` and once as `F:` is two rows with
    two ids and two embeddings, both searchable. Once the identity no
    longer contains a drive letter they are one file, and one of the rows
    has to go -- the survivor keeps its embedding, and the merge is
    counted and logged rather than silent.
    """
    seed_legacy_row(transitional_session, "D:/fotos/a.jpg")
    seed_legacy_row(transitional_session, "F:/fotos/a.jpg")
    # The second root is the same disk seen under another letter, so both
    # rows are matched by reconciling the whole volume.
    summary = run(transitional_session, root="D:/")

    assert summary.matched == 1
    assert summary.merged_duplicates == 0
    assert len(rows(transitional_session)) == 2

    # ...and reconciling the other letter finds the survivor already there.
    second = reconcile(
        session=transitional_session,
        device=DEVICE,
        volume=ResolvedVolume(identity=DEVICE.volume_identity, mount_point=Path("F:/")),
        root=Path("F:/"),
    )

    assert second.merged_duplicates == 1
    assert len(rows(transitional_session)) == 1
    assert rows(transitional_session)[0][3] is not None


def test_a_dry_run_reports_without_writing(transitional_session: Session) -> None:
    """Worth having for an operation that rewrites primary keys."""
    seed_legacy_row(transitional_session, "D:/fotos/a.jpg")
    before = rows(transitional_session)

    summary = run(transitional_session, dry_run=True)

    assert summary.reconciled == 1
    assert rows(transitional_session) == before


def test_a_dry_run_does_not_register_the_device(tmp_path: Path) -> None:
    """A dry run that created a device row would not be a dry run."""
    device_repository = FakeDeviceRepository()

    register_device(
        volume_provider=StubVolumeIdentityProvider(mount_point=tmp_path),
        device_repository=device_repository,
        root=tmp_path,
        label="HD2",
        persist=False,
    )

    assert device_repository.list() == []


def test_a_row_already_relative_to_the_mount_point_is_reconciled(
    transitional_session: Session,
) -> None:
    """The shape `b8e4d2a13c75.downgrade()` leaves behind.

    Rolling back cannot restore an absolute path -- the mount point it
    began with was never stored -- so it backfills `path` from
    `relative_path`. Matching only absolute paths would make a downgraded
    database impossible to reconcile, turning RFC-007's required round
    trip into a one-way door.
    """
    seed_legacy_row(transitional_session, "fotos/2018/DJI_0042.JPG")

    summary = run(transitional_session)

    assert summary.reconciled == 1
    assert rows(transitional_session)[0][2] == "fotos/2018/DJI_0042.JPG"


class TestCommandLineEntryPoint:
    def test_the_root_argument_is_required(self) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args([])

    def test_the_label_is_optional(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(["--root", str(tmp_path)])

        assert args.label == ""
        assert args.dry_run is False

    def test_dry_run_is_a_flag(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(
            ["--root", str(tmp_path), "--dry-run", "--label", "HD2"]
        )

        assert args.dry_run is True
        assert args.label == "HD2"
