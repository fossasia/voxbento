"""add youtube_connections and the room's linked YouTube broadcast (#1087)

Revision ID: 039
Revises: 038
Create Date: 2026-11-01 12:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "039"
down_revision: Union[str, Sequence[str], None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROOM_COLUMNS = (
    ("youtube_broadcast_id", sa.String(length=32)),
    ("youtube_stream_id", sa.String(length=64)),
)


def upgrade() -> None:
    """Create youtube_connections and add the two room columns, skipping what already exists."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "youtube_connections" not in inspector.get_table_names():
        op.create_table(
            "youtube_connections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("channel_title", sa.String(length=200), nullable=False),
            sa.Column("refresh_token_enc", sa.Text(), nullable=False),
            sa.Column("scope", sa.String(length=500), nullable=False),
            sa.Column(
                "connected_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("last_error", sa.String(length=300), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_youtube_connections_event_id", "youtube_connections", ["event_id"], unique=True)

    cols = [col["name"] for col in inspector.get_columns("rooms")]
    missing = [(name, type_) for name, type_ in ROOM_COLUMNS if name not in cols]
    if missing:
        with op.batch_alter_table("rooms") as batch_op:
            for name, type_ in missing:
                batch_op.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    """Drop the room columns and the youtube_connections table."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    cols = [col["name"] for col in inspector.get_columns("rooms")]
    present = [name for name, _ in ROOM_COLUMNS if name in cols]
    if present:
        with op.batch_alter_table("rooms") as batch_op:
            for name in present:
                batch_op.drop_column(name)

    if "youtube_connections" in inspector.get_table_names():
        op.drop_index("ix_youtube_connections_event_id", table_name="youtube_connections")
        op.drop_table("youtube_connections")
