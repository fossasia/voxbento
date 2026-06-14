"""Tests for portal.models and portal.database — CRUD operations.

Uses a fresh in-memory SQLite database per test function via the ``db``
fixture.  Mirrors the ``@pytest.mark.anyio`` async convention from the
existing test suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# We import CRUD helpers and test them against our isolated test DB.
# Because portal.database uses a module-level engine tied to settings,
# we import the functions and call them with our test session.
from portal.database import (
    create_booth,
    create_event,
    create_invite_token,
    create_room,
    create_user,
    delete_booth,
    delete_event,
    delete_room,
    get_booth_by_id,
    get_event_by_id,
    get_event_by_slug,
    get_invite_token,
    get_room_by_id,
    list_all_booths_for_events,
    list_booths_for_event,
    list_booths_for_room,
    list_events,
    list_rooms_for_event,
    list_tokens_for_booth,
    list_users,
    redeem_invite_token,
)
from portal.models import Base, DBBooth, Event, InviteToken, Room, generate_token, utc_now
from portal.roles import ALL_ROLES

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """Yield an async session backed by an in-memory SQLite database."""
    engine = create_async_engine('sqlite+aiosqlite://', echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Model unit tests
# ---------------------------------------------------------------------------


class TestUtcNow:
    def test_returns_aware_datetime(self):
        now = utc_now()
        assert now.tzinfo is not None
        assert now.tzinfo == timezone.utc

    def test_is_recent(self):
        now = utc_now()
        assert abs((datetime.now(tz=timezone.utc) - now).total_seconds()) < 2


class TestGenerateToken:
    def test_length(self):
        token = generate_token()
        assert len(token) == 64

    def test_hex_chars(self):
        token = generate_token()
        assert all(c in '0123456789abcdef' for c in token)

    def test_uniqueness(self):
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100


# ---------------------------------------------------------------------------
# Event CRUD
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_event(db: AsyncSession):
    ev = await create_event(db, slug='pycon2026', display_name='PyCon 2026')
    assert ev.id is not None
    assert ev.slug == 'pycon2026'
    assert ev.display_name == 'PyCon 2026'
    assert ev.created_at is not None


@pytest.mark.anyio
async def test_create_event_slug_validation(db: AsyncSession):
    with pytest.raises(ValueError):
        await create_event(db, slug='', display_name='Bad')


@pytest.mark.anyio
async def test_create_event_slug_normalised(db: AsyncSession):
    ev = await create_event(db, slug='PyCon2026', display_name='PyCon 2026')
    assert ev.slug == 'pycon2026'


@pytest.mark.anyio
async def test_get_event_by_slug(db: AsyncSession):
    await create_event(db, slug='fossasia2026', display_name='FOSSASIA 2026')
    found = await get_event_by_slug(db, 'fossasia2026')
    assert found is not None
    assert found.slug == 'fossasia2026'


@pytest.mark.anyio
async def test_get_event_by_slug_not_found(db: AsyncSession):
    result = await get_event_by_slug(db, 'nonexistent')
    assert result is None


@pytest.mark.anyio
async def test_get_event_by_id(db: AsyncSession):
    ev = await create_event(db, slug='test-event', display_name='Test')
    found = await get_event_by_id(db, ev.id)
    assert found is not None
    assert found.slug == 'test-event'


@pytest.mark.anyio
async def test_list_events(db: AsyncSession):
    await create_event(db, slug='ev-a', display_name='A')
    await create_event(db, slug='ev-b', display_name='B')
    events = await list_events(db)
    assert len(events) == 2


@pytest.mark.anyio
async def test_delete_event(db: AsyncSession):
    ev = await create_event(db, slug='to-delete', display_name='Delete Me')
    assert await delete_event(db, ev.id) is True
    assert await get_event_by_id(db, ev.id) is None


@pytest.mark.anyio
async def test_delete_event_not_found(db: AsyncSession):
    assert await delete_event(db, 99999) is False


# ---------------------------------------------------------------------------
# Room CRUD
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_room(db: AsyncSession):
    ev = await create_event(db, slug='ev-room', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Main Hall')
    assert room.id is not None
    assert room.event_id == ev.id
    assert room.display_name == 'Main Hall'
    assert room.eventyay_room_id is None


@pytest.mark.anyio
async def test_create_room_with_eventyay_id(db: AsyncSession):
    ev = await create_event(db, slug='ev-room2', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall', eventyay_room_id='room-123')
    assert room.eventyay_room_id == 'room-123'


@pytest.mark.anyio
async def test_get_room_by_id(db: AsyncSession):
    ev = await create_event(db, slug='ev-room3', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    found = await get_room_by_id(db, room.id)
    assert found is not None
    assert found.display_name == 'Hall'


@pytest.mark.anyio
async def test_list_rooms_for_event(db: AsyncSession):
    ev = await create_event(db, slug='ev-rooms', display_name='Ev')
    await create_room(db, event_id=ev.id, display_name='Room 1')
    await create_room(db, event_id=ev.id, display_name='Room 2')
    rooms = await list_rooms_for_event(db, ev.id)
    assert len(rooms) == 2


@pytest.mark.anyio
async def test_delete_room(db: AsyncSession):
    ev = await create_event(db, slug='ev-room-del', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Room')
    assert await delete_room(db, room.id) is True
    assert await get_room_by_id(db, room.id) is None


@pytest.mark.anyio
async def test_delete_room_not_found(db: AsyncSession):
    assert await delete_room(db, 99999) is False


# ---------------------------------------------------------------------------
# DBBooth CRUD
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_booth(db: AsyncSession):
    ev = await create_event(db, slug='ev-booth', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='fr', language_name='French',
    )
    assert booth.id is not None
    assert booth.language_code == 'fr'
    assert booth.language_name == 'French'


@pytest.mark.anyio
async def test_booth_language_code_validation(db: AsyncSession):
    ev = await create_event(db, slug='ev-booth-val', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    with pytest.raises(ValueError):
        await create_booth(
            db, event_id=ev.id, room_id=room.id,
            language_code='xyz', language_name='Bad',
        )


@pytest.mark.anyio
async def test_booth_mediamtx_path_derived(db: AsyncSession):
    ev = await create_event(db, slug='pycon2026', display_name='PyCon 2026')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    # Reload with event joinedloaded
    loaded = await get_booth_by_id(db, booth.id)
    assert loaded is not None
    assert loaded.mediamtx_path == 'pycon2026/en'


@pytest.mark.anyio
async def test_booth_mediamtx_path_not_column(db: AsyncSession):
    """mediamtx_path is a property, NOT a stored column."""
    columns = {c.name for c in DBBooth.__table__.columns}
    assert 'mediamtx_path' not in columns


@pytest.mark.anyio
async def test_no_hls_url_column(db: AsyncSession):
    """No hls_url column in any table — WHEP is the only playback protocol."""
    for model in [Event, Room, DBBooth, InviteToken]:
        columns = {c.name for c in model.__table__.columns}
        assert 'hls_url' not in columns


@pytest.mark.anyio
async def test_get_booth_by_id(db: AsyncSession):
    ev = await create_event(db, slug='ev-booth-get', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='de', language_name='German',
    )
    found = await get_booth_by_id(db, booth.id)
    assert found is not None
    assert found.language_code == 'de'


@pytest.mark.anyio
async def test_list_booths_for_event(db: AsyncSession):
    ev = await create_event(db, slug='ev-booths', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    await create_booth(db, event_id=ev.id, room_id=room.id, language_code='en', language_name='English')
    await create_booth(db, event_id=ev.id, room_id=room.id, language_code='fr', language_name='French')
    booths = await list_booths_for_event(db, ev.id)
    assert len(booths) == 2
    assert booths[0].language_code == 'en'  # ordered by language_code


@pytest.mark.anyio
async def test_list_all_booths_for_events_empty_input(db: AsyncSession):
    result = await list_all_booths_for_events(db, [])
    assert result == {}


@pytest.mark.anyio
async def test_list_all_booths_for_events_single_event(db: AsyncSession):
    ev = await create_event(db, slug='ev-all-single', display_name='Ev Single')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    await create_booth(db, event_id=ev.id, room_id=room.id, language_code='en', language_name='English')
    await create_booth(db, event_id=ev.id, room_id=room.id, language_code='fr', language_name='French')
    result = await list_all_booths_for_events(db, [ev.id])
    assert list(result.keys()) == [ev.id]
    assert len(result[ev.id]) == 2
    assert result[ev.id][0].language_code == 'en'
    assert result[ev.id][1].language_code == 'fr'


@pytest.mark.anyio
async def test_list_all_booths_for_events_multiple_events(db: AsyncSession):
    ev1 = await create_event(db, slug='ev-all-multi1', display_name='Ev 1')
    ev2 = await create_event(db, slug='ev-all-multi2', display_name='Ev 2')
    room1 = await create_room(db, event_id=ev1.id, display_name='Hall 1')
    room2 = await create_room(db, event_id=ev2.id, display_name='Hall 2')
    await create_booth(db, event_id=ev1.id, room_id=room1.id, language_code='en', language_name='English')
    await create_booth(db, event_id=ev1.id, room_id=room1.id, language_code='fr', language_name='French')
    await create_booth(db, event_id=ev2.id, room_id=room2.id, language_code='de', language_name='German')
    await create_booth(db, event_id=ev2.id, room_id=room2.id, language_code='ja', language_name='Japanese')
    result = await list_all_booths_for_events(db, [ev1.id, ev2.id])
    assert len(result[ev1.id]) == 2
    assert len(result[ev2.id]) == 2
    assert result[ev1.id][0].language_code == 'en'
    assert result[ev2.id][0].language_code == 'de'


@pytest.mark.anyio
async def test_list_all_booths_for_events_event_with_no_booths(db: AsyncSession):
    ev1 = await create_event(db, slug='ev-all-nobooth1', display_name='Ev With Booths')
    ev2 = await create_event(db, slug='ev-all-nobooth2', display_name='Ev No Booths')
    room1 = await create_room(db, event_id=ev1.id, display_name='Hall')
    await create_booth(db, event_id=ev1.id, room_id=room1.id, language_code='en', language_name='English')
    await create_booth(db, event_id=ev1.id, room_id=room1.id, language_code='fr', language_name='French')
    result = await list_all_booths_for_events(db, [ev1.id, ev2.id])
    assert len(result[ev1.id]) == 2
    assert result[ev2.id] == []


@pytest.mark.anyio
async def test_list_booths_for_room(db: AsyncSession):
    ev = await create_event(db, slug='ev-booths-room', display_name='Ev')
    room1 = await create_room(db, event_id=ev.id, display_name='Room 1')
    room2 = await create_room(db, event_id=ev.id, display_name='Room 2')
    await create_booth(db, event_id=ev.id, room_id=room1.id, language_code='en', language_name='English')
    await create_booth(db, event_id=ev.id, room_id=room2.id, language_code='fr', language_name='French')
    booths = await list_booths_for_room(db, room1.id)
    assert len(booths) == 1
    assert booths[0].language_code == 'en'


@pytest.mark.anyio
async def test_delete_booth(db: AsyncSession):
    ev = await create_event(db, slug='ev-booth-del', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='es', language_name='Spanish',
    )
    assert await delete_booth(db, booth.id) is True
    assert await get_booth_by_id(db, booth.id) is None


@pytest.mark.anyio
async def test_delete_booth_not_found(db: AsyncSession):
    assert await delete_booth(db, 99999) is False


# ---------------------------------------------------------------------------
# InviteToken CRUD
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_invite_token(db: AsyncSession):
    ev = await create_event(db, slug='ev-token', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    tok = await create_invite_token(
        db, booth_id=booth.id, role='interpreter',
        label='Alice Invite', created_by='admin@test.com',
    )
    assert len(tok.token) == 64
    assert tok.role == 'interpreter'
    assert tok.label == 'Alice Invite'
    assert tok.created_by == 'admin@test.com'
    assert tok.used_at is None
    assert tok.expires_at is None


@pytest.mark.anyio
async def test_invite_token_role_validation(db: AsyncSession):
    ev = await create_event(db, slug='ev-token-val', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    with pytest.raises(ValueError, match='Invalid role'):
        await create_invite_token(db, booth_id=booth.id, role='hacker')


@pytest.mark.anyio
@pytest.mark.parametrize('role', sorted(ALL_ROLES))
async def test_invite_token_all_valid_roles(db: AsyncSession, role: str):
    slug = f'ev-role-{role.replace("_", "-")}'
    ev = await create_event(db, slug=slug, display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    tok = await create_invite_token(db, booth_id=booth.id, role=role)
    assert tok.role == role


@pytest.mark.anyio
async def test_get_invite_token(db: AsyncSession):
    ev = await create_event(db, slug='ev-token-get', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    tok = await create_invite_token(db, booth_id=booth.id, role='interpreter')
    found = await get_invite_token(db, tok.token)
    assert found is not None
    assert found.token == tok.token
    # Verify joinedload brings in booth and event
    assert found.booth is not None
    assert found.booth.event is not None
    assert found.booth.event.slug == 'ev-token-get'


@pytest.mark.anyio
async def test_get_invite_token_not_found(db: AsyncSession):
    result = await get_invite_token(db, 'a' * 64)
    assert result is None


@pytest.mark.anyio
async def test_redeem_invite_token(db: AsyncSession):
    ev = await create_event(db, slug='ev-redeem', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    tok = await create_invite_token(db, booth_id=booth.id, role='interpreter')
    assert tok.is_used is False

    redeemed = await redeem_invite_token(db, tok.token)
    assert redeemed is not None
    assert redeemed.is_used is True
    assert redeemed.used_at is not None


@pytest.mark.anyio
async def test_redeem_invite_token_already_used(db: AsyncSession):
    ev = await create_event(db, slug='ev-redeem2', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    tok = await create_invite_token(db, booth_id=booth.id, role='interpreter')
    await redeem_invite_token(db, tok.token)
    with pytest.raises(ValueError, match='already been used'):
        await redeem_invite_token(db, tok.token)


@pytest.mark.anyio
async def test_redeem_invite_token_expired(db: AsyncSession):
    ev = await create_event(db, slug='ev-redeem3', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    tok = await create_invite_token(
        db, booth_id=booth.id, role='interpreter', expires_at=past,
    )
    assert tok.is_expired is True
    with pytest.raises(ValueError, match='expired'):
        await redeem_invite_token(db, tok.token)


@pytest.mark.anyio
async def test_redeem_invite_token_not_found(db: AsyncSession):
    result = await redeem_invite_token(db, 'b' * 64)
    assert result is None


@pytest.mark.anyio
async def test_invite_token_not_expired_when_no_expiry(db: AsyncSession):
    ev = await create_event(db, slug='ev-noexp', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    tok = await create_invite_token(db, booth_id=booth.id, role='interpreter')
    assert tok.is_expired is False


@pytest.mark.anyio
async def test_invite_token_not_expired_when_future(db: AsyncSession):
    ev = await create_event(db, slug='ev-futureexp', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    future = datetime.now(tz=timezone.utc) + timedelta(hours=24)
    tok = await create_invite_token(
        db, booth_id=booth.id, role='interpreter', expires_at=future,
    )
    assert tok.is_expired is False


@pytest.mark.anyio
async def test_list_tokens_for_booth(db: AsyncSession):
    ev = await create_event(db, slug='ev-list-tok', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    await create_invite_token(db, booth_id=booth.id, role='interpreter')
    await create_invite_token(db, booth_id=booth.id, role='interpreter')
    tokens = await list_tokens_for_booth(db, booth.id)
    assert len(tokens) == 2


# ---------------------------------------------------------------------------
# Cascade deletes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cascade_delete_event_removes_rooms_and_booths(db: AsyncSession):
    ev = await create_event(db, slug='ev-cascade', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    await create_invite_token(db, booth_id=booth.id, role='interpreter')

    await delete_event(db, ev.id)
    assert await get_room_by_id(db, room.id) is None
    assert await get_booth_by_id(db, booth.id) is None


@pytest.mark.anyio
async def test_cascade_delete_room_removes_booths(db: AsyncSession):
    ev = await create_event(db, slug='ev-cascade2', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='fr', language_name='French',
    )
    await delete_room(db, room.id)
    assert await get_booth_by_id(db, booth.id) is None


# ---------------------------------------------------------------------------
# Model __repr__
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_event_repr(db: AsyncSession):
    ev = await create_event(db, slug='repr-test', display_name='Test')
    assert 'repr-test' in repr(ev)


@pytest.mark.anyio
async def test_room_repr(db: AsyncSession):
    ev = await create_event(db, slug='repr-room', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Main Hall')
    assert 'Main Hall' in repr(room)


@pytest.mark.anyio
async def test_booth_repr(db: AsyncSession):
    ev = await create_event(db, slug='repr-booth', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    assert 'en' in repr(booth)


@pytest.mark.anyio
async def test_invite_token_repr(db: AsyncSession):
    ev = await create_event(db, slug='repr-tok', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Hall')
    booth = await create_booth(
        db, event_id=ev.id, room_id=room.id,
        language_code='en', language_name='English',
    )
    tok = await create_invite_token(db, booth_id=booth.id, role='interpreter')
    r = repr(tok)
    assert 'interpreter' in r
    assert tok.token[:8] in r


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_events_limit(db: AsyncSession):
    for i in range(5):
        await create_event(db, slug=f'ev-lim-{i}', display_name=f'Ev {i}')
    events = await list_events(db, limit=2)
    assert len(events) == 2


@pytest.mark.anyio
async def test_list_events_offset(db: AsyncSession):
    for i in range(5):
        await create_event(db, slug=f'ev-off-{i}', display_name=f'Ev {i}')
    events = await list_events(db, limit=2, offset=3)
    assert len(events) == 2
    assert events[0].slug == 'ev-off-3'  # 4th created event


@pytest.mark.anyio
async def test_list_rooms_pagination(db: AsyncSession):
    ev = await create_event(db, slug='ev-rm-pag', display_name='Ev')
    for i in range(5):
        await create_room(db, event_id=ev.id, display_name=f'Room {i}')
    rooms = await list_rooms_for_event(db, ev.id, limit=3)
    assert len(rooms) == 3


@pytest.mark.anyio
async def test_list_booths_pagination(db: AsyncSession):
    ev = await create_event(db, slug='ev-bth-pag', display_name='Ev')
    room = await create_room(db, event_id=ev.id, display_name='Room')
    langs = ['en', 'fr', 'de', 'es', 'it']
    for i, lang in enumerate(langs):
        await create_booth(db, event_id=ev.id, room_id=room.id, language_code=lang, language_name=f'Lang {i}')
    booths = await list_booths_for_event(db, ev.id, limit=2)
    assert len(booths) == 2


@pytest.mark.anyio
async def test_list_users_pagination(db: AsyncSession):
    for i in range(5):
        await create_user(db, email=f'u{i}@test.com', display_name=f'U{i}', password_hash='x')
    users = await list_users(db, limit=3, offset=1)
    assert len(users) == 3
    assert users[0].email == 'u1@test.com'


@pytest.mark.anyio
async def test_list_events_default_limit_does_not_break(db: AsyncSession):
    for i in range(3):
        await create_event(db, slug=f'ev-def-{i}', display_name=f'Ev {i}')
    events = await list_events(db)
    assert len(events) == 3

