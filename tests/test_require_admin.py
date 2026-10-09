"""Tests for portal.auth.require_admin and its helpers.

Organised by authorization branch:

1. Pure scope/role helpers (no DB, no request)
2. ``resolve_principal`` -- user_token / admin_token cookie resolution against the DB
3. ``require_admin``     -- workspace delegation, scoped access, and 403 fallback
"""

from __future__ import annotations

import os

os.environ.setdefault("BOOTH_ACCESS_TOKEN", "")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pass")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import HTTPException

from portal.auth import (
    _has_scope_access,
    _is_event_owner,
    _parse_scope_ids,
    create_admin_token,
    create_user_token,
    hash_password,
    require_admin,
    require_event_owner,
    resolve_principal,
)
from portal.config import settings

# Every async test runs under anyio's asyncio backend; sync helper tests are unaffected.
pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _make_request(
    cookies: dict[str, str],
    path_params: dict | None = None,
    *,
    is_workspace: bool = False,
):
    """Build a minimal fake Request with the given cookies and path params."""
    req = MagicMock()
    req.cookies = cookies
    req.path_params = path_params or {}
    req.state = SimpleNamespace(is_workspace=is_workspace)
    return req


def _signed(claims: dict, *, secret: str | None = None, expires_in: int = 60) -> str:
    """Sign an arbitrary claim set with the app's JWT secret (or a custom one)."""
    now = datetime.now(timezone.utc)
    payload = {"iat": now, "exp": now + timedelta(seconds=expires_in), **claims}
    return jwt.encode(payload, secret or settings.effective_jwt_secret, algorithm="HS256")


async def _create_user(email: str = "u@example.com"):
    from portal.database import create_user, get_session

    async with get_session() as s:
        return await create_user(s, email=email, display_name="Test", password_hash=hash_password("password123"))


async def _create_event(slug: str, name: str):
    from portal.database import create_event, get_session

    async with get_session() as s:
        return await create_event(s, slug=slug, display_name=name)


async def _create_room(event_id: int, name: str):
    from portal.database import create_room, get_session

    async with get_session() as s:
        return await create_room(s, event_id=event_id, display_name=name)


async def _grant_event_role(user_id: int, event_id: int, role: str) -> None:
    from portal.database import get_session, set_event_membership

    async with get_session() as s:
        await set_event_membership(s, user_id=user_id, event_id=event_id, role=role)


async def _grant_room_role(user_id: int, room_id: int, role: str) -> None:
    from portal.database import get_session, set_room_membership

    async with get_session() as s:
        await set_room_membership(s, user_id=user_id, room_id=room_id, role=role)


async def _make_db_admin(user_id: int) -> None:
    from portal.database import get_session, get_user_by_id

    async with get_session() as s:
        (await get_user_by_id(s, user_id)).is_admin = True


async def _resolve(cookies: dict, *, error_detail: str | None = None):
    """Run resolve_principal against a fresh sqlite session."""
    from portal.database import get_session

    request = _make_request(cookies)
    async with get_session() as s:
        if error_detail is None:
            return await resolve_principal(request, s)
        return await resolve_principal(request, s, error_detail=error_detail)


class _Membership:
    def __init__(self, event_id: int, role: str):
        self.event_id = event_id
        self.role = role


class _RoomMembership:
    def __init__(self, room_id: int, role: str, event_id: int):
        self.room_id = room_id
        self.role = role
        self.room = MagicMock(event_id=event_id)


# ---------------------------------------------------------------------------
# 1. Pure helpers
# ---------------------------------------------------------------------------


def test_parse_scope_ids_reads_digits_only():
    assert _parse_scope_ids(_make_request({}, {"event_id": "5", "room_id": "12"})) == (5, 12)


def test_parse_scope_ids_ignores_non_digit_or_missing():
    assert _parse_scope_ids(_make_request({}, {"event_id": "not-a-number"})) == (None, None)
    assert _parse_scope_ids(_make_request({})) == (None, None)


def test_is_event_owner_matches_only_owner_role_on_that_event():
    memberships = [_Membership(1, "event_owner"), _Membership(2, "interpreter")]
    assert _is_event_owner(memberships, 1) is True
    assert _is_event_owner(memberships, 2) is False  # wrong role
    assert _is_event_owner(memberships, 3) is False  # wrong event
    assert _is_event_owner(memberships, None) is False  # no event scope


def test_has_scope_access_room_scope_requires_matching_event_when_given():
    rms = [_RoomMembership(room_id=10, role="room_coordinator", event_id=7)]
    owner = [_Membership(8, "event_owner")]

    assert _has_scope_access([], rms, None, 10) is True  # no event scope in URL
    assert _has_scope_access([], rms, 7, 10) is True  # room belongs to the event
    assert _has_scope_access([], rms, 8, 10) is False  # room is in event 7, not 8
    assert _has_scope_access([], rms, None, 99) is False  # unknown room
    assert _has_scope_access(owner, [], 8, 10) is True  # event owner of the URL's event
    assert _has_scope_access(owner, [], 7, 10) is False  # owner of a different event


def test_has_scope_access_event_scope():
    rms = [_RoomMembership(room_id=10, role="room_coordinator", event_id=7)]
    assert _has_scope_access([], rms, 7, None) is True
    assert _has_scope_access([], rms, 8, None) is False
    assert _has_scope_access([_Membership(7, "event_owner")], [], 7, None) is True
    assert _has_scope_access([_Membership(7, "interpreter")], [], 7, None) is False


def test_has_scope_access_no_scope_accepts_any_admin_role_only():
    assert _has_scope_access([_Membership(1, "event_owner")], [], None, None) is True
    assert _has_scope_access([], [_RoomMembership(10, "room_coordinator", 7)], None, None) is True
    assert _has_scope_access([_Membership(1, "interpreter")], [], None, None) is False


# ---------------------------------------------------------------------------
# 2. resolve_principal: cookie resolution against the DB
# ---------------------------------------------------------------------------


async def test_principal_valid_admin_token_passes(setup_db):
    assert await _resolve({"admin_token": create_admin_token()}) == {"user_id": None, "is_global_admin": True}


async def test_principal_missing_admin_token_is_403(setup_db):
    with pytest.raises(HTTPException) as exc:
        await _resolve({})
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin access required."


async def test_principal_missing_admin_token_uses_custom_error_detail(setup_db):
    with pytest.raises(HTTPException) as exc:
        await _resolve({}, error_detail="Super-admin access required.")
    assert exc.value.detail == "Super-admin access required."


@pytest.mark.parametrize(
    "token",
    [
        "not-a-real-jwt",
        _signed({"admin": True}, expires_in=-10),  # expired
        _signed({"admin": True}, secret="some-other-secret-that-is-32-bytes!!"),  # bad signature
    ],
    ids=["garbage", "expired", "wrong-signature"],
)
async def test_principal_invalid_admin_token_is_403(setup_db, token):
    with pytest.raises(HTTPException) as exc:
        await _resolve({"admin_token": token})
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid admin token."


@pytest.mark.parametrize("claims", [{}, {"admin": False}], ids=["no-claim", "false-claim"])
async def test_principal_admin_signature_without_admin_claim_is_403(setup_db, claims):
    with pytest.raises(HTTPException) as exc:
        await _resolve({"admin_token": _signed(claims)})
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin access required."


async def test_principal_user_token_resolves_roles_from_db(setup_db):
    """The DB record decides: JWT is_admin claims are never trusted on their own."""
    admin = await _create_user("admin@example.com")
    plain = await _create_user("plain@example.com")
    await _make_db_admin(admin.id)

    admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)
    plain_token = create_user_token(user_id=plain.id, email=plain.email)

    assert await _resolve({"user_token": admin_token}) == {"user_id": admin.id, "is_global_admin": True}
    assert await _resolve({"user_token": plain_token}) == {"user_id": plain.id, "is_global_admin": False}


async def test_principal_db_admin_revocation_beats_stale_jwt_claim(setup_db):
    """is_admin=True in the JWT but False in the DB must not grant anything."""
    user = await _create_user("lapsed@example.com")
    token = create_user_token(user_id=user.id, email=user.email, is_admin=True)

    principal = await _resolve({"user_token": token})
    assert principal == {"user_id": user.id, "is_global_admin": False}


async def test_principal_user_token_non_numeric_sub_is_403(setup_db):
    token = _signed({"user": True, "sub": "not-a-number"})
    with pytest.raises(HTTPException) as exc:
        await _resolve({"user_token": token})
    assert exc.value.status_code == 403


async def test_principal_user_token_inactive_user_is_403(setup_db):
    from portal.database import get_session, get_user_by_id

    user = await _create_user("inactive@example.com")
    async with get_session() as s:
        (await get_user_by_id(s, user.id)).is_active = False

    token = create_user_token(user_id=user.id, email=user.email)
    with pytest.raises(HTTPException) as exc:
        await _resolve({"user_token": token})
    assert exc.value.status_code == 403


async def test_principal_user_token_without_user_claim_falls_back_to_admin_cookie(setup_db):
    """An admin-style token (no ``user`` claim) in the user_token slot is ignored."""
    token = _signed({"admin": True, "is_admin": True, "sub": "1"})
    with pytest.raises(HTTPException):
        await _resolve({"user_token": token})
    principal = await _resolve({"user_token": token, "admin_token": create_admin_token()})
    assert principal == {"user_id": None, "is_global_admin": True}


async def test_principal_user_token_invalid_jwt_falls_back_to_admin_cookie(setup_db):
    with pytest.raises(HTTPException):
        await _resolve({"user_token": "garbage"})
    principal = await _resolve({"user_token": "garbage", "admin_token": create_admin_token()})
    assert principal == {"user_id": None, "is_global_admin": True}


# ---------------------------------------------------------------------------
# 3. require_admin: workspace delegation, scoped access, 403 fallback
# ---------------------------------------------------------------------------


async def test_require_admin_no_cookies_is_403(setup_db):
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({}))
    assert exc.value.status_code == 403


async def test_require_admin_valid_admin_token_passes(setup_db):
    await require_admin(_make_request({"admin_token": create_admin_token()}))


async def test_require_admin_invalid_admin_token_is_403(setup_db):
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"admin_token": "not-a-real-jwt"}))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid admin token."


async def test_require_admin_db_global_admin_passes_any_scope(setup_db):
    user = await _create_user()
    await _make_db_admin(user.id)
    token = create_user_token(user_id=user.id, email=user.email)

    await require_admin(_make_request({"user_token": token}))
    await require_admin(_make_request({"user_token": token}, {"event_id": "123", "room_id": "456"}))


async def test_require_admin_jwt_admin_claim_without_db_backing_is_403(setup_db):
    """A user_token claiming is_admin must still resolve to a DB admin."""
    user = await _create_user()
    token = create_user_token(user_id=user.id, email=user.email, is_admin=True)

    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"user_token": token}))
    assert exc.value.status_code == 403


async def test_require_admin_role_user_token_passes_in_scope(setup_db):
    event = await _create_event("pycon", "PyCon")
    room = await _create_room(event.id, "Main Hall")
    user = await _create_user()
    await _grant_room_role(user.id, room.id, "room_coordinator")

    token = create_user_token(user_id=user.id, email=user.email)
    scope = {"event_id": str(event.id), "room_id": str(room.id)}
    await require_admin(_make_request({"user_token": token}, scope))


async def test_require_admin_room_coordinator_rejected_for_mismatched_event(setup_db):
    """URL pairs another event's id with this coordinator's room: must be denied."""
    real_event = await _create_event("pycon", "PyCon")
    other_event = await _create_event("djangocon", "DjangoCon")
    room = await _create_room(real_event.id, "Main Hall")
    user = await _create_user()
    await _grant_room_role(user.id, room.id, "room_coordinator")

    token = create_user_token(user_id=user.id, email=user.email)
    scope = {"event_id": str(other_event.id), "room_id": str(room.id)}
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"user_token": token}, scope))
    assert exc.value.status_code == 403


async def test_require_admin_event_owner_rejected_for_other_events_room(setup_db):
    """Owner of event A must not pass for /events/A/rooms/<room-in-B>/... (IDOR)."""
    event_a = await _create_event("a-con", "A")
    event_b = await _create_event("b-con", "B")
    room_b = await _create_room(event_b.id, "B Hall")
    user = await _create_user()
    await _grant_event_role(user.id, event_a.id, "event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    scope = {"event_id": str(event_a.id), "room_id": str(room_b.id)}
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"user_token": token}, scope))
    assert exc.value.status_code == 403


async def test_require_admin_event_owner_allowed_for_own_events_room(setup_db):
    event_a = await _create_event("a-con", "A")
    room_a = await _create_room(event_a.id, "A Hall")
    user = await _create_user()
    await _grant_event_role(user.id, event_a.id, "event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    scope = {"event_id": str(event_a.id), "room_id": str(room_a.id)}
    await require_admin(_make_request({"user_token": token}, scope))


async def test_require_admin_event_owner_rejected_for_nonexistent_room(setup_db):
    event_a = await _create_event("a-con", "A")
    user = await _create_user()
    await _grant_event_role(user.id, event_a.id, "event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    scope = {"event_id": str(event_a.id), "room_id": "99999"}
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"user_token": token}, scope))
    assert exc.value.status_code == 403


async def test_require_admin_ineligible_user_token_is_403(setup_db):
    user = await _create_user()
    token = create_user_token(user_id=user.id, email=user.email)
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"user_token": token}))
    assert exc.value.status_code == 403


async def test_require_admin_malformed_user_token_falls_back_to_admin_cookie(setup_db):
    """A syntactically invalid user_token never 500s; it defers to admin_token."""
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"user_token": "garbage"}))
    assert exc.value.status_code == 403
    await require_admin(_make_request({"user_token": "garbage", "admin_token": create_admin_token()}))


async def test_require_admin_non_numeric_sub_denies_even_with_admin_cookie(setup_db):
    """A well-signed user_token with a malformed sub aborts instead of falling back."""
    bad = _signed({"user": True, "sub": "not-a-number"})
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"user_token": bad, "admin_token": create_admin_token()}))
    assert exc.value.status_code == 403


async def test_require_admin_workspace_delegates_to_event_owner(setup_db):
    """is_workspace requests use the event-owner policy, ignoring admin role scope."""
    event = await _create_event("pycon", "PyCon")
    owner = await _create_user("owner@example.com")
    outsider = await _create_user("outsider@example.com")
    await _grant_event_role(owner.id, event.id, "event_owner")

    owner_token = create_user_token(user_id=owner.id, email=owner.email)
    outsider_token = create_user_token(user_id=outsider.id, email=outsider.email)

    # Unscoped workspace entry point: any event owner may pass.
    await require_admin(_make_request({"user_token": owner_token}, is_workspace=True))
    with pytest.raises(HTTPException):
        await require_admin(_make_request({"user_token": outsider_token}, is_workspace=True))

    # Event-scoped workspace route: only the owner of that event.
    scope = {"event_id": str(event.id)}
    await require_admin(_make_request({"user_token": owner_token}, scope, is_workspace=True))
    with pytest.raises(HTTPException):
        await require_admin(_make_request({"user_token": outsider_token}, scope, is_workspace=True))


async def test_require_event_owner_unscoped_accepts_any_event_owner(setup_db):
    event = await _create_event("pycon", "PyCon")
    user = await _create_user()
    await _grant_event_role(user.id, event.id, "event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    await require_event_owner(_make_request({"user_token": token}))
    with pytest.raises(HTTPException):
        await require_event_owner(_make_request({}))
