"""Role definitions and permission helpers for the interpretation portal.

Mirrors the Eventyay backend ``core/permissions.py`` pattern:
- ``Permission`` enum with ``SCOPE_ACTION`` naming.
- ``ROLE_PERMISSIONS`` dict mapping each role to its granted permissions.
- Standalone helper functions for use in FastAPI dependency injection.

The interpretation portal defines five roles ordered by privilege:

    super_admin > event_owner > room_coordinator > interpreter

Booth-level roles (interpreter) govern in-booth
actions. Admin roles (event_owner, super_admin) govern administrative
operations and inherit room_coordinator permissions within booths.
"""

from __future__ import annotations

from enum import Enum

from portal.booth_state import ParticipantRole

# ---------------------------------------------------------------------------
# Permission enum
# ---------------------------------------------------------------------------


class Permission(Enum):
    """Granular permissions for the interpretation portal.

    Naming convention follows Eventyay: ``SCOPE_ACTION`` in uppercase.
    String values use ``scope.action`` dot notation.
    """

    # Booth-level permissions
    BOOTH_GO_LIVE = "booth.go_live"
    BOOTH_SET_ACTIVE = "booth.set_active"
    BOOTH_CHAT_SEND = "booth.chat_send"
    BOOTH_VIEW = "booth.view"

    # Admin permissions
    ADMIN_MANAGE_BOOTHS = "admin.manage_booths"
    ADMIN_MANAGE_EVENTS = "admin.manage_events"


# ---------------------------------------------------------------------------
# Role → Permission mapping
# ---------------------------------------------------------------------------

ROLE_PERMISSIONS: dict[ParticipantRole, frozenset[Permission]] = {
    "super_admin": frozenset(Permission),
    "event_owner": frozenset(
        {
            Permission.BOOTH_GO_LIVE,
            Permission.BOOTH_SET_ACTIVE,
            Permission.BOOTH_CHAT_SEND,
            Permission.BOOTH_VIEW,
            Permission.ADMIN_MANAGE_BOOTHS,
        }
    ),
    "room_coordinator": frozenset(
        {
            Permission.BOOTH_GO_LIVE,
            Permission.BOOTH_SET_ACTIVE,
            Permission.BOOTH_CHAT_SEND,
            Permission.BOOTH_VIEW,
        }
    ),
    "interpreter": frozenset(
        {
            Permission.BOOTH_GO_LIVE,
            Permission.BOOTH_CHAT_SEND,
            Permission.BOOTH_VIEW,
        }
    ),
}

# Roles considered administrative (mirrors Eventyay's ``ORGANIZER_ROLES``).
ADMIN_ROLES: frozenset[ParticipantRole] = frozenset({"super_admin", "event_owner"})

# All valid role values as a frozenset for quick membership testing.
ALL_ROLES: frozenset[ParticipantRole] = frozenset(ROLE_PERMISSIONS.keys())

_ROLE_RANK = {
    "super_admin": 50,
    "event_owner": 40,
    "room_coordinator": 30,
    "interpreter": 20,
}


# ---------------------------------------------------------------------------
# Permission helper functions
# ---------------------------------------------------------------------------


def has_permission(role: ParticipantRole, permission: Permission) -> bool:
    """Return True if *role* is granted *permission*."""
    perms = ROLE_PERMISSIONS.get(role)
    if perms is None:
        return False
    return permission in perms


def can_go_live(role: ParticipantRole) -> bool:
    """True if *role* may publish audio (activate WHIP ingest).

    Only interpreters can go live.  Admin roles have the permission in the
    mapping for completeness, but in practice they do not hold microphones.
    """
    return has_permission(role, Permission.BOOTH_GO_LIVE)


def can_set_active(role: ParticipantRole) -> bool:
    """True if *role* may assign the active interpreter."""
    return has_permission(role, Permission.BOOTH_SET_ACTIVE)


def can_manage_booths(role: ParticipantRole) -> bool:
    """True if *role* may create/delete booths and generate invite tokens."""
    return has_permission(role, Permission.ADMIN_MANAGE_BOOTHS)


def can_manage_events(role: ParticipantRole) -> bool:
    """True if *role* may create/delete events."""
    return has_permission(role, Permission.ADMIN_MANAGE_EVENTS)


def is_admin_role(role: ParticipantRole) -> bool:
    """True if *role* is an administrative role."""
    return role in ADMIN_ROLES
