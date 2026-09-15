"""add image capture date

RFC-028: when a photograph was taken, read from its EXIF, and where that
date came from.

**`captured_at` is `TIMESTAMP WITHOUT TIME ZONE`, beside a `file_modified_at`
that is `TIMESTAMPTZ`, and both are right.** `st_mtime` is an absolute
instant, so it gets the type for instants. EXIF `DateTimeOriginal` is the
camera's local wall-clock reading with no zone at all; storing it in
`TIMESTAMPTZ` would force a zone to be invented -- UTC, or whatever machine
ran the scan -- and either choice moves shots taken late on 31 December
into the next year (RFC-028 section 5).

**`capture_source` is a plain string, not a PostgreSQL `ENUM`.** Adding a
source later (`gps_derived`, `filename_parsed`) would otherwise cost a
migration, for no benefit a closed database type provides here.

Both nullable, and deliberately not backfilled by this revision. A
backfill needs the files, which need their disk plugged in, which a
migration cannot arrange (RFC-028 section 7). Existing rows read back as
`capture_source = NULL`, meaning *never examined*: the next indexing scan
of their disk fills them in without re-embedding anything, and
`python -m app.infrastructure.workers.capture_date_backfill --root PATH`
does the same without loading the model.

No index on `captured_at`. Whether one changes the plan for a date-filtered
vector search is a measurement (RFC-028 section 8.1), not something to
decide in the migration that adds the column.

Revision ID: c5d1e8f24a90
Revises: b8e4d2a13c75
Create Date: 2026-09-15 14:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d1e8f24a90"
down_revision: str | Sequence[str] | None = "b8e4d2a13c75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "images",
        sa.Column("captured_at", sa.DateTime(timezone=False), nullable=True),
    )
    op.add_column(
        "images",
        sa.Column("capture_source", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops both columns and every extracted date with them. Nothing is lost
    that cannot be recovered: the dates live in the files, and re-running
    the scan or the backfill after upgrading again reads them back.
    """
    op.drop_column("images", "capture_source")
    op.drop_column("images", "captured_at")
