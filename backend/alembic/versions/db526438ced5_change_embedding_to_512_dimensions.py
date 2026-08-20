"""change embedding to 512 dimensions

RFC-023 replaces the placeholder 1152-dimension embedding space with the
512-dimension space of laion/CLIP-ViT-B-32-laion2B-s34B-b79K.

The column is dropped and re-added rather than altered in place. pgvector
encodes dimensionality in the column's type modifier, so changing it is a
type change, and `ALTER COLUMN ... TYPE vector(512)` has to contend with the
HNSW index that depends on the column. No production embeddings exist yet
(every row written so far carries either NULL or a FakeEmbeddingModel
vector), so there is nothing to preserve and the clean drop/recreate is both
simpler and safer than an in-place conversion.

Revision ID: db526438ced5
Revises: cbd5647f61b7
Create Date: 2026-08-20 16:12:44.108236

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = 'db526438ced5'
down_revision: str | Sequence[str] | None = 'cbd5647f61b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVIOUS_DIMENSION = 1152
CURRENT_DIMENSION = 512


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("ix_images_embedding_hnsw", table_name="images")
    op.drop_column("images", "embedding")
    op.add_column(
        "images",
        sa.Column("embedding", Vector(CURRENT_DIMENSION), nullable=True),
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
    op.add_column(
        "images",
        sa.Column("embedding", Vector(PREVIOUS_DIMENSION), nullable=True),
    )
    op.create_index(
        "ix_images_embedding_hnsw",
        "images",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
