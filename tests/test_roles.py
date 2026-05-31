"""Tests for portal.roles — permission helpers and role model.

Mirrors the testing conventions in test_booth_state.py and
test_booth_identity.py: flat functions grouped by topic, ``assert``
statements, ``pytest.mark.parametrize`` for combinatorial coverage.
"""

from __future__ import annotations

import pytest

from portal.booth_state import ParticipantRole
from portal.roles import (
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
    assert ALL_ROLES == frozenset({
        'super_admin',
        'event_admin',
        'coordinator',
        'interpreter',
        'listener',
    })


def test_role_permissions_covers_all_roles():
    """Every role in ALL_ROLES must have a ROLE_PERMISSIONS entry."""
    assert set(ROLE_PERMISSIONS.keys()) == ALL_ROLES


def test_admin_roles_subset_of_all_roles():
    assert ADMIN_ROLES.issubset(ALL_ROLES)


# ── Permission enum ─────────────────────────────────────────────────────


def test_permission_enum_values_use_dot_notation():
    for perm in Permission:
        assert '.' in perm.value, f'{perm.name} value should use dot notation'


def test_permission_enum_has_expected_members():
    names = {p.name for p in Permission}
    assert 'BOOTH_GO_LIVE' in names
    assert 'BOOTH_SET_ACTIVE' in names
    assert 'BOOTH_CHAT_SEND' in names
    assert 'BOOTH_VIEW' in names
    assert 'ADMIN_MANAGE_BOOTHS' in names
    assert 'ADMIN_MANAGE_EVENTS' in names


# ── has_permission (the primitive) ───────────────────────────────────────


@pytest.mark.parametrize('role', list(ALL_ROLES))
def test_has_permission_booth_view_all_roles(role: ParticipantRole):
    """Every role can view a booth."""
    assert has_permission(role, Permission.BOOTH_VIEW) is True


@pytest.mark.parametrize('role', list(ALL_ROLES))
def test_has_permission_booth_chat_all_roles(role: ParticipantRole):
    """Every role can send chat messages."""
    assert has_permission(role, Permission.BOOTH_CHAT_SEND) is True


def test_has_permission_unknown_role_returns_false():
    assert has_permission('unknown_role', Permission.BOOTH_VIEW) is False  # type: ignore[arg-type]


# ── super_admin gets everything ──────────────────────────────────────────


def test_super_admin_has_all_permissions():
    for perm in Permission:
        assert has_permission('super_admin', perm) is True


# ── can_go_live ──────────────────────────────────────────────────────────


ROLES_THAT_CAN_GO_LIVE = {'interpreter', 'event_admin', 'super_admin'}
ROLES_THAT_CANNOT_GO_LIVE = {'coordinator', 'listener'}


@pytest.mark.parametrize('role', list(ROLES_THAT_CAN_GO_LIVE))
def test_can_go_live_granted(role: ParticipantRole):
    assert can_go_live(role) is True


@pytest.mark.parametrize('role', list(ROLES_THAT_CANNOT_GO_LIVE))
def test_can_go_live_denied(role: ParticipantRole):
    assert can_go_live(role) is False


# ── can_set_active ───────────────────────────────────────────────────────


ROLES_THAT_CAN_SET_ACTIVE = {'coordinator', 'event_admin', 'super_admin'}
ROLES_THAT_CANNOT_SET_ACTIVE = {'interpreter', 'listener'}


@pytest.mark.parametrize('role', list(ROLES_THAT_CAN_SET_ACTIVE))
def test_can_set_active_granted(role: ParticipantRole):
    assert can_set_active(role) is True


@pytest.mark.parametrize('role', list(ROLES_THAT_CANNOT_SET_ACTIVE))
def test_can_set_active_denied(role: ParticipantRole):
    assert can_set_active(role) is False


# ── can_manage_booths ────────────────────────────────────────────────────


ROLES_THAT_CAN_MANAGE_BOOTHS = {'event_admin', 'super_admin'}
ROLES_THAT_CANNOT_MANAGE_BOOTHS = {'coordinator', 'interpreter', 'listener'}


@pytest.mark.parametrize('role', list(ROLES_THAT_CAN_MANAGE_BOOTHS))
def test_can_manage_booths_granted(role: ParticipantRole):
    assert can_manage_booths(role) is True


@pytest.mark.parametrize('role', list(ROLES_THAT_CANNOT_MANAGE_BOOTHS))
def test_can_manage_booths_denied(role: ParticipantRole):
    assert can_manage_booths(role) is False


# ── can_manage_events ────────────────────────────────────────────────────


def test_can_manage_events_super_admin():
    assert can_manage_events('super_admin') is True


@pytest.mark.parametrize('role', ['event_admin', 'coordinator', 'interpreter', 'listener'])
def test_can_manage_events_denied(role: ParticipantRole):
    assert can_manage_events(role) is False


# ── is_admin_role ────────────────────────────────────────────────────────


@pytest.mark.parametrize('role', ['super_admin', 'event_admin'])
def test_is_admin_role_true(role: ParticipantRole):
    assert is_admin_role(role) is True


@pytest.mark.parametrize('role', ['coordinator', 'interpreter', 'listener'])
def test_is_admin_role_false(role: ParticipantRole):
    assert is_admin_role(role) is False


# ── ROLE_PERMISSIONS structure ───────────────────────────────────────────


def test_role_permissions_are_frozensets():
    for role, perms in ROLE_PERMISSIONS.items():
        assert isinstance(perms, frozenset), f'{role} permissions should be frozenset'


def test_privilege_escalation_hierarchy():
    """Higher-privilege roles must be a superset of lower-privilege roles.

    super_admin ⊇ event_admin ⊇ coordinator (for booth-level permissions).
    """
    sa = ROLE_PERMISSIONS['super_admin']
    ea = ROLE_PERMISSIONS['event_admin']
    coord = ROLE_PERMISSIONS['coordinator']
    listener = ROLE_PERMISSIONS['listener']

    assert listener.issubset(coord), 'listener perms must be subset of coordinator'
    assert coord.issubset(ea), 'coordinator perms must be subset of event_admin'
    assert ea.issubset(sa), 'event_admin perms must be subset of super_admin'


def test_interpreter_is_not_subset_of_coordinator():
    """Interpreter has BOOTH_GO_LIVE which coordinator lacks.

    This verifies the intentional divergence: interpreters can go live
    but cannot set active, while coordinators can set active but cannot
    go live.
    """
    interp = ROLE_PERMISSIONS['interpreter']
    coord = ROLE_PERMISSIONS['coordinator']
    assert not interp.issubset(coord)
    assert not coord.issubset(interp)


# ── Backward compatibility with existing booth_state.py ──────────────────


def test_existing_booth_roles_still_valid():
    """The three original booth roles must remain valid ParticipantRole values."""
    original_roles = ['interpreter', 'coordinator', 'listener']
    for role in original_roles:
        assert role in ALL_ROLES


def test_new_admin_roles_are_valid():
    """The two new admin roles must be valid ParticipantRole values."""
    assert 'event_admin' in ALL_ROLES
    assert 'super_admin' in ALL_ROLES
