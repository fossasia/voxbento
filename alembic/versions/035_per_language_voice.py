"""Let each AI language be spoken (AI voice) or read only (AI text).

Revision ID: 035
Revises: 034
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("room_translation_languages") as batch_op:
        batch_op.add_column(sa.Column("tts_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("room_translation_languages") as batch_op:
        batch_op.drop_column("tts_enabled")
