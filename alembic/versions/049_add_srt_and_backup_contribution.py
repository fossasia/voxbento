"""add_srt_and_backup_contribution

Revision ID: 049
Revises: 048
Create Date: 2026-11-25 09:45:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "049"
down_revision: Union[str, Sequence[str], None] = "048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    room_cols = {col["name"] for col in inspector.get_columns("rooms")}

    cols = [
        ("program_srt_passphrase_enc", sa.Column("program_srt_passphrase_enc", sa.Text(), nullable=True)),
        (
            "program_srt_passphrase_expires_at",
            sa.Column("program_srt_passphrase_expires_at", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "program_backup_enabled",
            sa.Column("program_backup_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        ),
        ("program_backup_key_enc", sa.Column("program_backup_key_enc", sa.Text(), nullable=True)),
        (
            "program_backup_key_expires_at",
            sa.Column("program_backup_key_expires_at", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "program_active_feed",
            sa.Column("program_active_feed", sa.String(length=10), server_default="primary", nullable=False),
        ),
        (
            "program_backup_connected_at",
            sa.Column("program_backup_connected_at", sa.DateTime(timezone=True), nullable=True),
        ),
    ]
    for name, col in cols:
        if name not in room_cols:
            op.add_column("rooms", col)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.drop_column("program_backup_connected_at")
        batch_op.drop_column("program_active_feed")
        batch_op.drop_column("program_backup_key_expires_at")
        batch_op.drop_column("program_backup_key_enc")
        batch_op.drop_column("program_backup_enabled")
        batch_op.drop_column("program_srt_passphrase_expires_at")
        batch_op.drop_column("program_srt_passphrase_enc")
