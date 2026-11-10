"""Store hosting access requests made from the Account page (#768)."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hosting_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization", sa.String(200), nullable=False),
        sa.Column("event_name", sa.String(200), nullable=False),
        sa.Column("event_starts_on", sa.Date(), nullable=True),
        sa.Column("event_ends_on", sa.Date(), nullable=True),
        sa.Column("languages", sa.String(500), nullable=False),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("decline_reason", sa.Text(), nullable=True),
        sa.Column(
            "decided_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_hosting_requests_user_id", "hosting_requests", ["user_id"])
    op.create_index("ix_hosting_requests_status", "hosting_requests", ["status"])
    op.create_index(
        "uq_hosting_requests_open_user",
        "hosting_requests",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_hosting_requests_open_user", table_name="hosting_requests")
    op.drop_index("ix_hosting_requests_status", table_name="hosting_requests")
    op.drop_index("ix_hosting_requests_user_id", table_name="hosting_requests")
    op.drop_table("hosting_requests")
