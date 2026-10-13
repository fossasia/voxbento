"""Short-lived browser program-sender session credentials."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.add_column(sa.Column("program_session_secret_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("program_session_secret_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.drop_column("program_session_secret_expires_at")
        batch.drop_column("program_session_secret_hash")
