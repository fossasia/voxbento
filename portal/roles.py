"""VoxBento's roles and what each one may do — defined once, used everywhere (#757).

There are four roles. "Admin" is the platform-wide role; the word "admin" means
nothing else in VoxBento. Every other access check is named after the roles it
lets in (for example "owner of this event" or "coordinator of this room").

| Action                                                    | Admin | Event owner | Room coordinator | Interpreter |
|-----------------------------------------------------------|:-----:|:-----------:|:----------------:|:-----------:|
| Manage users, Admins, hosting access and developer apps   |   ✓   |             |                  |             |
| See all events on the platform                            |   ✓   |             |                  |             |
| Create new events (also any user with hosting access)     |   ✓   |             |                  |             |
| Edit and delete their event                               |   ✓   |      ✓      |                  |             |
| Manage rooms, booths, members and invites in their event  |   ✓   |      ✓      |                  |             |
| Manage AI provider keys and API keys of their event       |   ✓   |      ✓      |                  |             |
| Use Mission Control for their event or rooms              |   ✓   |      ✓      |        ✓         |             |
| Start and stop floor transcription in their rooms         |   ✓   |      ✓      |        ✓         |             |
| Choose the active interpreter in a booth                  |   ✓   |      ✓      |        ✓         |             |
| Go live in a booth                                        |   ✓   |      ✓      |        ✓         |      ✓      |
| Use the booth chat                                        |   ✓   |      ✓      |        ✓         |      ✓      |
| View booths of their event                                |   ✓   |      ✓      |        ✓         |      ✓      |
| Can be assigned per                                       | account-wide | event | event, room, booth | event, booth |

Hosting access is an account setting, not a role: it lets a user create events,
and the creator becomes Event owner of each event they create. Listening is open
to everyone with the event link or join code and is not a role either.

Each role holds every permission of the roles below it, so a person holding
several roles in one event gets the permissions of the highest one, which are
the combined permissions of all of them.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Literal

ADMIN = "admin"
EVENT_OWNER = "event_owner"
ROOM_COORDINATOR = "room_coordinator"
INTERPRETER = "interpreter"

Role = Literal["admin", "event_owner", "room_coordinator", "interpreter"]

# Highest first.
ROLES: tuple[Role, ...] = (ADMIN, EVENT_OWNER, ROOM_COORDINATOR, INTERPRETER)
ALL_ROLES: frozenset[Role] = frozenset(ROLES)

ROLE_LABELS: dict[str, str] = {
    ADMIN: "Admin",
    EVENT_OWNER: "Event owner",
    ROOM_COORDINATOR: "Room coordinator",
    INTERPRETER: "Interpreter",
}

# Where each role can be given. Admin is an account setting (users.is_admin).
EVENT_ROLES: frozenset[Role] = frozenset({EVENT_OWNER, ROOM_COORDINATOR, INTERPRETER})
ROOM_ROLES: frozenset[Role] = frozenset({ROOM_COORDINATOR})
BOOTH_ROLES: frozenset[Role] = frozenset({ROOM_COORDINATOR, INTERPRETER})

ASSIGNABLE_LEVELS: dict[str, frozenset[Role]] = {
    "event": EVENT_ROLES,
    "room": ROOM_ROLES,
    "booth": BOOTH_ROLES,
}


class Permission(Enum):
    """One permission per row of the table above (``scope.action``)."""

    MANAGE_PLATFORM = "platform.manage"
    VIEW_ALL_EVENTS = "platform.view_all_events"
    CREATE_EVENTS = "platform.create_events"
    EDIT_EVENT = "event.edit"
    MANAGE_EVENT_TEAM = "event.manage_team"
    MANAGE_EVENT_KEYS = "event.manage_keys"
    USE_MISSION_CONTROL = "room.mission_control"
    CONTROL_FLOOR_TRANSCRIPTION = "room.floor_transcription"
    BOOTH_SET_ACTIVE = "booth.set_active"
    BOOTH_GO_LIVE = "booth.go_live"
    BOOTH_CHAT_SEND = "booth.chat_send"
    BOOTH_VIEW = "booth.view"


_INTERPRETER_PERMISSIONS = frozenset({Permission.BOOTH_GO_LIVE, Permission.BOOTH_CHAT_SEND, Permission.BOOTH_VIEW})
_COORDINATOR_PERMISSIONS = _INTERPRETER_PERMISSIONS | {
    Permission.USE_MISSION_CONTROL,
    Permission.CONTROL_FLOOR_TRANSCRIPTION,
    Permission.BOOTH_SET_ACTIVE,
}
_OWNER_PERMISSIONS = _COORDINATOR_PERMISSIONS | {
    Permission.EDIT_EVENT,
    Permission.MANAGE_EVENT_TEAM,
    Permission.MANAGE_EVENT_KEYS,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    ADMIN: frozenset(Permission),
    EVENT_OWNER: _OWNER_PERMISSIONS,
    ROOM_COORDINATOR: _COORDINATOR_PERMISSIONS,
    INTERPRETER: _INTERPRETER_PERMISSIONS,
}

ROLE_RANK: dict[str, int] = {role: len(ROLES) - index for index, role in enumerate(ROLES)}


def role_label(role: str) -> str:
    """Plain-language name of a role, e.g. "Room coordinator"."""
    return ROLE_LABELS.get(role, role.replace("_", " ").capitalize())


def has_permission(role: str | None, permission: Permission) -> bool:
    """True if *role* is granted *permission*. Unknown roles have none."""
    return permission in ROLE_PERMISSIONS.get(role, frozenset())  # type: ignore[arg-type]


def permissions_for(roles: Iterable[str]) -> frozenset[Permission]:
    """The combined permissions of every role a person holds."""
    combined: set[Permission] = set()
    for role in roles:
        combined |= ROLE_PERMISSIONS.get(role, frozenset())  # type: ignore[arg-type]
    return frozenset(combined)


def highest_role(roles: Iterable[str]) -> str | None:
    """The most capable of *roles* (ignoring unknown ones), or None."""
    known = [role for role in roles if role in ROLE_RANK]
    return max(known, key=ROLE_RANK.__getitem__) if known else None


def is_at_least(granted_role: str | None, requested_role: str) -> bool:
    """True if *granted_role* can do everything *requested_role* can."""
    if granted_role not in ROLE_RANK:
        return False
    return ROLE_RANK[granted_role] >= ROLE_RANK.get(requested_role, len(ROLES) + 1)


def can_go_live(role: str | None) -> bool:
    return has_permission(role, Permission.BOOTH_GO_LIVE)


def can_set_active(role: str | None) -> bool:
    """True if *role* may choose the active interpreter for others."""
    return has_permission(role, Permission.BOOTH_SET_ACTIVE)


def assignment_error(role: str, level: str) -> str | None:
    """Why *role* can't be given at *level* ("event", "room" or "booth"), or None if it can."""
    allowed = ASSIGNABLE_LEVELS.get(level, frozenset())
    if role in allowed:
        return None
    if role not in ALL_ROLES:
        return f"Unknown role. Choose one of: {', '.join(role_label(r) for r in ROLES if r in allowed)}."
    where = {"event": "an event", "room": "a room", "booth": "a booth"}.get(level, level)
    return (
        f"{role_label(role)} can't be given to {where}. "
        f"Choose one of: {', '.join(role_label(r) for r in ROLES if r in allowed)}."
    )
