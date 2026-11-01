"""add meeting analysis settings and the meeting_reports table

Revision ID: 039
Revises: 038
Create Date: 2026-11-01 12:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "039"
down_revision: Union[str, Sequence[str], None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = {col["name"] for col in inspector.get_columns("events")}
    with op.batch_alter_table("events") as batch_op:
        if "meeting_analysis_enabled" not in cols:
            batch_op.add_column(sa.Column("meeting_analysis_enabled", sa.Boolean(), nullable=False, server_default="0"))
        if "meeting_analysis_provider" not in cols:
            batch_op.add_column(
                sa.Column("meeting_analysis_provider", sa.String(length=50), nullable=False, server_default="local")
            )
        if "meeting_analysis_model" not in cols:
            batch_op.add_column(
                sa.Column("meeting_analysis_model", sa.String(length=100), nullable=False, server_default="")
            )

    if "meeting_reports" not in inspector.get_table_names():
        op.create_table(
            "meeting_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
            sa.Column("channel_id", sa.String(length=100), nullable=False),
            sa.Column("channel_label", sa.String(length=200), nullable=False),
            sa.Column("language_code", sa.String(length=20), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("model", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error", sa.String(length=500), nullable=True),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("sections_json", sa.Text(), nullable=False),
            sa.Column("last_segment_id", sa.Integer(), nullable=True),
            sa.Column("segment_count", sa.Integer(), nullable=False),
            sa.Column("truncated", sa.Boolean(), nullable=False),
            sa.Column("tokens_used", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_meeting_reports_room_created", "meeting_reports", ["room_id", "created_at"])
        op.create_index(
            "uq_meeting_reports_room_in_progress",
            "meeting_reports",
            ["room_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('pending', 'running')"),
            postgresql_where=sa.text("status IN ('pending', 'running')"),
        )


def downgrade() -> None:
    op.drop_index("uq_meeting_reports_room_in_progress", table_name="meeting_reports")
    op.drop_index("ix_meeting_reports_room_created", table_name="meeting_reports")
    op.drop_table("meeting_reports")
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("meeting_analysis_model")
        batch_op.drop_column("meeting_analysis_provider")
        batch_op.drop_column("meeting_analysis_enabled")
