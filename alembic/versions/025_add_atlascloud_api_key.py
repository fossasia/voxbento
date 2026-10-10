"""add Atlas Cloud API key

Widens booths.transcription_model (provider model ids such as bytedance/seed-asr-2.0 are 22
characters, longer than the old 20) and adds the encrypted Atlas Cloud key to events.

Revision ID: 025
Revises: 024
Create Date: 2026-09-08

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "025"
down_revision: Union[str, Sequence[str], None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(sa.Column("atlascloud_api_key", sa.Text(), nullable=True))

    with op.batch_alter_table("booths") as batch_op:
        batch_op.alter_column(
            "transcription_model",
            existing_type=sa.String(length=20),
            type_=sa.String(length=40),
            existing_nullable=False,
            existing_server_default=sa.text("'tiny'"),
        )


def downgrade() -> None:
    # booths.transcription_model is left at the wider length: shrinking it could truncate stored ids.
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("atlascloud_api_key")
