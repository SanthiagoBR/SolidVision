"""add embedding vector and hnsw index

Revision ID: 999b801e80f4
Revises: 9d29f1a527fe
Create Date: 2026-08-09 20:52:23.863299

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = '999b801e80f4'
down_revision: str | Sequence[str] | None = '9d29f1a527fe'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "images",
        sa.Column("embedding", Vector(1152), nullable=True),
    )
    op.create_index(
        "ix_images_embedding_hnsw",
        "images",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_images_embedding_hnsw", table_name="images")
    op.drop_column("images", "embedding")
