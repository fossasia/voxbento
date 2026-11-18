"""Add configurable email templates and a durable delivery outbox.

Revision ID: 034
Revises: 033
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_templates",
        sa.Column("key", sa.String(length=80), primary_key=True),
        sa.Column("draft_subject", sa.Text(), nullable=True),
        sa.Column("draft_html", sa.Text(), nullable=True),
        sa.Column("published_subject", sa.Text(), nullable=True),
        sa.Column("published_html", sa.Text(), nullable=True),
        sa.Column("published_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "email_template_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_key", sa.String(length=80), sa.ForeignKey("email_templates.key", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("html", sa.Text(), nullable=False),
        sa.Column("published_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("template_key", "version", name="uq_email_template_revision"),
    )
    op.create_index("ix_email_template_revisions_template_key", "email_template_revisions", ["template_key"])
    op.create_table(
        "email_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("show_logo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("logo_url", sa.String(length=500), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "email_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("notification_key", sa.String(length=80), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("encrypted_subject", sa.Text(), nullable=False),
        sa.Column("encrypted_html", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), unique=True, nullable=True),
        # Lets the outbox drop a queued sign-in or invitation link once its token is used, expired or
        # deleted. Not a foreign key on purpose: a deleted token must leave its id behind.
        sa.Column("auth_token_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_email_deliveries_notification_key", "email_deliveries", ["notification_key"])
    op.create_index("ix_email_deliveries_due", "email_deliveries", ["status", "scheduled_at"])
    op.create_index("ix_email_deliveries_created", "email_deliveries", ["created_at"])
    op.create_index("ix_email_deliveries_auth_token_id", "email_deliveries", ["auth_token_id"])


def downgrade() -> None:
    op.drop_table("email_deliveries")
    op.drop_table("email_settings")
    op.drop_table("email_template_revisions")
    op.drop_table("email_templates")
