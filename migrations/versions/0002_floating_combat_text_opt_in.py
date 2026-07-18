"""Add the opt-in preference for floating combat text."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_floating_combat_text_opt_in"
down_revision = "0001_sightline_baseline"
branch_labels = None
depends_on = None


def _settings_has_column(column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "settings" not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns("settings")}


def upgrade() -> None:
    if _settings_has_column("floating_combat_text_enabled"):
        return
    op.add_column(
        "settings",
        sa.Column(
            "floating_combat_text_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    if _settings_has_column("floating_combat_text_enabled"):
        op.drop_column("settings", "floating_combat_text_enabled")
