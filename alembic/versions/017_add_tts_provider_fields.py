"""add_tts_provider_fields

Revision ID: 017
Revises: 016
Create Date: 2026-06-28 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "017"
down_revision: Union[str, Sequence[str], None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.add_column(
            sa.Column("floor_tts_provider", sa.String(length=20), server_default="deepgram", nullable=False)
        )
        batch_op.add_column(sa.Column("floor_tts_voice", sa.String(length=50), server_default="M1", nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.drop_column("floor_tts_voice")
        batch_op.drop_column("floor_tts_provider")
