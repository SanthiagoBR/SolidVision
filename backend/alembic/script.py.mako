"""Alembic migration script template."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "${revision}"
down_revision = ${down_revision}
depends_on = ${depends_on or "[]"}


def upgrade() -> None:
    """Upgrade script."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade script."""
    ${downgrades if downgrades else "pass"}
