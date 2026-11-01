"""add transcript segment indexes

Revision ID: 039
Revises: 038
Create Date: 2026-10-26 17:10:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "039"
down_revision: Union[str, Sequence[str], None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
disable_transaction: bool = True

ROOM_LANGUAGE_CREATED = "ix_transcript_segments_room_language_created"
BOOTH_ID = "ix_transcript_segments_booth_id"
MIGRATION_ADVISORY_LOCK_ID = 20261026039


def upgrade() -> None:
    """Index the transcript export predicate and the booth cascade path (#1033).

    ``transcript_segments`` was created in 010 with a primary key and two foreign
    keys but no secondary index, so the export in ``portal/routers/api_v1.py``
    filtered and sorted over the table with nothing to support it, and the
    ``ON DELETE CASCADE`` from ``booths`` scanned the child table once per
    deleted parent.

    ``id`` closes the composite because the export orders by ``(created_at, id)``.
    Without it PostgreSQL still sorts the rows that tie on ``created_at``; SQLite
    hid that because its index entries already carry the rowid.

    ``if_not_exists`` does two jobs. The container command applies migrations on
    startup, so two instances can race between observing an index absent and
    creating it, and a duplicate-index error would abort one migration. It also
    means this revision never reflects the live schema, which is what allows
    ``alembic upgrade --sql`` to emit the statements instead of failing on a mock
    connection.

    Concurrent migrations are serialized using a PostgreSQL session advisory lock
    rather than a table lock, preventing startup races without blocking concurrent
    application reads/writes on ``transcript_segments``. ``postgresql_concurrently``
    ensures index building does not block writes on large tables.
    """
    context = op.get_context()
    if context.dialect.name == "postgresql":
        op.execute(f"SELECT pg_advisory_lock({MIGRATION_ADVISORY_LOCK_ID})")

    try:
        op.create_index(
            ROOM_LANGUAGE_CREATED,
            "transcript_segments",
            ["room_id", "language_code", "created_at", "id"],
            if_not_exists=True,
            postgresql_concurrently=True,
        )
        op.create_index(
            BOOTH_ID,
            "transcript_segments",
            ["booth_id"],
            if_not_exists=True,
            postgresql_concurrently=True,
        )
    finally:
        if context.dialect.name == "postgresql":
            op.execute(f"SELECT pg_advisory_unlock({MIGRATION_ADVISORY_LOCK_ID})")


def downgrade() -> None:
    """Drop the transcript segment indexes."""
    context = op.get_context()
    if context.dialect.name == "postgresql":
        op.execute(f"SELECT pg_advisory_lock({MIGRATION_ADVISORY_LOCK_ID})")

    try:
        op.drop_index(
            BOOTH_ID,
            table_name="transcript_segments",
            if_exists=True,
            postgresql_concurrently=True,
        )
        op.drop_index(
            ROOM_LANGUAGE_CREATED,
            table_name="transcript_segments",
            if_exists=True,
            postgresql_concurrently=True,
        )
    finally:
        if context.dialect.name == "postgresql":
            op.execute(f"SELECT pg_advisory_unlock({MIGRATION_ADVISORY_LOCK_ID})")
