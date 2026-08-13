"""add incremental image metadata

Revision ID: cbd5647f61b7
Revises: 999b801e80f4
Create Date: 2026-08-13 17:36:15.171856

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cbd5647f61b7'
down_revision: str | Sequence[str] | None = '999b801e80f4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "images",
        sa.Column("file_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "images",
        sa.Column("file_modified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "file_size_non_negative",
        "images",
        "file_size >= 0",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("file_size_non_negative", "images", type_="check")
    op.drop_column("images", "file_modified_at")
    op.drop_column("images", "file_size")
