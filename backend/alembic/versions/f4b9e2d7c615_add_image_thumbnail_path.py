"""add image thumbnail path

RFC-030: where an image's thumbnail is kept, in a cache directory the
application owns.

**A location relative to the cache, not an absolute path.** The same
decision RFC-027 made for `relative_path`: storing the cache's current
directory in every row would strand all of them the day the cache moves.

**Nullable, and not backfilled here.** Rendering a thumbnail needs the
photo, which needs its disk plugged in, which a migration cannot arrange.
Existing rows read back NULL -- no thumbnail -- and the thumbnail endpoint
answers 404 for them, which the UI shows as a placeholder.
`python -m app.infrastructure.workers.thumbnail_backfill --root PATH`
renders them without loading the embedding model.

No index and no uniqueness constraint. Nothing looks rows up by thumbnail,
and the value is derived from `images.id`, which is already the key.

**The first schema that refers to `images.id` from outside the row**, if
not with a foreign key: the file on disk is named by it. RFC-027 section 6.2
justified its timing by the absence of such references and RFC-029 section
11 kept that true; this revision is where it stops being true (RFC-030
section 7.3).

Revision ID: f4b9e2d7c615
Revises: e7a2c9b41f30
Create Date: 2026-09-16 16:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4b9e2d7c615"
down_revision: str | Sequence[str] | None = "e7a2c9b41f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "images",
        sa.Column("thumbnail_path", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the column and every recorded location with it. The thumbnail
    files themselves are left in the cache directory: a migration must not
    delete files, and after upgrading again every row reads NULL, so the
    backfill regenerates and re-records them without needing `--force`.
    """
    op.drop_column("images", "thumbnail_path")
