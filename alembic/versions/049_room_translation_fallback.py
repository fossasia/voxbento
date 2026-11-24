"""Optional per-room fallback translation engine."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.add_column(sa.Column("translation_fallback_provider", sa.String(50), nullable=True))
        batch.add_column(sa.Column("translation_fallback_model", sa.String(100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.drop_column("translation_fallback_model")
        batch.drop_column("translation_fallback_provider")
