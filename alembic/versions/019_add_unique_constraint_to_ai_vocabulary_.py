"""add unique constraint to ai_vocabulary_entries

Revision ID: 019
Revises: 018
Create Date: 2026-07-07 12:02:06.993455

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "019"
down_revision: Union[str, Sequence[str], None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("ai_vocabulary_entries") as batch_op:
        batch_op.create_unique_constraint(
            "uq_ai_vocab_scope_term_lang", ["event_id", "room_id", "booth_id", "source_term", "target_language"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("ai_vocabulary_entries") as batch_op:
        batch_op.drop_constraint("uq_ai_vocab_scope_term_lang", type_="unique")
