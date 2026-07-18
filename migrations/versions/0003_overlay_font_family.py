"""Add the floating combat text font-family preference."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003_overlay_font_family"
down_revision = "0002_floating_combat_text_opt_in"
branch_labels = None
depends_on = None


def _settings_has_column(column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "settings" not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns("settings")}


def upgrade() -> None:
    if _settings_has_column("overlay_font_family"):
        return
    op.add_column(
        "settings",
        sa.Column(
            "overlay_font_family",
            sa.String(length=32),
            nullable=False,
            server_default="OpenDyslexic",
        ),
    )


def downgrade() -> None:
    if _settings_has_column("overlay_font_family"):
        op.drop_column("settings", "overlay_font_family")
