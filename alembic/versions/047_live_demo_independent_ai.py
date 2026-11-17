"""Independent live demo AI settings; retire the room link safely."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE live_demo_settings SET enabled = false, ai_source = 'built_in' WHERE ai_source = 'room'")
    with op.batch_alter_table("live_demo_settings") as batch:
        batch.drop_constraint("fk_live_demo_settings_room_id", type_="foreignkey")
        batch.drop_column("room_id")
        batch.add_column(sa.Column("speech_choice", sa.Text(), nullable=False, server_default="local|base"))
        batch.add_column(
            sa.Column("translation_choice", sa.Text(), nullable=False, server_default="local|nllb-200-distilled-600M")
        )
        batch.add_column(sa.Column("voice_provider", sa.Text(), nullable=False, server_default="supertonic"))
        batch.add_column(sa.Column("voice", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("persona", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("style", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("encrypted_speech_key", sa.Text(), nullable=True))
        batch.add_column(sa.Column("encrypted_translation_key", sa.Text(), nullable=True))
        batch.add_column(sa.Column("encrypted_voice_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("UPDATE live_demo_settings SET enabled = false, ai_source = 'built_in' WHERE ai_source = 'custom'")
    with op.batch_alter_table("live_demo_settings") as batch:
        batch.add_column(sa.Column("room_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_live_demo_settings_room_id", "rooms", ["room_id"], ["id"], ondelete="SET NULL")
        batch.drop_column("speech_choice")
        batch.drop_column("translation_choice")
        batch.drop_column("voice_provider")
        batch.drop_column("voice")
        batch.drop_column("persona")
        batch.drop_column("style")
        batch.drop_column("encrypted_speech_key")
        batch.drop_column("encrypted_translation_key")
        batch.drop_column("encrypted_voice_key")
