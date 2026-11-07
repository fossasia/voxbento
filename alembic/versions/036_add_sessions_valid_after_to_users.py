"""add sessions_valid_after to users

Revision ID: 036
Revises: 035
Create Date: 2026-10-24 15:30:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "036"
down_revision: Union[str, Sequence[str], None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add sessions_valid_after to users."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    users_cols = [col["name"] for col in inspector.get_columns("users")]

    if "sessions_valid_after" not in users_cols:
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("sessions_valid_after", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema: remove sessions_valid_after from users."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    users_cols = [col["name"] for col in inspector.get_columns("users")]

    if "sessions_valid_after" in users_cols:
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("sessions_valid_after")
