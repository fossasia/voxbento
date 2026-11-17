"""add_srt_and_backup_contribution

Revision ID: 046
Revises: 045
Create Date: 2026-11-15 23:19:22.290952

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '046'
down_revision: Union[str, Sequence[str], None] = '045'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('rooms', sa.Column('program_srt_passphrase_enc', sa.Text(), nullable=True))
    op.add_column('rooms', sa.Column('program_srt_passphrase_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('rooms', sa.Column('program_backup_enabled', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('rooms', sa.Column('program_backup_key_enc', sa.Text(), nullable=True))
    op.add_column('rooms', sa.Column('program_backup_key_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('rooms', sa.Column('program_active_feed', sa.String(length=10), server_default='primary', nullable=False))
    op.add_column('rooms', sa.Column('program_backup_connected_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('rooms', 'program_backup_connected_at')
    op.drop_column('rooms', 'program_active_feed')
    op.drop_column('rooms', 'program_backup_key_expires_at')
    op.drop_column('rooms', 'program_backup_key_enc')
    op.drop_column('rooms', 'program_backup_enabled')
    op.drop_column('rooms', 'program_srt_passphrase_expires_at')
    op.drop_column('rooms', 'program_srt_passphrase_enc')
