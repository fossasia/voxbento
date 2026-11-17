"""live demo: run on an event room's AI settings, with a daily cap

``ai_source`` chooses this server's built-in models ("built_in") or the speech, translation
and voice services of ``room_id`` ("room"), which use that room's event API keys.
``daily_minutes`` caps the visitor audio the demo accepts per UTC day.

Revision ID: 046
Revises: 045
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "046"
down_revision: Union[str, Sequence[str], None] = "045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("live_demo_settings") as batch:
        batch.add_column(sa.Column("ai_source", sa.String(length=20), nullable=False, server_default="built_in"))
        batch.add_column(sa.Column("room_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("daily_minutes", sa.Integer(), nullable=False, server_default="120"))
        batch.create_foreign_key("fk_live_demo_settings_room_id", "rooms", ["room_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("live_demo_settings") as batch:
        batch.drop_constraint("fk_live_demo_settings_room_id", type_="foreignkey")
        batch.drop_column("daily_minutes")
        batch.drop_column("room_id")
        batch.drop_column("ai_source")
