"""add public_id to invite_tokens

Revision ID: 038
Revises: 037
Create Date: 2026-10-24 16:30:00.000000

"""

from __future__ import annotations

import secrets
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "038"
down_revision: Union[str, Sequence[str], None] = "037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add public_id to invite_tokens, backfill, and enforce uniqueness."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [col["name"] for col in inspector.get_columns("invite_tokens")]

    if "public_id" not in cols:
        with op.batch_alter_table("invite_tokens") as batch_op:
            batch_op.add_column(sa.Column("public_id", sa.String(length=32), nullable=True))

        # Backfill existing rows with unique secrets.token_urlsafe(12) values
        rows = conn.execute(sa.text("SELECT token FROM invite_tokens WHERE public_id IS NULL")).fetchall()
        for row in rows:
            tok = row[0]
            pid = secrets.token_urlsafe(12)
            conn.execute(
                sa.text("UPDATE invite_tokens SET public_id = :pid WHERE token = :tok"),
                {"pid": pid, "tok": tok},
            )

        with op.batch_alter_table("invite_tokens") as batch_op:
            batch_op.alter_column("public_id", existing_type=sa.String(length=32), nullable=False)
            batch_op.create_index("ix_invite_tokens_public_id", ["public_id"], unique=True)


def downgrade() -> None:
    """Downgrade schema: remove public_id from invite_tokens."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [col["name"] for col in inspector.get_columns("invite_tokens")]

    if "public_id" in cols:
        with op.batch_alter_table("invite_tokens") as batch_op:
            batch_op.drop_index("ix_invite_tokens_public_id")
            batch_op.drop_column("public_id")
