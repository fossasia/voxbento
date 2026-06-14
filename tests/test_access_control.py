"""Tests for portal.auth.get_accessible_event_ids helper."""

from __future__ import annotations

import os

os.environ.setdefault('BOOTH_ACCESS_TOKEN', '')
os.environ.setdefault('ADMIN_PASSWORD', 'test-admin-pass')

import pytest

from portal.auth import create_admin_token, create_user_token, hash_password

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure('sqlite+aiosqlite://')
    await init_db()
    yield
    await dispose()


def _make_request(cookies: dict[str, str]):
    """Build a minimal fake Request with the given cookies."""
    from unittest.mock import MagicMock

    req = MagicMock()
    req.cookies = cookies
    return req


async def _create_user(email: str = 'u@example.com') -> object:
    from portal.database import create_user, get_session

    pw_hash = hash_password('password123')
    async with get_session() as s:
        return await create_user(s, email=email, display_name='Test', password_hash=pw_hash)


async def _create_event(slug: str, name: str) -> object:
    from portal.database import create_event, get_session

    async with get_session() as s:
        return await create_event(s, slug=slug, display_name=name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_accessible_event_ids_super_admin_sees_all(setup_db):
    """admin_token with admin=True → is_super_admin=True and allowed_event_ids=None."""
    from portal.auth import get_accessible_event_ids

    await _create_event('ev1', 'Event 1')
    await _create_event('ev2', 'Event 2')
    await _create_event('ev3', 'Event 3')

    req = _make_request({'admin_token': create_admin_token()})
    is_super_admin, allowed_event_ids = await get_accessible_event_ids(req, user_id=1)

    assert is_super_admin is True
    assert allowed_event_ids is None


@pytest.mark.anyio
async def test_get_accessible_event_ids_user_is_admin_flag(setup_db):
    """user_token with is_admin=True → is_super_admin=True."""
    from portal.auth import get_accessible_event_ids

    await _create_event('ev1', 'Event 1')
    token = create_user_token(user_id=99, email='admin@test.com', is_admin=True)
    req = _make_request({'user_token': token})
    is_super_admin, allowed_event_ids = await get_accessible_event_ids(req, user_id=99)

    assert is_super_admin is True
    assert allowed_event_ids is None


@pytest.mark.anyio
async def test_get_accessible_event_ids_event_owner_filtered(setup_db):
    """Non-admin user who owns event 1 only sees event 1."""
    from portal.auth import get_accessible_event_ids
    from portal.database import get_session, set_event_membership

    ev1 = await _create_event('pycon', 'PyCon')
    await _create_event('djangocon', 'DjangoCon')
    user = await _create_user()

    async with get_session() as s:
        await set_event_membership(s, user_id=user.id, event_id=ev1.id, role='event_owner')

    req = _make_request({})
    is_super_admin, allowed_event_ids = await get_accessible_event_ids(req, user_id=user.id)

    assert is_super_admin is False
    assert allowed_event_ids == {ev1.id}


@pytest.mark.anyio
async def test_get_accessible_event_ids_no_membership_returns_empty(setup_db):
    """User with no memberships gets an empty set."""
    from portal.auth import get_accessible_event_ids

    await _create_event('ev1', 'Event 1')
    await _create_event('ev2', 'Event 2')
    user = await _create_user()

    req = _make_request({})
    is_super_admin, allowed_event_ids = await get_accessible_event_ids(req, user_id=user.id)

    assert is_super_admin is False
    assert allowed_event_ids == set()


@pytest.mark.anyio
async def test_get_accessible_event_ids_user_id_none_returns_all_as_non_admin(setup_db):
    """user_id=None with no admin cookies → non-admin, unrestricted (None)."""
    from portal.auth import get_accessible_event_ids

    await _create_event('ev1', 'Event 1')
    await _create_event('ev2', 'Event 2')

    req = _make_request({})
    is_super_admin, allowed_event_ids = await get_accessible_event_ids(req, user_id=None)

    assert is_super_admin is False
    # None means "no filter applied" — consistent with anonymous/unauthenticated callers
    assert allowed_event_ids is None
