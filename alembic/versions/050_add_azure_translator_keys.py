"""add Azure Translator credentials

Adds the encrypted Azure Translator API key and subscription region to events.

Revision ID: 050
Revises: 049
Create Date: 2026-11-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "050"
down_revision: Union[str, Sequence[str], None] = "049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(sa.Column("azure_translator_api_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("azure_translator_region", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("azure_translator_region")
        batch_op.drop_column("azure_translator_api_key")
