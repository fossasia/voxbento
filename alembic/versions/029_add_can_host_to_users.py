"""add can_host to users

Revision ID: 029
Revises: 028
Create Date: 2026-10-14 11:31:00.000000

"""
from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

# revision identifiers, used by Alembic.
revision: str = '029'
down_revision: Union[str, Sequence[str], None] = '028'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    users_cols = [col["name"] for col in inspector.get_columns("users")]

    cols_to_add = []
    if "can_host" not in users_cols:
        cols_to_add.append(sa.Column("can_host", sa.Boolean(), server_default="0", nullable=False))
    if "hosting_requested_at" not in users_cols:
        cols_to_add.append(sa.Column("hosting_requested_at", sa.DateTime(timezone=True), nullable=True))
    if "hosting_revoked_at" not in users_cols:
        cols_to_add.append(sa.Column("hosting_revoked_at", sa.DateTime(timezone=True), nullable=True))

    if cols_to_add:
        with op.batch_alter_table("users") as batch_op:
            for col in cols_to_add:
                batch_op.add_column(col)

    owner_ids = [
        row[0]
        for row in conn.execute(
            sa.select(sa.distinct(sa.column("user_id")))
            .select_from(sa.table("event_memberships", sa.column("user_id"), sa.column("role")))
            .where(sa.column("role") == "event_owner")
        ).fetchall()
    ]
    logger.info("Backfilling can_host=True for %d event owners", len(owner_ids))
    if owner_ids:
        users_t = sa.table("users", sa.column("id"), sa.column("can_host"))
        conn.execute(
            sa.update(users_t).where(users_t.c.id.in_(owner_ids)).values(can_host=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    users_cols = [col["name"] for col in inspector.get_columns("users")]

    cols_to_drop = [c for c in ("hosting_revoked_at", "hosting_requested_at", "can_host") if c in users_cols]
    if cols_to_drop:
        with op.batch_alter_table("users") as batch_op:
            for col in cols_to_drop:
                batch_op.drop_column(col)
