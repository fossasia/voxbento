"""Persist when a new user's delayed onboarding email is due."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("onboarding_email_due_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_users_onboarding_email_due_at"), "users", ["onboarding_email_due_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_onboarding_email_due_at"), table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("onboarding_email_due_at")
