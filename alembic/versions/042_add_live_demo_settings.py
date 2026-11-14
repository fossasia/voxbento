"""add live demo settings

Admin settings for the home-page live demo: whether it is shown, its voice, speech
model, load limits and a site-wide glossary. One row (id 1); no row means the
LIVE_DEMO_* environment defaults.

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
        "live_demo_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("voice_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("whisper_model", sa.String(length=20), nullable=False, server_default="base"),
        sa.Column("max_sessions", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("max_session_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("sessions_per_ip_per_hour", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("max_targets", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("glossary", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("live_demo_settings")
