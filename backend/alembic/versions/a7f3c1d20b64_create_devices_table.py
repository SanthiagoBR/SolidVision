"""create devices table

RFC-027 step 1 of 3. Creates `devices` and adds `images.device_id` and
`images.relative_path` as **nullable**, adding no constraints and
**guessing nothing**.

The guessing is what it refuses to do, and the refusal is the design. A
row's `relative_path` cannot be derived from its absolute `path` without
knowing which prefix was the scan root -- and the root arrived as the
worker's `--root` argument, which was never stored anywhere. There is not
enough information in the database to split `D:/fotos/2018/x.JPG` into a
device and a path within it, so this migration does not try (RFC-027
section 6.3).

`path` therefore survives this revision. Reconciliation reads it to decide
which rows belong to which disk; the next revision drops it.

Between this revision and the next, the split is filled in by
`python -m app.infrastructure.workers.device_reconcile --root PATH --label HD2`,
which resolves the volume identity of the named root, creates or finds the
`Device`, and rewrites every row whose `path` falls under that root --
including its primary key, and **preserving its embedding**, which is the
only expensive column in the table.

Revision ID: a7f3c1d20b64
Revises: 26058b9e1d9a
Create Date: 2026-09-08 09:12:44.108236

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a7f3c1d20b64'
down_revision: str | Sequence[str] | None = '26058b9e1d9a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "devices",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        # The opaque platform identifier. Unique because two rows claiming
        # the same physical disk is precisely the duplication this RFC
        # exists to remove.
        sa.Column("volume_identity", sa.Text(), nullable=False, unique=True),
        # The platform discriminator -- "windows-volume-guid" today. Stored
        # so that a Linux or macOS adapter needs no migration of its own
        # (RFC-027 section 4.1).
        sa.Column("volume_kind", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("filesystem_label", sa.Text(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        # History ("when did I last see this disk"), never state ("is it
        # plugged in"). There is deliberately no is_connected column, and
        # no drive_letter or mount_point column either -- see RFC-027
        # sections 4 and 7.
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        # The declared denominator of "% indexed" (RFC-027 section 8),
        # persisted apart from the indexing result because the two count
        # different things.
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_file_count", sa.Integer(), nullable=True),
        sa.CheckConstraint("total_bytes >= 0", name="total_bytes_non_negative"),
        sa.CheckConstraint(
            "last_scan_file_count >= 0", name="last_scan_file_count_non_negative"
        ),
    )

    # Nullable, unconstrained, and unpopulated on purpose. Making either
    # column NOT NULL here would fail against any database that already
    # holds indexed images, and backfilling them would require inventing
    # the scan root that nobody recorded.
    op.add_column(
        "images",
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("images", sa.Column("relative_path", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("images", "relative_path")
    op.drop_column("images", "device_id")
    op.drop_table("devices")
