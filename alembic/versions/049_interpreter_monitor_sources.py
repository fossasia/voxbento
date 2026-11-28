"""Room-level interpreter monitor choices."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("rooms")}
    with op.batch_alter_table("rooms") as batch:
        for name in ("monitor_floor_enabled", "monitor_meeting_enabled"):
            if name not in existing:
                batch.add_column(sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.drop_column("monitor_meeting_enabled")
        batch.drop_column("monitor_floor_enabled")
