"""Tests for portal.auth.require_admin and its helpers.
Organised by authorization branch:

1. Pure scope/role helpers (no DB, no request)
2. ``_check_admin_token``  -- the ``admin_token`` cookie session
3. ``_check_user_token``   -- ``user_token`` claims (admin flag, sub handling)
4. ``_check_scoped_admin_role`` -- DB-backed global admin / event_owner / room_coordinator
5. ``require_admin``       -- dispatch order and fallback between the two cookies
"""

from __future__ import annotations

import os

os.environ.setdefault("BOOTH_ACCESS_TOKEN", "")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pass")

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import HTTPException

from portal.auth import (
    _check_admin_token,
    _check_event_owner,
    _check_room_coordinator,
    _check_scoped_admin_role,
    _check_user_token,
    _parse_scope_ids,
    create_admin_token,
    create_user_token,
    hash_password,
    require_admin,
)
from portal.config import settings

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


def _make_request(cookies: dict[str, str], path_params: dict | None = None):
    """Build a minimal fake Request with the given cookies and path params."""
    req = MagicMock()
    req.cookies = cookies
    req.path_params = path_params or {}
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


def test_check_event_owner_matches_only_owner_role_on_that_event():
    memberships = [_Membership(1, "event_owner"), _Membership(2, "interpreter")]
    assert _check_event_owner(memberships, 1) is True
    assert _check_event_owner(memberships, 2) is False  # wrong role
    assert _check_event_owner(memberships, 3) is False  # wrong event
    assert _check_event_owner(memberships, None) is False  # no event scope


def test_check_room_coordinator_by_room_requires_matching_event_when_given():
    rms = [_RoomMembership(room_id=10, role="room_coordinator", event_id=7)]
    assert _check_room_coordinator(rms, room_id=10) is True
    assert _check_room_coordinator(rms, room_id=10, event_id=7) is True
    assert _check_room_coordinator(rms, room_id=10, event_id=8) is False  # room is in event 7
    assert _check_room_coordinator(rms, room_id=99) is False


def test_check_room_coordinator_by_event_and_unscoped():
    rms = [_RoomMembership(room_id=10, role="room_coordinator", event_id=7)]
    assert _check_room_coordinator(rms, event_id=7) is True
    assert _check_room_coordinator(rms, event_id=8) is False
    assert _check_room_coordinator(rms) is True
    assert _check_room_coordinator([_RoomMembership(10, "interpreter", 7)]) is False


# ---------------------------------------------------------------------------
# 2. admin_token session
# ---------------------------------------------------------------------------


def test_admin_token_valid_passes():
    assert _check_admin_token(_make_request({"admin_token": create_admin_token()})) is None


def test_admin_token_missing_is_403():
    with pytest.raises(HTTPException) as exc:
        _check_admin_token(_make_request({}))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin access required."


@pytest.mark.parametrize(
    "token",
    [
        "not-a-real-jwt",
        _signed({"admin": True}, expires_in=-10),  # expired
        _signed({"admin": True}, secret="some-other-secret-that-is-32-bytes!!"),  # bad signature
    ],
    ids=["garbage", "expired", "wrong-signature"],
)
def test_admin_token_invalid_is_403(token):
    with pytest.raises(HTTPException) as exc:
        _check_admin_token(_make_request({"admin_token": token}))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid admin token."


@pytest.mark.parametrize("claims", [{}, {"admin": False}], ids=["no-claim", "false-claim"])
def test_admin_token_valid_signature_without_admin_claim_is_403(claims):
    with pytest.raises(HTTPException) as exc:
        _check_admin_token(_make_request({"admin_token": _signed(claims)}))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin access required."


# ---------------------------------------------------------------------------
# 3. user_token claims
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_user_token_absent_or_invalid_returns_false():
    assert await _check_user_token(_make_request({}), None, None) is False
    assert await _check_user_token(_make_request({"user_token": "garbage"}), None, None) is False
    expired = _signed({"user": True, "sub": "1", "is_admin": True}, expires_in=-10)
    assert await _check_user_token(_make_request({"user_token": expired}), None, None) is False


@pytest.mark.anyio
async def test_user_token_without_user_claim_returns_false():
    """An admin-style token (no ``user`` claim) must not be accepted in the user_token slot."""
    token = _signed({"admin": True, "is_admin": True, "sub": "1"})
    assert await _check_user_token(_make_request({"user_token": token}), None, None) is False


@pytest.mark.anyio
async def test_user_token_is_admin_claim_short_circuits_without_db():
    token = create_user_token(user_id=1, email="a@test.com", is_admin=True)
    assert await _check_user_token(_make_request({"user_token": token}), 1, 2) is True


@pytest.mark.anyio
async def test_user_token_missing_or_non_numeric_sub_returns_false():
    for claims in ({"user": True}, {"user": True, "sub": "not-a-number"}, {"user": True, "sub": [1]}):
        token = _signed(claims)
        assert await _check_user_token(_make_request({"user_token": token}), None, None) is False


@pytest.mark.anyio
async def test_user_token_delegates_to_db_roles(setup_db):
    event = await _create_event("pycon", "PyCon")
    owner = await _create_user("owner@example.com")
    nobody = await _create_user("nobody@example.com")
    await _grant_event_role(owner.id, event.id, "event_owner")

    def request_for(user):
        return _make_request({"user_token": create_user_token(user_id=user.id, email=user.email)})

    assert await _check_user_token(request_for(owner), event.id, None) is True
    assert await _check_user_token(request_for(nobody), event.id, None) is False


# ---------------------------------------------------------------------------
# 4. DB-backed scoped roles
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_scoped_role_unknown_user_is_false(setup_db):
    assert await _check_scoped_admin_role(9999, None, None) is False


@pytest.mark.anyio
async def test_scoped_role_db_global_admin_passes_any_scope(setup_db):
    """DB is_admin wins even when the JWT's is_admin claim is stale/false."""
    from portal.database import get_session, get_user_by_id

    user = await _create_user()
    async with get_session() as s:
        (await get_user_by_id(s, user.id)).is_admin = True

    assert await _check_scoped_admin_role(user.id, 123, 456) is True


@pytest.mark.anyio
async def test_scoped_role_event_scope(setup_db):
    event = await _create_event("pycon", "PyCon")
    other = await _create_event("djangocon", "DjangoCon")
    room = await _create_room(event.id, "Main Hall")
    owner = await _create_user("owner@example.com")
    coordinator = await _create_user("coord@example.com")
    await _grant_event_role(owner.id, event.id, "event_owner")
    await _grant_room_role(coordinator.id, room.id, "room_coordinator")

    assert await _check_scoped_admin_role(owner.id, event.id, None) is True
    assert await _check_scoped_admin_role(owner.id, other.id, None) is False
    assert await _check_scoped_admin_role(coordinator.id, event.id, None) is True
    assert await _check_scoped_admin_role(coordinator.id, other.id, None) is False


@pytest.mark.anyio
async def test_scoped_role_room_scope(setup_db):
    event = await _create_event("pycon", "PyCon")
    other = await _create_event("djangocon", "DjangoCon")
    room = await _create_room(event.id, "Main Hall")
    other_room = await _create_room(event.id, "Side Room")
    owner = await _create_user("owner@example.com")
    coordinator = await _create_user("coord@example.com")
    await _grant_event_role(owner.id, event.id, "event_owner")
    await _grant_room_role(coordinator.id, room.id, "room_coordinator")

    # coordinator: only their own room, and only via the event that room belongs to
    assert await _check_scoped_admin_role(coordinator.id, event.id, room.id) is True
    assert await _check_scoped_admin_role(coordinator.id, None, room.id) is True
    assert await _check_scoped_admin_role(coordinator.id, event.id, other_room.id) is False
    assert await _check_scoped_admin_role(coordinator.id, other.id, room.id) is False  # mismatched event
    # event_owner falls back to owning the event named in the URL
    assert await _check_scoped_admin_role(owner.id, event.id, room.id) is True
    assert await _check_scoped_admin_role(owner.id, other.id, room.id) is False


@pytest.mark.anyio
async def test_scoped_role_no_scope_accepts_any_admin_role_only(setup_db):
    event = await _create_event("pycon", "PyCon")
    room = await _create_room(event.id, "Main Hall")
    owner = await _create_user("owner@example.com")
    coordinator = await _create_user("coord@example.com")
    interpreter = await _create_user("interp@example.com")
    await _grant_event_role(owner.id, event.id, "event_owner")
    await _grant_room_role(coordinator.id, room.id, "room_coordinator")
    await _grant_event_role(interpreter.id, event.id, "interpreter")

    assert await _check_scoped_admin_role(owner.id, None, None) is True
    assert await _check_scoped_admin_role(coordinator.id, None, None) is True
    assert await _check_scoped_admin_role(interpreter.id, None, None) is False


# ---------------------------------------------------------------------------
# 5. require_admin: dispatch order and fallback
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_require_admin_no_cookies_is_403(setup_db):
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({}))
    assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_require_admin_valid_admin_token_passes(setup_db):
    await require_admin(_make_request({"admin_token": create_admin_token()}))


@pytest.mark.anyio
async def test_require_admin_invalid_admin_token_is_403(setup_db):
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"admin_token": "not-a-real-jwt"}))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid admin token."


@pytest.mark.anyio
async def test_require_admin_user_token_wins_before_admin_cookie_is_checked(setup_db):
    """An authorised user_token short-circuits; a bad admin_token is never consulted."""
    token = create_user_token(user_id=1, email="a@test.com", is_admin=True)
    await require_admin(_make_request({"user_token": token, "admin_token": "garbage"}))


@pytest.mark.anyio
async def test_require_admin_role_user_token_passes_in_scope(setup_db):
    event = await _create_event("pycon", "PyCon")
    room = await _create_room(event.id, "Main Hall")
    user = await _create_user()
    await _grant_room_role(user.id, room.id, "room_coordinator")

    token = create_user_token(user_id=user.id, email=user.email)
    scope = {"event_id": str(event.id), "room_id": str(room.id)}
    await require_admin(_make_request({"user_token": token}, scope))


@pytest.mark.anyio
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


@pytest.mark.anyio
async def test_require_admin_ineligible_user_token_falls_back_to_admin_cookie(setup_db):
    user = await _create_user()
    token = create_user_token(user_id=user.id, email=user.email)
    await require_admin(_make_request({"user_token": token, "admin_token": create_admin_token()}))


@pytest.mark.anyio
async def test_require_admin_ineligible_user_token_without_admin_cookie_is_403(setup_db):
    user = await _create_user()
    token = create_user_token(user_id=user.id, email=user.email)
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"user_token": token}))
    assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_require_admin_malformed_user_tokens_fall_back_cleanly(setup_db):
    """Garbage or non-numeric-sub user tokens never 500; they defer to admin_token."""
    bad_tokens = ["garbage", _signed({"user": True, "sub": "not-a-number"})]
    for bad in bad_tokens:
        with pytest.raises(HTTPException) as exc:
            await require_admin(_make_request({"user_token": bad}))
        assert exc.value.status_code == 403
        await require_admin(_make_request({"user_token": bad, "admin_token": create_admin_token()}))


@pytest.mark.anyio
async def test_require_admin_event_owner_rejected_for_other_events_room(setup_db):
    """Owner of event A must not pass for /events/A/rooms/<room-in-B>/..."""
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


@pytest.mark.anyio
async def test_require_admin_event_owner_allowed_for_own_events_room(setup_db):
    event_a = await _create_event("a-con", "A")
    room_a = await _create_room(event_a.id, "A Hall")
    user = await _create_user()
    await _grant_event_role(user.id, event_a.id, "event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    scope = {"event_id": str(event_a.id), "room_id": str(room_a.id)}
    await require_admin(_make_request({"user_token": token}, scope))


@pytest.mark.anyio
async def test_require_admin_event_owner_rejected_for_nonexistent_room(setup_db):
    event_a = await _create_event("a-con", "A")
    user = await _create_user()
    await _grant_event_role(user.id, event_a.id, "event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    scope = {"event_id": str(event_a.id), "room_id": "99999"}
    with pytest.raises(HTTPException) as exc:
        await require_admin(_make_request({"user_token": token}, scope))
    assert exc.value.status_code == 403
