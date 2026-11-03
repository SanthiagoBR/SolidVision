"""restart the index for rfc 045

RFC-045 section 5.9: **changing the vector restarts the database from zero.**
CLIP now decodes every JPEG with `draft("RGB", (1000, 1000))`, which moves the
vector of every photo whose shorter side is 2,000 px or more -- the drone
photos of 12 MP that are most of the user's collection. The database does not
record which preprocessing made each vector, so an index holding vectors of
both recipes cannot be told apart row by row; the product's rule (28/10) is
that it is not kept at all.

What goes, and what stays:

* **Every row of `images`** -- the vectors, the hashes, the dates and the
  positions. `TRUNCATE` rather than `DELETE`: it empties the HNSW index with
  the table, where a `DELETE` would leave 116,396 dead entries in it for a
  vacuum to repair.
* **Every row of `indexing_jobs` and `indexing_job_scopes`**: the history of
  scans of an index that no longer exists, and the checkpoints that would
  resume them past files that are now unindexed.
* **`devices` stays**, with each disk's label and volume identity, which are
  not vectors: restarting is indexing again, not registering the disks
  again. Their percentage indexed (RFC-039) reads 0% until they are.
* **The thumbnails are not touched.** They are files named by the image id,
  which is the same after the restart (it comes from the device and the
  path, RFC-027), and the reindex overwrites them. The ones of photos that
  no longer exist stay orphaned; cleaning them is not this RFC's.

Precedent: `db526438ced5`, which changed the vector to 512 dimensions, also
discarded every vector.

**`downgrade()` gives nothing back.** RFC-007 requires one, and it leaves the
schema as it was -- which this revision never changed -- but the deleted rows
do not return.

Revision ID: c3f81a5e7d24
Revises: 6d77379a36a1
Create Date: 2026-10-28 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f81a5e7d24"
down_revision: str | Sequence[str] | None = "6d77379a36a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Empty the index and the job history; keep the devices."""
    op.execute("TRUNCATE TABLE images")
    op.execute("TRUNCATE TABLE indexing_job_scopes, indexing_jobs")


def downgrade() -> None:
    """Nothing to undo in the schema, and nothing that can be undone in the data.

    The rows the upgrade deleted are gone; indexing the disks again is how
    they come back, under either revision.
    """
