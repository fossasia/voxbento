"""add_srt_and_backup_contribution

Revision ID: 050
Revises: 049
Create Date: 2026-11-25 10:04:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "050"
down_revision: Union[str, Sequence[str], None] = "049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
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
    if context.is_offline_mode():
        for _, col in cols:
            op.add_column("rooms", col)
        return

    inspector = sa.inspect(op.get_bind())
    room_cols = {col["name"] for col in inspector.get_columns("rooms")}
    missing = [col for name, col in cols if name not in room_cols]
    if missing:
        with op.batch_alter_table("rooms") as batch_op:
            for col in missing:
                batch_op.add_column(col)


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
