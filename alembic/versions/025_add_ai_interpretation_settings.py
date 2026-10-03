"""Add AI interpretation settings and vocabulary entries.

Revision ID: 025
Revises: 024
Create Date: 2026-09-28
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "025"
down_revision: Union[str, Sequence[str], None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("floor_ai_interpreter_persona", sa.Text(), nullable=True))
    op.add_column("rooms", sa.Column("floor_ai_interpretation_style", sa.Text(), nullable=True))
    op.add_column(
        "rooms",
        sa.Column("floor_ai_vocabulary_enabled", sa.Boolean(), server_default="1", nullable=False),
    )
    op.create_table(
        "ai_vocabulary_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=True),
        sa.Column("booth_id", sa.Integer(), nullable=True),
        sa.Column("source_term", sa.String(length=255), nullable=False),
        sa.Column("target_language", sa.String(length=20), server_default="all", nullable=False),
        sa.Column("target_term", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("case_sensitive", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("match_type", sa.String(length=20), server_default="phrase", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["booth_id"], ["booths.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_vocab_booth_language", "ai_vocabulary_entries", ["booth_id", "target_language"])
    op.create_index("ix_ai_vocab_event_language", "ai_vocabulary_entries", ["event_id", "target_language"])
    op.create_index("ix_ai_vocab_room_language", "ai_vocabulary_entries", ["room_id", "target_language"])
    op.create_index("ix_ai_vocab_source_term", "ai_vocabulary_entries", ["source_term"])


def downgrade() -> None:
    op.drop_index("ix_ai_vocab_source_term", table_name="ai_vocabulary_entries")
    op.drop_index("ix_ai_vocab_room_language", table_name="ai_vocabulary_entries")
    op.drop_index("ix_ai_vocab_event_language", table_name="ai_vocabulary_entries")
    op.drop_index("ix_ai_vocab_booth_language", table_name="ai_vocabulary_entries")
    op.drop_table("ai_vocabulary_entries")
    op.drop_column("rooms", "floor_ai_vocabulary_enabled")
    op.drop_column("rooms", "floor_ai_interpretation_style")
    op.drop_column("rooms", "floor_ai_interpreter_persona")
