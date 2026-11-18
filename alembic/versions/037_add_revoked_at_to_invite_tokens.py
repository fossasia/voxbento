"""add revoked_at to invite_tokens

Revision ID: 037
Revises: 036
Create Date: 2026-10-24 16:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "037"
down_revision: Union[str, Sequence[str], None] = "036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add revoked_at to invite_tokens."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [col["name"] for col in inspector.get_columns("invite_tokens")]

    if "revoked_at" not in cols:
        with op.batch_alter_table("invite_tokens") as batch_op:
            batch_op.add_column(sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema: remove revoked_at from invite_tokens."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [col["name"] for col in inspector.get_columns("invite_tokens")]

    if "revoked_at" in cols:
        with op.batch_alter_table("invite_tokens") as batch_op:
            batch_op.drop_column("revoked_at")
