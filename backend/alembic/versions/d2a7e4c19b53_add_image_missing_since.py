"""add image missing since

RFC-046: the photos a whole-disk scan no longer finds. `images.missing_since`
is NULL for a row whose file is on its disk, and the date of the scan that
first failed to list it otherwise. A marked row is hidden from every read that
serves the interface -- the search, the listing, the map and the counts -- and
is never deleted by a scan: the next scan that lists the file unmarks it
without inference, and only the purge, by a button, deletes (RFC-046 section
3).

**`TIMESTAMPTZ`, not a boolean** (section 5.1): the card says since when, and
a purge by age, if one ever exists, needs no other migration.

**Additive and without a backfill.** Every existing row starts present: what
is missing is learned by the next whole-disk scan, which needs the disk, which
a migration cannot arrange. The rows already indexed in a volume's
`$RECYCLE.BIN` (1,268 of `E:` on the development database, RFC-045 section
14.10) need nothing here either: the scan stops listing that folder, and its
first sweep marks them (RFC-046 section 5.6).

**No index.** The filter discards almost nothing, and the counts per device
already read the whole device (section 5.1); a partial index on the marked
rows comes in only if the count of the card shows up in a measurement.

Revision ID: d2a7e4c19b53
Revises: c3f81a5e7d24
Create Date: 2026-10-31 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2a7e4c19b53"
down_revision: str | Sequence[str] | None = "c3f81a5e7d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable column; every existing row reads back as present."""
    op.add_column(
        "images",
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove the column, and with it every mark: all rows visible again."""
    op.drop_column("images", "missing_since")
