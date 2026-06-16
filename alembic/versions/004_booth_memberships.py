"""Add booth_memberships table.

- Create booth_memberships table (user_id, booth_id, role)

Revision ID: 004
Revises: 003
Create Date: 2026-06-01
"""

import sqlalchemy as sa

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create booth_memberships table
    op.create_table(
        "booth_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("booth_id", sa.Integer(), sa.ForeignKey("booths.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_membership_user_booth", "booth_memberships", ["user_id", "booth_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_membership_user_booth", table_name="booth_memberships")
    op.drop_table("booth_memberships")
