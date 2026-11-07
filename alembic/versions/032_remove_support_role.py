"""Remove the unused Support role (#757).

VoxBento has four roles (Admin, Event owner, Room coordinator, Interpreter).
Support was only ever assigned automatically to a third-party app's developer,
and that assignment failed, so this normally deletes nothing. Any rows that do
exist are removed rather than mapped to another role, because Support granted
no more than viewing booths.

Event memberships spelled with the old "owner" / "super_admin" aliases become
"event_owner". Invite links only give booth roles (Room coordinator,
Interpreter), so links carrying any other role are deleted.

The downgrade does not restore deleted rows.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# One literal statement per table, so no SQL is built from strings.
DELETE_SUPPORT_ROWS = {
    "event_memberships": "DELETE FROM event_memberships WHERE role = 'support'",
    "room_memberships": "DELETE FROM room_memberships WHERE role = 'support'",
    "booth_memberships": "DELETE FROM booth_memberships WHERE role = 'support'",
}


def _log(count: int, message: str, table: str) -> None:
    if count:
        logger.info(message, count, table)


def upgrade() -> None:
    conn = op.get_bind()
    existing = set(sa.inspect(conn).get_table_names())

    # Old spellings of Event owner that a synced event membership might carry.
    if "event_memberships" in existing:
        result = conn.execute(
            sa.text("UPDATE event_memberships SET role = 'event_owner' WHERE role IN ('owner', 'super_admin')")
        )
        _log(result.rowcount, "Renamed %d old Event owner rows in %s", "event_memberships")

    for table, statement in DELETE_SUPPORT_ROWS.items():
        if table in existing:
            result = conn.execute(sa.text(statement))
            _log(result.rowcount, "Removed %d Support role rows from %s", table)

    # Invite links only give booth roles now; older links could carry any role.
    if "invite_tokens" in existing:
        result = conn.execute(
            sa.text("DELETE FROM invite_tokens WHERE role NOT IN ('room_coordinator', 'interpreter')")
        )
        _log(result.rowcount, "Removed %d invite links with a non-booth role from %s", "invite_tokens")


def downgrade() -> None:
    pass
