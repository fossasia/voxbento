"""meeting reports phase 2: automatic reports and reports from translations

Revision ID: 048
Revises: 047
Create Date: 2026-11-22 12:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "048"
down_revision: Union[str, Sequence[str], None] = "047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    event_cols = {col["name"] for col in inspector.get_columns("events")}
    if "meeting_analysis_auto" not in event_cols:
        with op.batch_alter_table("events") as batch_op:
            batch_op.add_column(
                sa.Column("meeting_analysis_auto", sa.Boolean(), nullable=False, server_default=sa.false())
            )

    report_cols = {col["name"] for col in inspector.get_columns("meeting_reports")}
    with op.batch_alter_table("meeting_reports") as batch_op:
        if "transcript_language" not in report_cols:
            batch_op.add_column(sa.Column("transcript_language", sa.String(length=20), nullable=True))
        if "automatic" not in report_cols:
            batch_op.add_column(sa.Column("automatic", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("meeting_reports") as batch_op:
        batch_op.drop_column("automatic")
        batch_op.drop_column("transcript_language")
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("meeting_analysis_auto")
