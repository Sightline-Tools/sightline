"""Establish the versioned Sightline schema baseline."""
from __future__ import annotations

from alembic import op

from app.db import Base


revision = "0001_sightline_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())

