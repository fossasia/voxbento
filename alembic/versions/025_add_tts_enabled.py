"""add tts_enabled to room translation languages

Revision ID: 025
Revises: 024
Create Date: 2026-09-08 21:46:15.773928

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "025"
down_revision: Union[str, Sequence[str], None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "room_translation_languages",
        sa.Column("tts_enabled", sa.Boolean(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("room_translation_languages", "tts_enabled")
