"""add transcript segment indexes

Revision ID: 039
Revises: 038
Create Date: 2026-10-26 17:10:00.000000

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

ROOM_LANGUAGE_CREATED = "ix_transcript_segments_room_language_created"
BOOTH_ID = "ix_transcript_segments_booth_id"


def _existing_indexes() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("transcript_segments")}


def upgrade() -> None:
    """Index the transcript export predicate and the booth cascade path (#1033).

    ``transcript_segments`` was created in 010 with a primary key and two foreign
    keys but no secondary index, so the export in ``portal/routers/api_v1.py``
    scanned the table and sorted into a temporary B-tree, and the ``ON DELETE
    CASCADE`` from ``booths`` scanned the child table once per deleted parent.
    """
    existing = _existing_indexes()
    if ROOM_LANGUAGE_CREATED not in existing:
        op.create_index(
            ROOM_LANGUAGE_CREATED,
            "transcript_segments",
            ["room_id", "language_code", "created_at"],
        )
    if BOOTH_ID not in existing:
        op.create_index(BOOTH_ID, "transcript_segments", ["booth_id"])


def downgrade() -> None:
    """Drop the transcript segment indexes."""
    existing = _existing_indexes()
    if BOOTH_ID in existing:
        op.drop_index(BOOTH_ID, table_name="transcript_segments")
    if ROOM_LANGUAGE_CREATED in existing:
        op.drop_index(ROOM_LANGUAGE_CREATED, table_name="transcript_segments")
