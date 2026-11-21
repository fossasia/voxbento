"""Index durable queue discovery and track when report execution starts."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meeting_reports", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_meeting_reports_status", "meeting_reports", ["status", "id"])
    op.create_index("ix_webhook_deliveries_due", "webhook_deliveries", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_index("ix_meeting_reports_status", table_name="meeting_reports")
    with op.batch_alter_table("meeting_reports") as batch:
        batch.drop_column("started_at")
