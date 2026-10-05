"""Tests for portal.roles — permission helpers and role model.

Mirrors the testing conventions in test_booth_state.py and
test_booth_identity.py: flat functions grouped by topic, ``assert``
statements, ``pytest.mark.parametrize`` for combinatorial coverage.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import portal.auth as auth_module
from portal.auth import can_perform_role, resolve_booth_role
from portal.booth_state import ParticipantRole
from portal.roles import (
    _ROLE_RANK,
    ADMIN_ROLES,
    ALL_ROLES,
    ROLE_PERMISSIONS,
    Permission,
    can_go_live,
    can_manage_booths,
    can_manage_events,
    can_set_active,
    has_permission,
    is_admin_role,
)

# ── Role type completeness ──────────────────────────────────────────────


def test_all_roles_has_five_entries():
    assert len(ALL_ROLES) == 5


def test_all_roles_values():
    assert ALL_ROLES == frozenset(
        {
            "super_admin",
            "event_owner",
            "room_coordinator",
            "interpreter",
            "support",
        }
    )


def test_role_permissions_covers_all_roles():
    """Every role in ALL_ROLES must have a ROLE_PERMISSIONS entry."""
    assert set(ROLE_PERMISSIONS.keys()) == ALL_ROLES


def test_admin_roles_subset_of_all_roles():
    assert ADMIN_ROLES.issubset(ALL_ROLES)


# ── Permission enum ─────────────────────────────────────────────────────


def test_permission_enum_values_use_dot_notation():
    for perm in Permission:
        assert "." in perm.value, f"{perm.name} value should use dot notation"


def test_permission_enum_has_expected_members():
    names = {p.name for p in Permission}
    assert "BOOTH_GO_LIVE" in names
    assert "BOOTH_SET_ACTIVE" in names
    assert "BOOTH_CHAT_SEND" in names
    assert "BOOTH_VIEW" in names
    assert "ADMIN_MANAGE_BOOTHS" in names
    assert "ADMIN_MANAGE_EVENTS" in names


# ── has_permission (the primitive) ───────────────────────────────────────


@pytest.mark.parametrize("role", list(ALL_ROLES))
def test_has_permission_booth_view_all_roles(role: ParticipantRole):
    """Every role can view a booth."""
    assert has_permission(role, Permission.BOOTH_VIEW) is True


@pytest.mark.parametrize("role", list(ALL_ROLES - {"support"}))
def test_has_permission_booth_chat_some_roles(role: ParticipantRole):
    """Every role except support can send chat messages."""
    assert has_permission(role, Permission.BOOTH_CHAT_SEND) is True


def test_has_permission_unknown_role_returns_false():
    assert has_permission("unknown_role", Permission.BOOTH_VIEW) is False  # type: ignore[arg-type]


# ── super_admin gets everything ──────────────────────────────────────────


def test_super_admin_has_all_permissions():
    for perm in Permission:
        assert has_permission("super_admin", perm) is True


# ── can_go_live ──────────────────────────────────────────────────────────


ROLES_THAT_CAN_GO_LIVE = {"room_coordinator", "interpreter", "event_owner", "super_admin"}
ROLES_THAT_CANNOT_GO_LIVE = set()


@pytest.mark.parametrize("role", list(ROLES_THAT_CAN_GO_LIVE))
def test_can_go_live_granted(role: ParticipantRole):
    assert can_go_live(role) is True


# ── can_set_active ───────────────────────────────────────────────────────


ROLES_THAT_CAN_SET_ACTIVE = {"room_coordinator", "event_owner", "super_admin"}
ROLES_THAT_CANNOT_SET_ACTIVE = {"interpreter"}


@pytest.mark.parametrize("role", list(ROLES_THAT_CAN_SET_ACTIVE))
def test_can_set_active_granted(role: ParticipantRole):
    assert can_set_active(role) is True


@pytest.mark.parametrize("role", list(ROLES_THAT_CANNOT_SET_ACTIVE))
def test_can_set_active_denied(role: ParticipantRole):
    assert can_set_active(role) is False


# ── can_manage_booths ────────────────────────────────────────────────────


ROLES_THAT_CAN_MANAGE_BOOTHS = {"event_owner", "super_admin"}
ROLES_THAT_CANNOT_MANAGE_BOOTHS = {"interpreter", "room_coordinator"}


@pytest.mark.parametrize("role", list(ROLES_THAT_CAN_MANAGE_BOOTHS))
def test_can_manage_booths_granted(role: ParticipantRole):
    assert can_manage_booths(role) is True


@pytest.mark.parametrize("role", list(ROLES_THAT_CANNOT_MANAGE_BOOTHS))
def test_can_manage_booths_denied(role: ParticipantRole):
    assert can_manage_booths(role) is False


# ── can_manage_events ────────────────────────────────────────────────────


def test_can_manage_events_super_admin():
    assert can_manage_events("super_admin") is True


@pytest.mark.parametrize("role", ["event_owner", "room_coordinator", "interpreter"])
def test_can_manage_events_denied(role: ParticipantRole):
    assert can_manage_events(role) is False


# ── is_admin_role ────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["super_admin", "event_owner"])
def test_is_admin_role_true(role: ParticipantRole):
    assert is_admin_role(role) is True


@pytest.mark.parametrize("role", ["interpreter", "room_coordinator"])
def test_is_admin_role_false(role: ParticipantRole):
    assert is_admin_role(role) is False


# ── ROLE_PERMISSIONS structure ───────────────────────────────────────────


def test_role_permissions_are_frozensets():
    for role, perms in ROLE_PERMISSIONS.items():
        assert isinstance(perms, frozenset), f"{role} permissions should be frozenset"


def test_privilege_escalation_hierarchy():
    """Higher-privilege roles must be a superset of lower-privilege roles.

    super_admin ⊇ event_owner ⊇ room_coordinator (for booth-level permissions).
    """
    sa = ROLE_PERMISSIONS["super_admin"]
    ea = ROLE_PERMISSIONS["event_owner"]
    coord = ROLE_PERMISSIONS["room_coordinator"]

    assert ROLE_PERMISSIONS["interpreter"].issubset(coord), "interpreter perms must be subset of room_coordinator"
    assert coord.issubset(ea), "room_coordinator perms must be subset of event_owner"
    assert coord.issubset(ea), "room_coordinator perms must be subset of event_owner"
    assert ea.issubset(sa), "event_owner perms must be subset of super_admin"


def test_interpreter_is_subset_of_coordinator():
    """Interpreter has BOOTH_GO_LIVE, which room_coordinator now also has.

    This means interpreter permissions are a strict subset of room_coordinator permissions.
    """
    interp = ROLE_PERMISSIONS["interpreter"]
    coord = ROLE_PERMISSIONS["room_coordinator"]
    assert interp.issubset(coord)
    assert not coord.issubset(interp)


# ── Backward compatibility with existing booth_state.py ──────────────────


def test_existing_booth_roles_still_valid():
    """The original booth role must remain valid ParticipantRole values."""
    original_roles = ["interpreter"]
    for role in original_roles:
        assert role in ALL_ROLES


def test_new_admin_roles_are_valid():
    """The new admin roles must be valid ParticipantRole values."""
    assert "event_owner" in ALL_ROLES
    assert "super_admin" in ALL_ROLES
    assert "room_coordinator" in ALL_ROLES


# ── Role ranking ─────────────────────────────────────────────────────────


def _rank_pairs():
    """Every (weaker, stronger) pair where one role's permissions strictly contain the other's."""
    return [
        (weaker, stronger)
        for weaker in ALL_ROLES
        for stronger in ALL_ROLES
        if ROLE_PERMISSIONS[weaker] < ROLE_PERMISSIONS[stronger]
    ]


@pytest.mark.parametrize("weaker,stronger", _rank_pairs())
def test_rank_follows_permission_sets(weaker, stronger):
    """A role that can do strictly less must rank strictly lower.

    resolve_booth_role picks a user's highest-ranked role, so a rank that
    disagrees with the permission sets demotes anyone holding both roles.
    """
    assert _ROLE_RANK[weaker] < _ROLE_RANK[stronger]


def test_support_is_the_lowest_ranked_role():
    assert _ROLE_RANK["support"] == min(_ROLE_RANK.values())


def test_every_role_has_a_rank():
    assert set(_ROLE_RANK) == set(ALL_ROLES)


def test_auth_uses_the_same_rank_table():
    """auth.py used to keep its own copy, which drifted and omitted support."""
    assert auth_module._ROLE_RANK is _ROLE_RANK


# ── can_perform_role ─────────────────────────────────────────────────────


@pytest.mark.parametrize("requested", ["interpreter", "room_coordinator", "event_owner", "super_admin"])
def test_support_cannot_perform_a_booth_or_admin_role(requested):
    assert can_perform_role("support", requested) is False


def test_support_can_act_as_itself():
    assert can_perform_role("support", "support") is True


def test_coordinator_can_join_as_interpreter():
    assert can_perform_role("room_coordinator", "interpreter") is True


def test_interpreter_cannot_join_as_coordinator():
    assert can_perform_role("interpreter", "room_coordinator") is False


def test_no_role_can_perform_nothing():
    assert can_perform_role(None, "interpreter") is False


# ── resolve_booth_role ───────────────────────────────────────────────────

BOOTH_ID = "pycon2026-3-en"
_BOOTH = SimpleNamespace(id=5, room_id=3, event_id=9)


@asynccontextmanager
async def _fake_session():
    session = MagicMock()
    result = MagicMock()
    result.first.return_value = _BOOTH
    session.scalars = AsyncMock(return_value=result)
    yield session


def _memberships(booth_role=None, room_role=None, event_role=None):
    """Patch the membership lookups resolve_booth_role reads from."""
    booth = [SimpleNamespace(booth_id=_BOOTH.id, role=booth_role)] if booth_role else []
    room = [SimpleNamespace(room_id=_BOOTH.room_id, role=room_role)] if room_role else []
    event = [SimpleNamespace(event_id=_BOOTH.event_id, role=event_role)] if event_role else []
    return (
        patch("portal.database.get_session", _fake_session),
        patch("portal.database.list_booth_memberships_for_user", AsyncMock(return_value=booth)),
        patch("portal.database.list_room_memberships_for_user", AsyncMock(return_value=room)),
        patch("portal.database.list_memberships_for_user", AsyncMock(return_value=event)),
    )


async def _resolve(**memberships):
    patches = _memberships(**memberships)
    for p in patches:
        p.start()
    try:
        return await resolve_booth_role({"sub": "7"}, BOOTH_ID)
    finally:
        for p in patches:
            p.stop()


@pytest.mark.anyio
async def test_a_support_membership_does_not_demote_a_room_coordinator():
    # support is given to an OAuth app's developer account at event level; a
    # developer who also coordinates a room must keep coordinator abilities.
    assert await _resolve(room_role="room_coordinator", event_role="support") == "room_coordinator"


@pytest.mark.anyio
async def test_a_support_membership_does_not_demote_an_interpreter():
    assert await _resolve(booth_role="interpreter", event_role="support") == "interpreter"


@pytest.mark.anyio
async def test_an_event_owner_outranks_support():
    assert await _resolve(event_role="event_owner") == "event_owner"


@pytest.mark.anyio
async def test_support_alone_resolves_to_support():
    assert await _resolve(event_role="support") == "support"
