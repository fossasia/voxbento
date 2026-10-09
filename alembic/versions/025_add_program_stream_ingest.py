"""add program stream ingest columns to rooms

Revision ID: 025
Revises: 024
Create Date: 2026-10-08 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "025"
down_revision: Union[str, Sequence[str], None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.add_column(
            sa.Column("floor_source_mode", sa.String(length=20), server_default="jitsi_bot", nullable=False)
        )
        batch_op.add_column(sa.Column("program_ingest_secret_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("program_ingest_secret_hint", sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column("program_ingest_secret_created_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("program_ingest_secret_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("program_ingest_last_connected_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("program_ingest_last_disconnected_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("program_sync_offset_ms", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.drop_column("program_sync_offset_ms")
        batch_op.drop_column("program_ingest_last_disconnected_at")
        batch_op.drop_column("program_ingest_last_connected_at")
        batch_op.drop_column("program_ingest_secret_expires_at")
        batch_op.drop_column("program_ingest_secret_created_at")
        batch_op.drop_column("program_ingest_secret_hint")
        batch_op.drop_column("program_ingest_secret_hash")
        batch_op.drop_column("floor_source_mode")
