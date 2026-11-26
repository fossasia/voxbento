"""Runtime floor language: Mission Control's choice per room and the real language of each segment (#1397)."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "floor_language_override" not in {column["name"] for column in inspector.get_columns("rooms")}:
        with op.batch_alter_table("rooms") as batch:
            batch.add_column(sa.Column("floor_language_override", sa.String(10), nullable=True))
    if "source_language_code" not in {column["name"] for column in inspector.get_columns("transcript_segments")}:
        with op.batch_alter_table("transcript_segments") as batch:
            batch.add_column(sa.Column("source_language_code", sa.String(10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("transcript_segments") as batch:
        batch.drop_column("source_language_code")
    with op.batch_alter_table("rooms") as batch:
        batch.drop_column("floor_language_override")
