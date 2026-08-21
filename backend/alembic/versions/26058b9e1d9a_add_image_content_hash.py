"""add image content hash

RFC-024 completes the three-step incremental check that `ARCHITECTURE.md`
section 16 specifies and RFC-020 implemented only the first two steps of.
With size and mtime alone, any mtime touch -- a copy, a backup restore, a
`git checkout` -- forces a full re-embed. `content_hash` lets the pipeline
confirm that the bytes really changed before paying for inference.

Nullable, and deliberately not backfilled. Existing rows read back as NULL,
which the skip decision treats as "unknown, cannot confirm unchanged" and
resolves by re-embedding; a backfill would have to read every indexed file
from disk to save a one-time re-embed that the incremental check is already
designed to absorb.

The column is change detection only. `ImageId` stays path-derived (RFC-022
section 7.1), so two byte-identical files at two paths remain two rows that
happen to share a hash. Nothing here is unique, indexed, or a key.

Revision ID: 26058b9e1d9a
Revises: db526438ced5
Create Date: 2026-08-21 11:20:37.914306

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '26058b9e1d9a'
down_revision: str | Sequence[str] | None = 'db526438ced5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A hex-encoded SHA-256 digest is exactly 64 characters.
SHA256_HEX_LENGTH = 64


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "images",
        sa.Column("content_hash", sa.String(length=SHA256_HEX_LENGTH), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("images", "content_hash")
