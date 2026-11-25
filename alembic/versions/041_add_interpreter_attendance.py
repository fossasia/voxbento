"""add interpreter attendance

One row per visit to an interpreter booth (#1215): when the person joined and left, and
how many seconds they spent interpreting (active, stream connected, microphone on).

Revision ID: 041
Revises: 040
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "041"
down_revision: Union[str, Sequence[str], None] = "040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "interpreter_attendance",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=True),
        sa.Column("language_code", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invite_public_id", sa.String(length=32), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interpreting_seconds", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_interpreter_attendance_event_joined", "interpreter_attendance", ["event_id", "joined_at"])
    op.create_index("ix_interpreter_attendance_user_id", "interpreter_attendance", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_interpreter_attendance_user_id", table_name="interpreter_attendance")
    op.drop_index("ix_interpreter_attendance_event_joined", table_name="interpreter_attendance")
    op.drop_table("interpreter_attendance")
