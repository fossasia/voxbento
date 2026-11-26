"""Two-way booths: a booth's return language and its live output direction (#1397)."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "return_language_code" not in {column["name"] for column in inspector.get_columns("booths")}:
        with op.batch_alter_table("booths") as batch:
            batch.add_column(sa.Column("return_language_code", sa.String(2), nullable=True))
    if "output_language_code" not in {column["name"] for column in inspector.get_columns("booth_live_state")}:
        with op.batch_alter_table("booth_live_state") as batch:
            batch.add_column(sa.Column("output_language_code", sa.String(2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("booth_live_state") as batch:
        batch.drop_column("output_language_code")
    with op.batch_alter_table("booths") as batch:
        batch.drop_column("return_language_code")
