"""add index ix_booths_event_id to booths

Revision ID: 033
Revises: 032
Create Date: 2026-10-23
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "033"
down_revision: Union[str, Sequence[str], None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("booths") as batch_op:
        batch_op.create_index("ix_booths_event_id", ["event_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("booths") as batch_op:
        batch_op.drop_index("ix_booths_event_id")
