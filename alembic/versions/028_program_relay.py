"""Room-scoped RTMPS ingest mode and encrypted YouTube relay destination (#749)."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.add_column(sa.Column("program_ingest_mode", sa.String(20), nullable=False, server_default="whip"))
        batch.add_column(sa.Column("program_relay_server", sa.String(255), nullable=True))
        batch.add_column(sa.Column("program_relay_key_enc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("program_relay_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        for name in (
            "program_relay_updated_at",
            "program_relay_key_enc",
            "program_relay_server",
            "program_ingest_mode",
        ):
            batch.drop_column(name)
