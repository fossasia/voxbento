"""Event homepage visibility: is_public flag and optional start/end times."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # is_public defaults to false so existing events (including test events) stop being advertised
    # until an organizer opts in.
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.drop_column("ends_at")
        batch.drop_column("starts_at")
        batch.drop_column("is_public")
