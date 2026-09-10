"""enforce device ownership

RFC-027 step 3 of 3, and the step that refuses to be quiet.

Step 1 (`a7f3c1d20b64`) created `devices` and added two nullable columns.
Step 2 is not a migration at all -- it is
`python -m app.infrastructure.workers.device_reconcile --root PATH --label HD2`,
run once per disk, because the information needed to split an absolute
path into (device, relative path) is not in the database and never was
(RFC-027 section 6.3).

This revision closes the schema behind that work: NOT NULL on both
columns, the foreign key to `devices`, the unique constraint that finally
means what `UNIQUE (path)` only appeared to mean, and the removal of
`path` itself.

**It fails loudly on unreconciled rows rather than deleting them.** A row
with a NULL `device_id` is a photo on a disk the operator has not
reconciled yet, and its embedding cost real inference time -- RFC-024
measured indexing at 2.2 images/second on CPU, so a 40,000-image disk is
about five hours. Dropping those rows to make a constraint apply would
throw that away silently. The check below raises with the count and the
command to run instead.

**Why `path` goes rather than staying alongside.** Keeping both forms
would invite one of them to go stale, and the absolute one is exactly the
one that cannot be kept correct: on Windows a removable volume's drive
letter is a function of mount order, so a stationary file's absolute path
changes with nobody writing anything (RFC-027 section 2.1). The absolute
path still exists -- it is computed by joining a mount point resolved at
the moment of use onto `relative_path`.

**What `downgrade()` can and cannot restore.** It restores the *schema*:
`path` comes back, NOT NULL and unique, backfilled from `relative_path`,
and the device columns and constraints go away. It cannot restore the old
*identities*, because recomputing a pre-RFC-027 `ImageId` needs the
absolute path that this schema stopped storing, and the mount point it
began with was never stored at all. Nor can it restore absolute paths for
the same reason: the backfilled `path` is device-relative. Downgrading
and re-upgrading therefore requires re-running the reconciliation
command; it does not require re-running any inference, which is the
property that actually matters.

The backfill can also collide: two devices holding the same relative path
produce one `path` value twice, and the restored `UNIQUE (path)` will
reject it. That failure is correct and is the point of the constraint --
it is the same collision the old schema silently could not see.

Revision ID: b8e4d2a13c75
Revises: a7f3c1d20b64
Create Date: 2026-09-08 09:31:07.552914

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8e4d2a13c75'
down_revision: str | Sequence[str] | None = 'a7f3c1d20b64'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECONCILE_COMMAND = (
    "python -m app.infrastructure.workers.device_reconcile "
    "--root PATH --label LABEL"
)


def _unreconciled_row_count() -> int:
    """Count image rows that no reconciliation has claimed yet."""
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "SELECT count(*) FROM images "
            "WHERE device_id IS NULL OR relative_path IS NULL"
        )
    )
    return int(result.scalar_one())


def upgrade() -> None:
    """Upgrade schema."""
    unreconciled = _unreconciled_row_count()
    if unreconciled:
        raise RuntimeError(
            f"{unreconciled} image row(s) still have no device. They are not "
            "deleted, because each one holds an embedding that cost real "
            "inference time (RFC-027 section 6.3). Reconcile every indexed "
            f"root first:\n    {RECONCILE_COMMAND}"
        )

    op.alter_column("images", "device_id", nullable=False)
    op.alter_column("images", "relative_path", nullable=False)
    op.create_foreign_key(
        "fk_images_device_id", "images", "devices", ["device_id"], ["id"]
    )
    # No ON DELETE clause: deleting a device while its images remain must
    # be refused, not cascaded. The rows are a record of a disk the user
    # may need to plug in.
    op.drop_constraint("uq_images_path", "images", type_="unique")
    op.create_unique_constraint(
        "uq_images_device_relative_path", "images", ["device_id", "relative_path"]
    )
    op.drop_column("images", "path")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("images", sa.Column("path", sa.String(), nullable=True))
    op.execute(sa.text("UPDATE images SET path = relative_path"))
    op.alter_column("images", "path", nullable=False)
    op.drop_constraint("uq_images_device_relative_path", "images", type_="unique")
    op.create_unique_constraint("uq_images_path", "images", ["path"])
    op.drop_constraint("fk_images_device_id", "images", type_="foreignkey")
    op.alter_column("images", "relative_path", nullable=True)
    op.alter_column("images", "device_id", nullable=True)
