"""Persist the desired state of the managed Jitsi meeting floor agent."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.add_column(sa.Column("meeting_agent_enabled", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.drop_column("meeting_agent_enabled")
