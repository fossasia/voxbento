"""ai_interpretation_config

Revision ID: 018
Revises: 017
Create Date: 2026-07-06 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "018"
down_revision: Union[str, Sequence[str], None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add AI interpretation persona/style/vocabulary fields and vocabulary table."""
    # --- Room-level AI interpretation settings ---
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.add_column(sa.Column("floor_ai_interpreter_persona", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("floor_ai_interpretation_style", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("floor_ai_vocabulary_enabled", sa.Boolean(), server_default="1", nullable=False))

    # --- Booth-level AI interpretation settings ---
    with op.batch_alter_table("booths") as batch_op:
        batch_op.add_column(sa.Column("ai_interpreter_persona", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ai_interpretation_style", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ai_vocabulary_enabled", sa.Boolean(), server_default="1", nullable=False))

    # --- Vocabulary entries table ---
    op.create_table(
        "ai_vocabulary_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=True),
        sa.Column("booth_id", sa.Integer(), sa.ForeignKey("booths.id", ondelete="CASCADE"), nullable=True),
        sa.Column("source_term", sa.String(255), nullable=False),
        sa.Column("target_language", sa.String(20), server_default="all", nullable=False),
        sa.Column("target_term", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("case_sensitive", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("match_type", sa.String(20), server_default="phrase", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ai_vocab_event_language", "ai_vocabulary_entries", ["event_id", "target_language"])
    op.create_index("ix_ai_vocab_room_language", "ai_vocabulary_entries", ["room_id", "target_language"])
    op.create_index("ix_ai_vocab_booth_language", "ai_vocabulary_entries", ["booth_id", "target_language"])
    op.create_index("ix_ai_vocab_source_term", "ai_vocabulary_entries", ["source_term"])


def downgrade() -> None:
    """Remove AI interpretation config."""
    op.drop_index("ix_ai_vocab_source_term")
    op.drop_index("ix_ai_vocab_booth_language")
    op.drop_index("ix_ai_vocab_room_language")
    op.drop_index("ix_ai_vocab_event_language")
    op.drop_table("ai_vocabulary_entries")

    with op.batch_alter_table("booths") as batch_op:
        batch_op.drop_column("ai_vocabulary_enabled")
        batch_op.drop_column("ai_interpretation_style")
        batch_op.drop_column("ai_interpreter_persona")

    with op.batch_alter_table("rooms") as batch_op:
        batch_op.drop_column("floor_ai_vocabulary_enabled")
        batch_op.drop_column("floor_ai_interpretation_style")
        batch_op.drop_column("floor_ai_interpreter_persona")
