"""add the room's YouTube caption language (#1087, plan 006)

Revision ID: 048
Revises: 047
Create Date: 2026-11-22 12:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "048"
down_revision: Union[str, Sequence[str], None] = "047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMN = "youtube_captions_language"


def upgrade() -> None:
    """Add rooms.youtube_captions_language (NULL = off), skipping it when it already exists."""
    cols = [col["name"] for col in sa.inspect(op.get_bind()).get_columns("rooms")]
    if COLUMN not in cols:
        with op.batch_alter_table("rooms") as batch_op:
            batch_op.add_column(sa.Column(COLUMN, sa.String(length=20), nullable=True))


def downgrade() -> None:
    cols = [col["name"] for col in sa.inspect(op.get_bind()).get_columns("rooms")]
    if COLUMN in cols:
        with op.batch_alter_table("rooms") as batch_op:
            batch_op.drop_column(COLUMN)
