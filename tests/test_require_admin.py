"""Tests for the refactored portal.auth.require_admin and its helpers."""

from __future__ import annotations

import os

os.environ.setdefault("BOOTH_ACCESS_TOKEN", "")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pass")

import pytest
from fastapi import HTTPException

from portal.auth import (
    _check_event_owner,
    _check_room_coordinator,
    _has_any_admin_role,
    _parse_scope_ids,
    create_admin_token,
    create_user_token,
    hash_password,
    require_admin,
)


# helpers


@pytest.fixture
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _make_request(cookies: dict[str, str], path_params: dict | None = None):
    """Build a minimal fake Request with the given cookies and path params."""
    from unittest.mock import MagicMock

    req = MagicMock()
    req.cookies = cookies
    req.path_params = path_params or {}
    return req


async def _create_user(email: str = "u@example.com") -> object:
    from portal.database import create_user, get_session

    pw_hash = hash_password("password123")
    async with get_session() as s:
        return await create_user(s, email=email, display_name="Test", password_hash=pw_hash)


async def _create_event(slug: str, name: str) -> object:
    from portal.database import create_event, get_session

    async with get_session() as s:
        return await create_event(s, slug=slug, display_name=name)


async def _create_room(event_id: int, name: str) -> object:
    from portal.database import create_room, get_session

    async with get_session() as s:
        return await create_room(s, event_id=event_id, display_name=name)


# Pure helper unit tests no DB, no request, no async needed


class _FakeMembership:
    def __init__(self, event_id: int, role: str):
        self.event_id = event_id
        self.role = role


class _FakeRoom:
    def __init__(self, event_id: int):
        self.event_id = event_id


class _FakeRoomMembership:
    def __init__(self, room_id: int, role: str, event_id: int | None = None):
        self.room_id = room_id
        self.role = role
        self.room = _FakeRoom(event_id) if event_id is not None else None


def test_parse_scope_ids_reads_digits_only():
    req = _make_request({}, {"event_id": "5", "room_id": "12"})
    assert _parse_scope_ids(req) == (5, 12)


def test_parse_scope_ids_ignores_non_digit_or_missing():
    req = _make_request({}, {"event_id": "not-a-number"})
    assert _parse_scope_ids(req) == (None, None)


def test_check_event_owner_true_for_matching_membership():
    memberships = [_FakeMembership(1, "event_owner"), _FakeMembership(2, "interpreter")]
    assert _check_event_owner(memberships, event_id=1) is True


def test_check_event_owner_false_when_event_id_none():
    memberships = [_FakeMembership(1, "event_owner")]
    assert _check_event_owner(memberships, event_id=None) is False


def test_check_room_coordinator_by_room_id():
    rms = [_FakeRoomMembership(room_id=10, role="room_coordinator")]
    assert _check_room_coordinator(rms, room_id=10) is True
    assert _check_room_coordinator(rms, room_id=99) is False


def test_check_room_coordinator_by_event_id():
    rms = [_FakeRoomMembership(room_id=10, role="room_coordinator", event_id=7)]
    assert _check_room_coordinator(rms, event_id=7) is True
    assert _check_room_coordinator(rms, event_id=8) is False


def test_check_room_coordinator_no_scope_checks_any():
    rms = [_FakeRoomMembership(room_id=10, role="room_coordinator")]
    assert _check_room_coordinator(rms) is True


def test_has_any_admin_role():
    assert _has_any_admin_role([_FakeMembership(1, "event_owner")], []) is True
    assert _has_any_admin_role([], [_FakeRoomMembership(1, "room_coordinator")]) is True
    assert _has_any_admin_role([_FakeMembership(1, "interpreter")], []) is False



# require_admin end-to-end behavior, matching the original semantics


@pytest.mark.anyio
async def test_require_admin_no_cookies_raises_403(setup_db):
    req = _make_request({})
    with pytest.raises(HTTPException) as exc:
        await require_admin(req)
    assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_require_admin_valid_admin_token_passes(setup_db):
    req = _make_request({"admin_token": create_admin_token()})
    await require_admin(req)  # should not raise


@pytest.mark.anyio
async def test_require_admin_invalid_admin_token_raises_403(setup_db):
    req = _make_request({"admin_token": "not-a-real-jwt"})
    with pytest.raises(HTTPException) as exc:
        await require_admin(req)
    assert exc.value.status_code == 403
    assert "Invalid admin token" in exc.value.detail


@pytest.mark.anyio
async def test_require_admin_user_token_is_admin_flag_passes(setup_db):
    token = create_user_token(user_id=1, email="a@test.com", is_admin=True)
    req = _make_request({"user_token": token})
    await require_admin(req)


@pytest.mark.anyio
async def test_require_admin_db_global_admin_passes(setup_db):
    """A user whose DB record has is_admin=True qualifies even if the JWT's
    is_admin claim is stale/false — this is the get_user_by_id(...).is_admin
    branch inside _check_scoped_admin_role."""
    from portal.database import get_session, get_user_by_id

    user = await _create_user()
    async with get_session() as s:
        db_user = await get_user_by_id(s, user.id)
        db_user.is_admin = True

    token = create_user_token(user_id=user.id, email=user.email, is_admin=False)
    req = _make_request({"user_token": token})
    await require_admin(req)


@pytest.mark.anyio
async def test_require_admin_event_owner_in_scope_passes(setup_db):
    from portal.database import get_session, set_event_membership

    event = await _create_event("pycon", "PyCon")
    user = await _create_user()
    async with get_session() as s:
        await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    req = _make_request({"user_token": token}, {"event_id": str(event.id)})
    await require_admin(req)


@pytest.mark.anyio
async def test_require_admin_event_owner_wrong_event_falls_through_and_raises(setup_db):
    from portal.database import get_session, set_event_membership

    owned_event = await _create_event("pycon", "PyCon")
    other_event = await _create_event("djangocon", "DjangoCon")
    user = await _create_user()
    async with get_session() as s:
        await set_event_membership(s, user_id=user.id, event_id=owned_event.id, role="event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    req = _make_request({"user_token": token}, {"event_id": str(other_event.id)})
    with pytest.raises(HTTPException) as exc:
        await require_admin(req)
    assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_require_admin_room_coordinator_in_scope_passes(setup_db):
    from portal.database import get_session, set_room_membership

    event = await _create_event("pycon", "PyCon")
    room = await _create_room(event.id, "Main Hall")
    user = await _create_user()
    async with get_session() as s:
        await set_room_membership(s, user_id=user.id, room_id=room.id, role="room_coordinator")

    token = create_user_token(user_id=user.id, email=user.email)
    req = _make_request({"user_token": token}, {"room_id": str(room.id)})
    await require_admin(req)


@pytest.mark.anyio
async def test_require_admin_room_coordinator_falls_back_to_event_owner(setup_db):
    """When room_id is in scope, an event_owner of the room's event also qualifies."""
    from portal.database import get_session, set_event_membership

    event = await _create_event("pycon", "PyCon")
    room = await _create_room(event.id, "Main Hall")
    user = await _create_user()
    async with get_session() as s:
        await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    req = _make_request({"user_token": token}, {"event_id": str(event.id), "room_id": str(room.id)})
    await require_admin(req)


@pytest.mark.anyio
async def test_require_admin_no_scope_uses_any_role(setup_db):
    from portal.database import get_session, set_event_membership

    event = await _create_event("pycon", "PyCon")
    user = await _create_user()
    async with get_session() as s:
        await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")

    token = create_user_token(user_id=user.id, email=user.email)
    req = _make_request({"user_token": token})  # no event_id/room_id path params
    await require_admin(req)


@pytest.mark.anyio
async def test_require_admin_malformed_user_token_falls_back_to_admin_cookie(setup_db):
    req = _make_request({"user_token": "garbage", "admin_token": create_admin_token()})
    await require_admin(req)  # user_token is ignored, admin_token still validates


@pytest.mark.anyio
async def test_require_admin_no_matching_role_raises_403(setup_db):
    user = await _create_user()
    token = create_user_token(user_id=user.id, email=user.email)
    req = _make_request({"user_token": token})
    with pytest.raises(HTTPException) as exc:
        await require_admin(req)
    assert exc.value.status_code == 403
