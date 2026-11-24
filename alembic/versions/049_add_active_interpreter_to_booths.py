"""remember the active interpreter of a booth across portal restarts

Revision ID: 049
Revises: 048
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "049"
down_revision: Union[str, Sequence[str], None] = "048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("booths") as batch_op:
        batch_op.add_column(sa.Column("active_interpreter_session_key", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("active_interpreter_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("booths") as batch_op:
        batch_op.drop_column("active_interpreter_since")
        batch_op.drop_column("active_interpreter_session_key")
