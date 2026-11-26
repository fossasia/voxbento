"""Room setting: listeners hear the floor while their interpreter is off air (#1397)."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("rooms")}
    if "auto_floor_enabled" not in existing:
        with op.batch_alter_table("rooms") as batch:
            batch.add_column(sa.Column("auto_floor_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.drop_column("auto_floor_enabled")
