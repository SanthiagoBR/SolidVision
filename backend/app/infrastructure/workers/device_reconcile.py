"""Assisted migration step for RFC-027: give existing image rows a device.

    python -m app.infrastructure.workers.device_reconcile --root PATH --label HD2

This is step 2 of the three-step migration in RFC-027 section 6.3, and it
is a deliverable rather than a line in a README on purpose: the work it
does cannot be done by `upgrade()`.

**Why a migration cannot do it.** A row's `relative_path` is not derivable
from its absolute `path` without knowing which prefix was the scan root,
and the root arrived as the indexing worker's `--root` argument, which was
never stored. There is no way to split `D:/fotos/2018/x.JPG` into a device
and a path within it from inside the database. So an operator supplies the
missing half -- the root -- and this command supplies everything else.

**What it rewrites, and what it protects.** Every matched row gets a
`device_id`, a `relative_path`, and a **new primary key**, because
`ImageId` is `uuid5` over the location and the location changed shape
(RFC-027 section 6.1). It does not touch `embedding`. That is the whole
point: RFC-024 measured indexing at 2.2 images/second on CPU, so a
40,000-image disk is roughly five hours of inference, and this command
exists so that none of it is repeated.

**Rewriting the primary key is safe here and will not be later.** Nothing
in the schema references `images.id` today -- `IndexingJobs`,
`SearchHistory` and thumbnails are all designed in `ARCHITECTURE.md` and
none is built -- so this is one table and one `UPDATE`, with no cascade.
RFC-029 closes that window (RFC-027 section 6.2).

**Rows it does not match are left exactly as they are.** They are the
record of a disk the operator has not reconciled yet, and each one holds
an embedding that cost real time; the next migration refuses to proceed
while any remain, which is the loud failure RFC-027 section 6.3 asks for.

The command is idempotent: rows that already have a device are skipped, so
running it twice over the same root is a no-op, and running it once per
disk is the intended usage.

The device itself is resolved by `register_device()`, imported from the
indexing worker rather than reimplemented here. Both commands mint image
ids from the device they resolve, so a second copy of the "find or create,
and which fields survive" rules would be a second chance for the two to
disagree about the same disk.

**It talks to the database in raw SQL, deliberately.** It runs *between*
two migrations, against a schema that matches neither the old `ImageModel`
nor the new one: `path` still exists, `device_id` and `relative_path`
exist but are nullable, and there is no foreign key yet. Mapping that
transitional shape onto the ORM model would mean an ORM class describing a
schema that exists for the duration of one operator session.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.entities.device import Device
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.filesystem.volume_identity_provider import ResolvedVolume
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.workers.indexing_worker import register_device

logger = get_logger(__name__)


@dataclass
class ReconcileSummary:
    """What one reconciliation run did, as data rather than as log output."""

    device_id: DeviceId
    matched: int = 0
    """Rows whose `path` fell under the supplied root."""

    reconciled: int = 0
    """Rows given a device, a relative path, and a new id."""

    already_reconciled: int = 0
    """Rows under the root that a previous run had already claimed."""

    merged_duplicates: int = 0
    """Rows discarded because a *different* row is already the same file.

    The one case where a row is removed, and it is not a loss: these are
    the duplicates RFC-027 section 2.1 describes -- the same file on the
    same disk indexed twice under two drive letters -- collapsing onto one
    identity now that the identity no longer contains a letter. The
    surviving row keeps its embedding, and every merge is logged with both
    of the old paths.

    "Different" is load-bearing. A row whose recomputed id equals its own
    current id is not a duplicate; it is a row that was already reconciled
    once and only needs its device columns filled in, which is the state
    `b8e4d2a13c75.downgrade()` leaves behind. Counting that as a merge
    empties the table.
    """

    remaining_unreconciled: int = 0
    """Rows anywhere in the table that still have no device.

    Reported so that an operator learns there is another disk to plug in
    *before* the next migration refuses to run, rather than from its error
    message.
    """

    merges: list[tuple[str, str]] = field(default_factory=list)

    def format_report(self) -> str:
        return (
            f"Reconciled device {self.device_id}: matched={self.matched}, "
            f"reconciled={self.reconciled}, "
            f"already_reconciled={self.already_reconciled}, "
            f"merged_duplicates={self.merged_duplicates}, "
            f"remaining_unreconciled={self.remaining_unreconciled}"
        )


def reconcile(
    session: Session,
    device: Device,
    volume: ResolvedVolume,
    root: Path,
    dry_run: bool = False,
) -> ReconcileSummary:
    """Rewrite every unreconciled row under `root` to belong to `device`.

    **`relative_path` is relative to the device's mount point, not to
    `root`.** The distinction is the entire second half of RFC-027 section
    2.2: `--root` is how the operator says *which rows are on this disk*,
    and nothing more. If the relative path were measured from `--root`,
    then reconciling `D:\\fotos` and later indexing `D:\\fotos\\2018` would
    describe the same file two different ways and mint two ids for it --
    reintroducing, through a different door, the duplication this whole
    RFC removes.

    Matching is case-insensitive because Windows paths are, and a stored
    `D:/fotos` must match a root the operator typed as `d:\\fotos`.
    """
    summary = ReconcileSummary(device_id=device.id)
    mount_prefix = _posix_prefix(volume.mount_point)
    root_prefix = _posix_prefix(root)
    relative_root = _strip_prefix(root_prefix, mount_prefix)

    for old_id, stored_path, has_device in _rows_under(
        session, root_prefix, relative_root
    ):
        summary.matched += 1
        if has_device:
            summary.already_reconciled += 1
            continue

        relative = _device_relative(stored_path, mount_prefix, relative_root)
        if relative is None:
            # The row matched the root but does not sit under the mount
            # point the root resolved to. Only reachable if the operator
            # pointed at one disk and the table holds a path that merely
            # looks like it -- leave it alone rather than guess.
            logger.warning(
                "Skipping %s: it does not sit under mount point %s",
                stored_path,
                volume.mount_point,
            )
            continue

        new_id = compute_image_id(device.id, relative).value
        if dry_run:
            summary.reconciled += 1
            continue

        if new_id != old_id and _row_exists(session, new_id):
            summary.merged_duplicates += 1
            summary.merges.append((stored_path, str(relative)))
            logger.warning(
                "Merging duplicate row for %s: another row already holds "
                "device %s / %s, and keeps its embedding.",
                stored_path,
                device.id,
                relative,
            )
            session.execute(
                text("DELETE FROM images WHERE id = :old_id"), {"old_id": old_id}
            )
            continue

        session.execute(
            text(
                "UPDATE images SET id = :new_id, device_id = :device_id, "
                "relative_path = :relative_path WHERE id = :old_id"
            ),
            {
                "new_id": new_id,
                "device_id": device.id.value,
                "relative_path": str(relative),
                "old_id": old_id,
            },
        )
        summary.reconciled += 1

    if dry_run:
        session.rollback()
    else:
        session.commit()

    summary.remaining_unreconciled = _unreconciled_count(session)
    return summary


def _rows_under(
    session: Session, root_prefix: str, relative_root: str | None
) -> list[tuple[uuid.UUID, str, bool]]:
    """Return `(id, path, already_has_device)` for every row under a root.

    Two prefixes are tried, because `path` can be in either of two shapes.
    The normal one is absolute -- `D:/fotos/2018/x.JPG` -- written by a
    pre-RFC-027 indexing run. The other is device-relative, and it is what
    `b8e4d2a13c75.downgrade()` leaves behind: rolling back cannot restore
    an absolute path, since the mount point it began with was never
    stored, so it backfills `path` from `relative_path`. Matching only the
    absolute form would leave a downgraded database impossible to
    reconcile, which would make RFC-007's required round trip a one-way
    door.

    Read in full before any write, rather than iterated while updating.
    The `UPDATE` below changes the primary key, so a cursor still walking
    the table could legitimately hand the same row back a second time
    under its new id -- and the second visit would compute a relative path
    from a `path` value that is no longer there.
    """
    statement = text(
        "SELECT id, path, device_id IS NOT NULL AS has_device FROM images "
        "WHERE lower(path) = lower(:root) "
        "OR lower(path) LIKE lower(:root) || '/%' "
        "OR lower(path) = lower(:relative_root) "
        "OR lower(path) LIKE lower(:relative_root) || '/%' "
        "ORDER BY path"
    )
    # A root that is not under the mount point has no relative form, and
    # the second pair of clauses then simply repeats the first rather than
    # being switched off with a NULL. PostgreSQL cannot infer a type for a
    # bare `NULL` parameter in `:p IS NOT NULL`, and casting one just to
    # disable a branch is more machinery than repeating a predicate that
    # is already true or already false.
    rows = session.execute(
        statement,
        {"root": root_prefix, "relative_root": relative_root or root_prefix},
    ).all()
    return [(row.id, row.path, bool(row.has_device)) for row in rows]


def _row_exists(session: Session, image_id: uuid.UUID) -> bool:
    """Return whether some row already holds this id.

    Callers must first check that the id is not the one the row already
    has. A row whose computed id equals its current id is not a duplicate
    of anything -- it is itself, and it happens whenever the identity was
    already correct and only the device columns need filling in, which is
    exactly the state a downgrade leaves behind. Treating that as a
    collision would delete every row in the table, one at a time, each
    time reporting that "another row already holds" the identity it is
    about to destroy.
    """
    statement = text("SELECT 1 FROM images WHERE id = :image_id")
    return session.execute(statement, {"image_id": image_id}).first() is not None


def _unreconciled_count(session: Session) -> int:
    statement = text(
        "SELECT count(*) FROM images "
        "WHERE device_id IS NULL OR relative_path IS NULL"
    )
    return int(session.execute(statement).scalar_one())


def _posix_prefix(path: Path) -> str:
    """Normalize a path to the posix form `ImagePath` stores, without a trailing slash.

    Stored paths went through `ImagePath`, which replaces backslashes with
    forward slashes, so the comparison has to happen in that form or a
    Windows root would match nothing at all.
    """
    return str(ImagePath(str(path))).rstrip("/")


def _device_relative(
    stored_path: str, mount_prefix: str, relative_root: str | None
) -> ImagePath | None:
    """Turn a stored `path` into a path relative to the device's mount point.

    Handles both shapes `_rows_under()` matches. An absolute path has the
    mount point stripped off it; a path that is already device-relative --
    what a downgrade leaves behind -- is returned unchanged.

    Returns `None` when the path is neither, which the caller reports
    rather than working around: a path that matched the root but sits
    under no recognised prefix means the two disagree, and guessing would
    file a photo under the wrong disk.
    """
    stripped = _strip_prefix(stored_path, mount_prefix)
    if stripped is not None:
        return ImagePath(stripped) if stripped else None
    if relative_root is not None and _is_under(stored_path, relative_root):
        return ImagePath(stored_path)
    return None


def _strip_prefix(path: str, prefix: str) -> str | None:
    r"""Remove a leading `prefix/` from `path`, case-insensitively.

    Case-insensitive because Windows paths are, so a stored `D:/fotos`
    has to match a mount point Windows reported as `d:\`. Returns `None`
    when `path` does not sit under `prefix` at all.
    """
    normalized = prefix.rstrip("/") + "/"
    if not path.lower().startswith(normalized.lower()):
        return None
    return path[len(normalized) :]


def _is_under(path: str, prefix: str) -> bool:
    """Return whether `path` is `prefix` itself or sits inside it."""
    return path.lower() == prefix.lower() or _strip_prefix(path, prefix) is not None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.workers.device_reconcile",
        description=(
            "Give existing image rows a device and a device-relative path, "
            "preserving their embeddings (RFC-027 section 6.3)."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help=(
            "The root that was passed to the indexing worker for this disk. "
            "Required, and required to be mounted: its volume identity is "
            "what the device is derived from, and there is no way to infer "
            "it from the stored paths."
        ),
    )
    parser.add_argument(
        "--label",
        default="",
        help=(
            "The user-facing name for the disk -- 'HD2'. Optional when the "
            "device already exists, in which case its current label stands."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report what would change and roll back. Worth having for an "
            "operation that rewrites primary keys over a real photo "
            "collection."
        ),
    )
    return parser


def main() -> None:
    """Compose the real reconciliation and run it against one named root."""
    from app.infrastructure.filesystem.volume_identity_provider import (
        WindowsVolumeIdentityProvider,
    )
    from app.infrastructure.persistence.postgres_device_repository import (
        PostgresDeviceRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal

    args = _build_arg_parser().parse_args()
    root = args.root.resolve()

    session = SessionLocal()
    try:
        device, volume = register_device(
            volume_provider=WindowsVolumeIdentityProvider(),
            device_repository=PostgresDeviceRepository(session),
            root=root,
            label=args.label,
            persist=not args.dry_run,
        )
        logger.info(
            "Device %s (%s) resolved at %s", device.label, device.id, volume.mount_point
        )
        summary = reconcile(
            session=session,
            device=device,
            volume=volume,
            root=root,
            dry_run=args.dry_run,
        )
        logger.info(
            "%s%s", summary.format_report(), " (dry run)" if args.dry_run else ""
        )
        if summary.remaining_unreconciled:
            logger.warning(
                "%d image row(s) still belong to no device. Run this command "
                "once per indexed root before applying the next migration.",
                summary.remaining_unreconciled,
            )
    finally:
        session.close()


if __name__ == "__main__":
    main()
