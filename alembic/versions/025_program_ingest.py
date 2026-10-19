"""Room-scoped program ingest credentials and floor ownership."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.add_column(sa.Column("floor_source", sa.String(20), nullable=False, server_default="jitsi_bot"))
        batch.add_column(sa.Column("program_key_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("program_key_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("program_session_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("program_connected_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("program_disconnected_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("program_sync_offset_ms", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("floor_caption_seq", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        for name in (
            "floor_caption_seq",
            "program_sync_offset_ms",
            "program_disconnected_at",
            "program_connected_at",
            "program_session_id",
            "program_key_expires_at",
            "program_key_hash",
            "floor_source",
        ):
            batch.drop_column(name)
