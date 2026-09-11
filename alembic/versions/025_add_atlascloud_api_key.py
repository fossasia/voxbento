"""add Atlas Cloud API key

Revision ID: 025
Revises: 024
Create Date: 2026-09-08

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "025"
down_revision: Union[str, Sequence[str], None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("events", sa.Column("atlascloud_api_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "atlascloud_api_key")
