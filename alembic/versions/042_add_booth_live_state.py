"""add booth_live_state

What a live booth needs to survive a portal restart (TD-03): Mission Control's
Go Live switch and who held the active slot, one row per booth with something live.

Revision ID: 042
Revises: 041
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "042"
down_revision: Union[str, Sequence[str], None] = "041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "booth_live_state",
        sa.Column("booth_id", sa.String(length=200), primary_key=True),
        sa.Column("language", sa.String(length=100), nullable=False),
        sa.Column("channel_id", sa.String(length=200), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=True),
        sa.Column("broadcast_unlocked", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("active_participant_id", sa.String(length=64), nullable=True),
        sa.Column("active_session_key", sa.String(length=100), nullable=True),
        sa.Column("active_display_name", sa.String(length=200), nullable=True),
        sa.Column("active_role", sa.String(length=32), nullable=True),
        sa.Column("active_user_id", sa.Integer(), nullable=True),
        sa.Column("active_invite_public_id", sa.String(length=32), nullable=True),
        sa.Column("active_mic_active", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("active_ingest_connected", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("booth_live_state")
