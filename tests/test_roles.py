"""portal.roles: the one definition of VoxBento's roles and permissions (#757)."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from portal.auth import can_perform_role, resolve_booth_role
from portal.roles import (
    ADMIN,
    ALL_ROLES,
    BOOTH_ROLES,
    EVENT_OWNER,
    EVENT_ROLES,
    INTERPRETER,
    ROLE_LABELS,
    ROLE_PERMISSIONS,
    ROLE_RANK,
    ROLES,
    ROOM_COORDINATOR,
    ROOM_ROLES,
    Permission,
    assignment_error,
    can_go_live,
    can_set_active,
    has_permission,
    highest_role,
    is_at_least,
    permissions_for,
    role_label,
)

ROOT = Path(__file__).resolve().parent.parent

# The issue's table: which roles may do each action.
TABLE = {
    Permission.MANAGE_PLATFORM: {ADMIN},
    Permission.VIEW_ALL_EVENTS: {ADMIN},
    Permission.CREATE_EVENTS: {ADMIN},
    Permission.EDIT_EVENT: {ADMIN, EVENT_OWNER},
    Permission.MANAGE_EVENT_TEAM: {ADMIN, EVENT_OWNER},
    Permission.MANAGE_EVENT_KEYS: {ADMIN, EVENT_OWNER},
    Permission.USE_MISSION_CONTROL: {ADMIN, EVENT_OWNER, ROOM_COORDINATOR},
    Permission.CONTROL_FLOOR_TRANSCRIPTION: {ADMIN, EVENT_OWNER, ROOM_COORDINATOR},
    Permission.BOOTH_SET_ACTIVE: {ADMIN, EVENT_OWNER, ROOM_COORDINATOR},
    Permission.BOOTH_GO_LIVE: {ADMIN, EVENT_OWNER, ROOM_COORDINATOR, INTERPRETER},
    Permission.BOOTH_CHAT_SEND: {ADMIN, EVENT_OWNER, ROOM_COORDINATOR, INTERPRETER},
    Permission.BOOTH_VIEW: {ADMIN, EVENT_OWNER, ROOM_COORDINATOR, INTERPRETER},
}


# ── The four roles ───────────────────────────────────────────────────────


def test_exactly_four_roles_highest_first():
    assert ROLES == ("admin", "event_owner", "room_coordinator", "interpreter")
    assert ALL_ROLES == frozenset(ROLES)


def test_every_role_has_a_label_rank_and_permissions():
    assert set(ROLE_LABELS) == set(ALL_ROLES) == set(ROLE_PERMISSIONS) == set(ROLE_RANK)
    assert ROLE_LABELS == {
        "admin": "Admin",
        "event_owner": "Event owner",
        "room_coordinator": "Room coordinator",
        "interpreter": "Interpreter",
    }


@pytest.mark.parametrize("old", ["support", "super_admin", "owner", "listener", "event_admin", "coordinator"])
def test_retired_and_alias_names_are_not_roles(old):
    assert old not in ALL_ROLES
    assert not has_permission(old, Permission.BOOTH_VIEW)
    assert old not in EVENT_ROLES | ROOM_ROLES | BOOTH_ROLES


# ── Permissions follow the table exactly ────────────────────────────────


def test_table_covers_every_permission():
    assert set(TABLE) == set(Permission)


@pytest.mark.parametrize("permission", list(Permission), ids=lambda p: p.name)
@pytest.mark.parametrize("role", ROLES)
def test_permission_matches_table(permission, role):
    assert has_permission(role, permission) is (role in TABLE[permission])


def test_unknown_role_has_no_permissions():
    assert permissions_for(["nobody"]) == frozenset()
    assert has_permission(None, Permission.BOOTH_VIEW) is False


@pytest.mark.parametrize("weaker,stronger", list(zip(ROLES[1:], ROLES[:-1], strict=True)))
def test_each_role_holds_every_permission_of_the_role_below(weaker, stronger):
    assert ROLE_PERMISSIONS[weaker] < ROLE_PERMISSIONS[stronger]
    assert ROLE_RANK[weaker] < ROLE_RANK[stronger]


@pytest.mark.parametrize(
    "held",
    [(INTERPRETER, ROOM_COORDINATOR), (ROOM_COORDINATOR, EVENT_OWNER), (INTERPRETER, EVENT_OWNER, ROOM_COORDINATOR)],
)
def test_several_roles_combine_and_the_highest_role_carries_them_all(held):
    # resolve_booth_role keeps one role per person; it must never be fewer permissions than they hold.
    assert ROLE_PERMISSIONS[highest_role(held)] == permissions_for(held)


def test_booth_shortcuts():
    assert [r for r in ROLES if can_go_live(r)] == list(ROLES)
    assert [r for r in ROLES if can_set_active(r)] == [ADMIN, EVENT_OWNER, ROOM_COORDINATOR]


# ── Where each role can be given ────────────────────────────────────────


def test_assignable_levels_match_the_table():
    assert EVENT_ROLES == {EVENT_OWNER, ROOM_COORDINATOR, INTERPRETER}
    assert ROOM_ROLES == {ROOM_COORDINATOR}
    assert BOOTH_ROLES == {ROOM_COORDINATOR, INTERPRETER}


@pytest.mark.parametrize(
    ("role", "level"),
    [(r, lvl) for lvl, roles in (("event", EVENT_ROLES), ("room", ROOM_ROLES), ("booth", BOOTH_ROLES)) for r in roles],
)
def test_allowed_assignments_have_no_error(role, level):
    assert assignment_error(role, level) is None


@pytest.mark.parametrize(
    ("role", "level", "message"),
    [
        (ADMIN, "event", "Admin can't be given to an event. Choose one of: Event owner, Room coordinator, Interpreter."),
        (EVENT_OWNER, "room", "Event owner can't be given to a room. Choose one of: Room coordinator."),
        (INTERPRETER, "room", "Interpreter can't be given to a room. Choose one of: Room coordinator."),
        (EVENT_OWNER, "booth", "Event owner can't be given to a booth. Choose one of: Room coordinator, Interpreter."),
        ("support", "event", "Unknown role. Choose one of: Event owner, Room coordinator, Interpreter."),
    ],
)
def test_refused_assignments_explain_why(role, level, message):
    assert assignment_error(role, level) == message


def test_role_label_falls_back_for_unknown_values():
    assert role_label("room_coordinator") == "Room coordinator"
    assert role_label("something_else") == "Something else"


# ── can_perform_role (joining a booth as a role) ─────────────────────────


@pytest.mark.parametrize(
    ("granted", "requested", "allowed"),
    [
        (ROOM_COORDINATOR, INTERPRETER, True),
        (INTERPRETER, ROOM_COORDINATOR, False),
        (ADMIN, EVENT_OWNER, True),
        (INTERPRETER, INTERPRETER, True),
        (None, INTERPRETER, False),
        ("support", INTERPRETER, False),
        (INTERPRETER, "support", False),
    ],
)
def test_can_perform_role(granted, requested, allowed):
    assert can_perform_role(granted, requested) is allowed
    assert is_at_least(granted, requested) is allowed


# ── Role names are typed out only in portal/roles.py ─────────────────────

ROLE_LITERAL = re.compile(r"""["'](super_admin|event_owner|room_coordinator|interpreter|support|owner|event_admin)["']""")


def _python_sources():
    for path in [*sorted((ROOT / "portal").rglob("*.py")), ROOT / "fastapi_app.py"]:
        if path.name != "roles.py" and "__pycache__" not in path.parts:
            yield path


def test_no_role_names_are_typed_out_outside_roles_module():
    offenders = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for path in _python_sources()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if ROLE_LITERAL.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == []


def test_browser_code_uses_capabilities_not_role_names():
    for name in ("interpreter-booth.js", "mission-control.js"):
        source = (ROOT / "portal" / "static" / "js" / name).read_text(encoding="utf-8")
        assert not re.search(r"""['"](super_admin|event_owner|room_coordinator|event_admin|coordinator)['"]""", source), name


def test_super_admin_is_gone_from_interface_code_and_docs():
    places = [ROOT / "portal", ROOT / "README.md", ROOT / "website" / "docs", ROOT / "agents.md", ROOT / ".agents"]
    # A misspelled path must fail rather than silently skip the scan (paths are case-sensitive in CI).
    assert [p for p in places if not p.exists()] == []
    pattern = re.compile(r"super[ _-]?admin", re.I)
    offenders = []
    for place in places:
        files = [place] if place.is_file() else [p for p in place.rglob("*") if p.is_file()]
        for path in files:
            if path.suffix not in {".py", ".html", ".js", ".css", ".md", ".mdx", ".txt"} or "__pycache__" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


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


def _patches(booth_role=None, room_role=None, event_role=None, is_admin=False):
    booth = [SimpleNamespace(booth_id=_BOOTH.id, role=booth_role)] if booth_role else []
    room = [SimpleNamespace(room_id=_BOOTH.room_id, role=room_role)] if room_role else []
    event = [SimpleNamespace(event_id=_BOOTH.event_id, role=event_role)] if event_role else []
    return (
        patch("portal.database.get_session", _fake_session),
        patch("portal.database.list_booth_memberships_for_user", AsyncMock(return_value=booth)),
        patch("portal.database.list_room_memberships_for_user", AsyncMock(return_value=room)),
        patch("portal.database.list_memberships_for_user", AsyncMock(return_value=event)),
        patch("portal.auth.user_is_active_admin", AsyncMock(return_value=is_admin)),
    )


async def _resolve(payload=None, **memberships):
    patches = _patches(**memberships)
    for p in patches:
        p.start()
    try:
        return await resolve_booth_role(payload or {"sub": "7", "user": True}, BOOTH_ID)
    finally:
        for p in patches:
            p.stop()


@pytest.mark.anyio
async def test_highest_membership_wins():
    assert await _resolve(booth_role=INTERPRETER, room_role=ROOM_COORDINATOR) == ROOM_COORDINATOR
    assert await _resolve(booth_role=INTERPRETER, event_role=EVENT_OWNER) == EVENT_OWNER


@pytest.mark.anyio
async def test_admin_comes_from_the_account_not_the_token():
    # A token issued while the person was an Admin no longer grants Admin once they are demoted.
    stale = {"sub": "7", "user": True, "is_admin": True}
    assert await _resolve(stale, booth_role=INTERPRETER, is_admin=False) == INTERPRETER
    assert await _resolve(stale, booth_role=INTERPRETER, is_admin=True) == ADMIN


@pytest.mark.anyio
async def test_retired_role_claims_are_ignored():
    # An old invite token claiming "support" grants nothing.
    assert await resolve_booth_role({"role": "support", "event_slug": "pycon2026"}, BOOTH_ID) is None


# ── Migration 032: existing team members keep their roles ────────────────


def test_migration_032_keeps_everyone_and_drops_retired_roles(tmp_path):
    import importlib.util
    import sqlite3

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    spec = importlib.util.spec_from_file_location("m032", ROOT / "alembic" / "versions" / "032_remove_support_role.py")
    m032 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m032)

    db = tmp_path / "v.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE event_memberships (id INTEGER PRIMARY KEY, user_id INT, event_id INT, role TEXT);
        CREATE TABLE room_memberships (id INTEGER PRIMARY KEY, user_id INT, room_id INT, role TEXT);
        CREATE TABLE booth_memberships (id INTEGER PRIMARY KEY, user_id INT, booth_id INT, role TEXT);
        CREATE TABLE invite_tokens (id INTEGER PRIMARY KEY, booth_id INT, role TEXT);
        INSERT INTO event_memberships (user_id, event_id, role) VALUES
            (1, 1, 'event_owner'), (2, 1, 'room_coordinator'), (3, 1, 'interpreter'), (9, 1, 'support'),
            (7, 2, 'owner'), (8, 2, 'super_admin');
        INSERT INTO room_memberships (user_id, room_id, role) VALUES (4, 1, 'room_coordinator');
        INSERT INTO booth_memberships (user_id, booth_id, role) VALUES (5, 1, 'interpreter'), (6, 1, 'room_coordinator');
        INSERT INTO invite_tokens (booth_id, role) VALUES
            (1, 'interpreter'), (1, 'room_coordinator'), (1, 'event_owner'), (1, 'super_admin'), (1, 'support');
        """
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as sa_conn:
        with Operations.context(MigrationContext.configure(sa_conn)):
            m032.upgrade()
        sa_conn.commit()
    engine.dispose()

    conn = sqlite3.connect(db)
    rows = {
        table: sorted(conn.execute(f"SELECT role FROM {table}").fetchall())
        for table in ("event_memberships", "room_memberships", "booth_memberships", "invite_tokens")
    }
    conn.close()
    assert rows == {
        "event_memberships": [("event_owner",), ("event_owner",), ("event_owner",), ("interpreter",), ("room_coordinator",)],
        "room_memberships": [("room_coordinator",)],
        "booth_memberships": [("interpreter",), ("room_coordinator",)],
        "invite_tokens": [("interpreter",), ("room_coordinator",)],
    }
