"""add image position

RFC-032: where a photograph was taken, read from the GPS IFD of its EXIF, and
where that position came from.

**Two `double precision` columns, not one `point` and not `NUMERIC`.** The
entity has two fields and the response publishes two numbers; a `point` would
turn every `latitude` into `(images.position)[1]`, and `NUMERIC` would buy
decimal arithmetic to store digits below a drone's GNSS error (RFC-032
section 5).

**Three CHECKs, because the invariant has to hold for writers that never pass
through the Domain** -- a manual `UPDATE`, a careless backfill, a fixture built
by hand. Never half a position, and never off the planet.

**`position_source` is a plain string, not a PostgreSQL `ENUM`**, so that the
two sources already named for later -- `manual` and `subject_estimated` --
cost no migration.

**No extension.** No PostGIS, no `cube`/`earthdistance`: the distance is a
haversine expression over plain columns (RFC-032 section 5.1).

All three columns nullable, and deliberately not backfilled by this revision.
A backfill needs the files, which need their disk plugged in, which a
migration cannot arrange. Existing rows read back `position_source = NULL`,
meaning *never examined*: the next indexing scan of their disk fills them in
without re-embedding anything, and
`python -m app.infrastructure.workers.exif_backfill --root PATH` does the same
without loading the model.

**No spatial index, because the measurement did not justify one** (RFC-032
section 6.1; `experiments/rfc-032-geolocation/planner_check.log`). A B-tree on
`latitude` and a GiST on `point(longitude, latitude)` were both measured on
20,000 clustered positions: neither made any HNSW configuration meet the
criterion it failed without one, and at 100,000 rows the B-tree took the map's
whole-collection grouping from ~387 ms to ~306 ms -- not the halving the
criterion asked for. RFC-028 measured a B-tree on `captured_at`, did not
justify it, and did not create it; this is the same rule applied again.

Revision ID: 6d77379a36a1
Revises: f4b9e2d7c615
Create Date: 2026-10-03 18:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6d77379a36a1"
down_revision: str | Sequence[str] | None = "f4b9e2d7c615"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("images", sa.Column("latitude", sa.Double(), nullable=True))
    op.add_column("images", sa.Column("longitude", sa.Double(), nullable=True))
    op.add_column("images", sa.Column("position_source", sa.String(), nullable=True))
    # Short names on purpose: the metadata's naming convention renders a
    # CHECK as `ck_<table>_<name>`, so these become `ck_images_position_pairing`,
    # `ck_images_latitude_range` and `ck_images_longitude_range` -- the names
    # RFC-032 section 5 gives -- exactly as RFC-020's `file_size_non_negative`
    # became `ck_images_file_size_non_negative`. Spelled out in full here they
    # would be prefixed twice.
    op.create_check_constraint(
        "position_pairing",
        "images",
        "(latitude IS NULL) = (longitude IS NULL)",
    )
    op.create_check_constraint(
        "latitude_range",
        "images",
        "latitude IS NULL OR (latitude BETWEEN -90 AND 90)",
    )
    op.create_check_constraint(
        "longitude_range",
        "images",
        "longitude IS NULL OR (longitude BETWEEN -180 AND 180)",
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the constraints, then the columns and every extracted position with
    them. Nothing is lost that cannot be recovered: the positions live in the
    files, and re-running the scan or the backfill after upgrading again reads
    them back. No embedding is touched in either direction.
    """
    op.drop_constraint("longitude_range", "images", type_="check")
    op.drop_constraint("latitude_range", "images", type_="check")
    op.drop_constraint("position_pairing", "images", type_="check")
    op.drop_column("images", "position_source")
    op.drop_column("images", "longitude")
    op.drop_column("images", "latitude")
