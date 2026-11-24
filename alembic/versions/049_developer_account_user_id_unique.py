"""Enforce unique developer account per user (#1368 review)

Revision ID: 049
Revises: 048
Create Date: 2026-11-24 09:58:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "049"
down_revision: Union[str, Sequence[str], None] = "048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Deduplicate any existing developer accounts per user, keeping approved accounts first, then latest
    op.execute("""
        DELETE FROM developer_accounts WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY user_id
                    ORDER BY CASE WHEN status = 'approved' THEN 1 ELSE 2 END, id DESC
                ) as rn
                FROM developer_accounts
            ) WHERE rn = 1
        )
    """)
    with op.batch_alter_table("developer_accounts") as batch:
        batch.drop_index("ix_developer_accounts_user_id")
        batch.create_index("ix_developer_accounts_user_id", ["user_id"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("developer_accounts") as batch:
        batch.drop_index("ix_developer_accounts_user_id")
        batch.create_index("ix_developer_accounts_user_id", ["user_id"], unique=False)
