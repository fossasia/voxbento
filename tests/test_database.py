"""Tests for portal.models and portal.database — CRUD operations.

Uses a fresh in-memory SQLite database per test function via the ``db``
fixture.  Mirrors the ``@pytest.mark.anyio`` async convention from the
existing test suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
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
from portal.models import (
    Base,
    DBBooth,
    Event,
    InviteToken,
    Room,
    TranscriptSegment,
    TranscriptTranslation,
    generate_token,
    utc_now,
)
from portal.roles import ALL_ROLES

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """Yield an async session backed by an in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
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
        assert all(c in "0123456789abcdef" for c in token)

    def test_uniqueness(self):
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100


# ---------------------------------------------------------------------------
# Event CRUD
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_event(db: AsyncSession):
    ev = await create_event(db, slug="pycon2026", display_name="PyCon 2026")
    assert ev.id is not None
    assert ev.slug == "pycon2026"
    assert ev.display_name == "PyCon 2026"
    assert ev.created_at is not None


@pytest.mark.anyio
async def test_create_event_slug_validation(db: AsyncSession):
    with pytest.raises(ValueError):
        await create_event(db, slug="", display_name="Bad")


@pytest.mark.anyio
async def test_create_event_slug_normalised(db: AsyncSession):
    ev = await create_event(db, slug="PyCon2026", display_name="PyCon 2026")
    assert ev.slug == "pycon2026"


@pytest.mark.anyio
async def test_get_event_by_slug(db: AsyncSession):
    await create_event(db, slug="fossasia2026", display_name="FOSSASIA 2026")
    found = await get_event_by_slug(db, "fossasia2026")
    assert found is not None
    assert found.slug == "fossasia2026"


@pytest.mark.anyio
async def test_get_event_by_slug_not_found(db: AsyncSession):
    result = await get_event_by_slug(db, "nonexistent")
    assert result is None


@pytest.mark.anyio
async def test_get_event_by_id(db: AsyncSession):
    ev = await create_event(db, slug="test-event", display_name="Test")
    found = await get_event_by_id(db, ev.id)
    assert found is not None
    assert found.slug == "test-event"


@pytest.mark.anyio
async def test_list_events(db: AsyncSession):
    await create_event(db, slug="ev-a", display_name="A")
    await create_event(db, slug="ev-b", display_name="B")
    events = await list_events(db)
    assert len(events) == 2


@pytest.mark.anyio
async def test_delete_event(db: AsyncSession):
    ev = await create_event(db, slug="to-delete", display_name="Delete Me")
    assert await delete_event(db, ev.id) is True
    assert await get_event_by_id(db, ev.id) is None


@pytest.mark.anyio
async def test_delete_event_with_a_relay_booth(db: AsyncSession):
    """rooms.relay_booth_id points at a booth that points back at the room."""
    # A throwaway event with its own rooms first, so the target event's id cannot
    # coincide with any of its room ids and hide a filter reading the wrong column.
    other = await create_event(db, slug="ev-offset", display_name="Offset")
    for i in range(3):
        await create_room(db, event_id=other.id, display_name=f"Other {i}")
    ev = await create_event(db, slug="ev-relay-del", display_name="Ev")
    rooms = []
    for code, name in (("en", "English"), ("es", "Spanish"), ("fr", "French")):
        room = await create_room(db, event_id=ev.id, display_name=f"Room {code}")
        booth = await create_booth(db, event_id=ev.id, room_id=room.id, language_code=code, language_name=name)
        room.relay_booth_id = booth.id
        db.add(InviteToken(booth_id=booth.id, token=generate_token(), role="interpreter"))
        rooms.append((room, booth))
    await db.flush()
    assert all(room.id != ev.id for room, _ in rooms)

    for room, booth in rooms:
        assert len(await list_booths_for_room(db, room.id)) == 1
        assert len(await list_tokens_for_booth(db, booth.id)) == 1

    assert await delete_event(db, ev.id) is True
    assert await get_event_by_id(db, ev.id) is None
    for room, booth in rooms:
        assert await get_room_by_id(db, room.id) is None
        assert await get_booth_by_id(db, booth.id) is None
        assert await list_booths_for_room(db, room.id) == []
        assert await list_tokens_for_booth(db, booth.id) == []


@pytest.mark.anyio
async def test_delete_event_with_a_relay_booth_already_loaded(db: AsyncSession):
    """Clearing only the column would leave the loaded relationship holding the cycle."""
    from sqlalchemy import select as sa_select
    from sqlalchemy.orm import joinedload

    ev = await create_event(db, slug="ev-relay-loaded", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Room")
    booth = await create_booth(db, event_id=ev.id, room_id=room.id, language_code="en", language_name="English")
    room.relay_booth_id = booth.id
    await db.flush()

    loaded = await db.execute(sa_select(Room).where(Room.id == room.id).options(joinedload(Room.relay_booth)))
    assert loaded.scalars().first().relay_booth is not None

    assert await delete_event(db, ev.id) is True
    assert await get_event_by_id(db, ev.id) is None
    assert await get_room_by_id(db, room.id) is None


@pytest.mark.anyio
async def test_delete_event_not_found(db: AsyncSession):
    assert await delete_event(db, 99999) is False


# ---------------------------------------------------------------------------
# Room CRUD
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_room(db: AsyncSession):
    ev = await create_event(db, slug="ev-room", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Main Hall")
    assert room.id is not None
    assert room.event_id == ev.id
    assert room.display_name == "Main Hall"
    assert room.eventyay_room_id is None
    assert room.audio_delay_ms == 0


@pytest.mark.anyio
async def test_room_audio_delay_defaults_to_zero(db: AsyncSession):
    ev = await create_event(db, slug="ev-room-delay", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Main Hall")
    loaded = await get_room_by_id(db, room.id)
    assert loaded is not None
    assert loaded.audio_delay_ms == 0


@pytest.mark.anyio
async def test_create_room_with_eventyay_id(db: AsyncSession):
    ev = await create_event(db, slug="ev-room2", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall", eventyay_room_id="room-123")
    assert room.eventyay_room_id == "room-123"


@pytest.mark.anyio
async def test_get_room_by_id(db: AsyncSession):
    ev = await create_event(db, slug="ev-room3", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    found = await get_room_by_id(db, room.id)
    assert found is not None
    assert found.display_name == "Hall"


@pytest.mark.anyio
async def test_list_rooms_for_event(db: AsyncSession):
    ev = await create_event(db, slug="ev-rooms", display_name="Ev")
    await create_room(db, event_id=ev.id, display_name="Room 1")
    await create_room(db, event_id=ev.id, display_name="Room 2")
    rooms = await list_rooms_for_event(db, ev.id)
    assert len(rooms) == 2


@pytest.mark.anyio
async def test_delete_room(db: AsyncSession):
    ev = await create_event(db, slug="ev-room-del", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Room")
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
    ev = await create_event(db, slug="ev-booth", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="fr",
        language_name="French",
    )
    assert booth.id is not None
    assert booth.language_code == "fr"
    assert booth.language_name == "French"


@pytest.mark.anyio
async def test_booth_language_code_validation(db: AsyncSession):
    ev = await create_event(db, slug="ev-booth-val", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    with pytest.raises(ValueError):
        await create_booth(
            db,
            event_id=ev.id,
            room_id=room.id,
            language_code="xyz",
            language_name="Bad",
        )


@pytest.mark.anyio
async def test_booth_mediamtx_path_derived(db: AsyncSession):
    ev = await create_event(db, slug="pycon2026", display_name="PyCon 2026")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    # Reload with event joinedloaded
    loaded = await get_booth_by_id(db, booth.id)
    assert loaded is not None
    assert loaded.mediamtx_path == "pycon2026/1/en"


@pytest.mark.anyio
async def test_booth_mediamtx_path_not_column(db: AsyncSession):
    """mediamtx_path is a property, NOT a stored column."""
    columns = {c.name for c in DBBooth.__table__.columns}
    assert "mediamtx_path" not in columns


@pytest.mark.anyio
async def test_no_hls_url_column(db: AsyncSession):
    """No hls_url column in any table — WHEP is the only playback protocol."""
    for model in [Event, Room, DBBooth, InviteToken]:
        columns = {c.name for c in model.__table__.columns}
        assert "hls_url" not in columns


@pytest.mark.anyio
async def test_get_booth_by_id(db: AsyncSession):
    ev = await create_event(db, slug="ev-booth-get", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="de",
        language_name="German",
    )
    found = await get_booth_by_id(db, booth.id)
    assert found is not None
    assert found.language_code == "de"


@pytest.mark.anyio
async def test_list_booths_for_event(db: AsyncSession):
    ev = await create_event(db, slug="ev-booths", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    await create_booth(db, event_id=ev.id, room_id=room.id, language_code="en", language_name="English")
    await create_booth(db, event_id=ev.id, room_id=room.id, language_code="fr", language_name="French")
    booths = await list_booths_for_event(db, ev.id)
    assert len(booths) == 2
    assert booths[0].language_code == "en"  # ordered by language_code


@pytest.mark.anyio
async def test_list_all_booths_for_events_empty_input(db: AsyncSession):
    result = await list_all_booths_for_events(db, [])
    assert result == {}


@pytest.mark.anyio
async def test_list_all_booths_for_events_single_event(db: AsyncSession):
    ev = await create_event(db, slug="ev-all-single", display_name="Ev Single")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    await create_booth(db, event_id=ev.id, room_id=room.id, language_code="en", language_name="English")
    await create_booth(db, event_id=ev.id, room_id=room.id, language_code="fr", language_name="French")
    result = await list_all_booths_for_events(db, [ev.id])
    assert list(result.keys()) == [ev.id]
    assert len(result[ev.id]) == 2
    assert result[ev.id][0].language_code == "en"
    assert result[ev.id][1].language_code == "fr"


@pytest.mark.anyio
async def test_list_all_booths_for_events_multiple_events(db: AsyncSession):
    ev1 = await create_event(db, slug="ev-all-multi1", display_name="Ev 1")
    ev2 = await create_event(db, slug="ev-all-multi2", display_name="Ev 2")
    room1 = await create_room(db, event_id=ev1.id, display_name="Hall 1")
    room2 = await create_room(db, event_id=ev2.id, display_name="Hall 2")
    await create_booth(db, event_id=ev1.id, room_id=room1.id, language_code="en", language_name="English")
    await create_booth(db, event_id=ev1.id, room_id=room1.id, language_code="fr", language_name="French")
    await create_booth(db, event_id=ev2.id, room_id=room2.id, language_code="de", language_name="German")
    await create_booth(db, event_id=ev2.id, room_id=room2.id, language_code="ja", language_name="Japanese")
    result = await list_all_booths_for_events(db, [ev1.id, ev2.id])
    assert len(result[ev1.id]) == 2
    assert len(result[ev2.id]) == 2
    assert result[ev1.id][0].language_code == "en"
    assert result[ev2.id][0].language_code == "de"


@pytest.mark.anyio
async def test_list_all_booths_for_events_event_with_no_booths(db: AsyncSession):
    ev1 = await create_event(db, slug="ev-all-nobooth1", display_name="Ev With Booths")
    ev2 = await create_event(db, slug="ev-all-nobooth2", display_name="Ev No Booths")
    room1 = await create_room(db, event_id=ev1.id, display_name="Hall")
    await create_booth(db, event_id=ev1.id, room_id=room1.id, language_code="en", language_name="English")
    await create_booth(db, event_id=ev1.id, room_id=room1.id, language_code="fr", language_name="French")
    result = await list_all_booths_for_events(db, [ev1.id, ev2.id])
    assert len(result[ev1.id]) == 2
    assert result[ev2.id] == []


@pytest.mark.anyio
async def test_list_booths_for_room(db: AsyncSession):
    ev = await create_event(db, slug="ev-booths-room", display_name="Ev")
    room1 = await create_room(db, event_id=ev.id, display_name="Room 1")
    room2 = await create_room(db, event_id=ev.id, display_name="Room 2")
    await create_booth(db, event_id=ev.id, room_id=room1.id, language_code="en", language_name="English")
    await create_booth(db, event_id=ev.id, room_id=room2.id, language_code="fr", language_name="French")
    booths = await list_booths_for_room(db, room1.id)
    assert len(booths) == 1
    assert booths[0].language_code == "en"


@pytest.mark.anyio
async def test_delete_booth(db: AsyncSession):
    ev = await create_event(db, slug="ev-booth-del", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="es",
        language_name="Spanish",
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
    ev = await create_event(db, slug="ev-token", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    tok = await create_invite_token(
        db,
        booth_id=booth.id,
        role="interpreter",
        label="Alice Invite",
        created_by="admin@test.com",
    )
    assert len(tok.token) == 64
    assert tok.role == "interpreter"
    assert tok.label == "Alice Invite"
    assert tok.created_by == "admin@test.com"
    assert tok.used_at is None
    assert tok.expires_at is None


@pytest.mark.anyio
async def test_invite_token_role_validation(db: AsyncSession):
    ev = await create_event(db, slug="ev-token-val", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    with pytest.raises(ValueError, match="Invalid role"):
        await create_invite_token(db, booth_id=booth.id, role="hacker")


@pytest.mark.anyio
@pytest.mark.parametrize("role", sorted(ALL_ROLES))
async def test_invite_token_all_valid_roles(db: AsyncSession, role: str):
    slug = f"ev-role-{role.replace('_', '-')}"
    ev = await create_event(db, slug=slug, display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    tok = await create_invite_token(db, booth_id=booth.id, role=role)
    assert tok.role == role


@pytest.mark.anyio
async def test_get_invite_token(db: AsyncSession):
    ev = await create_event(db, slug="ev-token-get", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    tok = await create_invite_token(db, booth_id=booth.id, role="interpreter")
    found = await get_invite_token(db, tok.token)
    assert found is not None
    assert found.token == tok.token
    # Verify joinedload brings in booth and event
    assert found.booth is not None
    assert found.booth.event is not None
    assert found.booth.event.slug == "ev-token-get"


@pytest.mark.anyio
async def test_get_invite_token_not_found(db: AsyncSession):
    result = await get_invite_token(db, "a" * 64)
    assert result is None


@pytest.mark.anyio
async def test_redeem_invite_token(db: AsyncSession):
    ev = await create_event(db, slug="ev-redeem", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    tok = await create_invite_token(db, booth_id=booth.id, role="interpreter")
    assert tok.is_used is False

    redeemed = await redeem_invite_token(db, tok.token)
    assert redeemed is not None
    assert redeemed.is_used is True
    assert redeemed.used_at is not None


@pytest.mark.anyio
async def test_redeem_invite_token_already_used(db: AsyncSession):
    ev = await create_event(db, slug="ev-redeem2", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    tok = await create_invite_token(db, booth_id=booth.id, role="interpreter")
    await redeem_invite_token(db, tok.token)
    with pytest.raises(ValueError, match="already been used"):
        await redeem_invite_token(db, tok.token)


@pytest.mark.anyio
async def test_redeem_invite_token_expired(db: AsyncSession):
    ev = await create_event(db, slug="ev-redeem3", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    tok = await create_invite_token(
        db,
        booth_id=booth.id,
        role="interpreter",
        expires_at=past,
    )
    assert tok.is_expired is True
    with pytest.raises(ValueError, match="expired"):
        await redeem_invite_token(db, tok.token)


@pytest.mark.anyio
async def test_redeem_invite_token_not_found(db: AsyncSession):
    result = await redeem_invite_token(db, "b" * 64)
    assert result is None


@pytest.mark.anyio
async def test_invite_token_not_expired_when_no_expiry(db: AsyncSession):
    ev = await create_event(db, slug="ev-noexp", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    tok = await create_invite_token(db, booth_id=booth.id, role="interpreter")
    assert tok.is_expired is False


@pytest.mark.anyio
async def test_invite_token_not_expired_when_future(db: AsyncSession):
    ev = await create_event(db, slug="ev-futureexp", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    future = datetime.now(tz=timezone.utc) + timedelta(hours=24)
    tok = await create_invite_token(
        db,
        booth_id=booth.id,
        role="interpreter",
        expires_at=future,
    )
    assert tok.is_expired is False


@pytest.mark.anyio
async def test_list_tokens_for_booth(db: AsyncSession):
    ev = await create_event(db, slug="ev-list-tok", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    await create_invite_token(db, booth_id=booth.id, role="interpreter")
    await create_invite_token(db, booth_id=booth.id, role="interpreter")
    tokens = await list_tokens_for_booth(db, booth.id)
    assert len(tokens) == 2


# ---------------------------------------------------------------------------
# Cascade deletes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cascade_delete_event_removes_rooms_and_booths(db: AsyncSession):
    ev = await create_event(db, slug="ev-cascade", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    await create_invite_token(db, booth_id=booth.id, role="interpreter")

    await delete_event(db, ev.id)
    assert await get_room_by_id(db, room.id) is None
    assert await get_booth_by_id(db, booth.id) is None


async def _seed_event_with_every_scoped_row(db: AsyncSession, slug: str) -> dict:
    """Seed one row in every table that becomes unreachable when the event is deleted."""
    from portal.database import create_user
    from portal.models import (
        BoothMembership,
        BoothTranslationLanguage,
        DeveloperAccount,
        EventAPIKey,
        EventMembership,
        OAuthAuditLog,
        OAuthAuthorizationCode,
        OAuthClient,
        OAuthConsentGrant,
        OAuthToken,
        RoomMembership,
        RoomTranslationLanguage,
        TranscriptSegment,
        TranscriptTranslation,
        UsageMetric,
    )

    now = utc_now()
    ev = await create_event(db, slug=slug, display_name=slug)
    room = await create_room(db, event_id=ev.id, display_name=f"{slug} Hall")
    booth = await create_booth(db, event_id=ev.id, room_id=room.id, language_code="en", language_name="English")
    user = await create_user(db, email=f"{slug}@example.com", display_name=slug)
    await create_invite_token(db, booth_id=booth.id, role="interpreter")
    db.add_all(
        [
            BoothMembership(user_id=user.id, booth_id=booth.id, role="interpreter"),
            EventMembership(user_id=user.id, event_id=ev.id, role="event_owner"),
            RoomMembership(user_id=user.id, room_id=room.id, role="room_coordinator"),
            RoomTranslationLanguage(room_id=room.id, language_code="es", language_name="Spanish"),
            BoothTranslationLanguage(booth_id=booth.id, language_code="es", language_name="Spanish"),
            EventAPIKey(event_id=ev.id, name=slug, preview="pfx", key_hash=generate_token()),
            UsageMetric(event_id=ev.id, metric_name="minutes", value=1),
        ]
    )
    seg = TranscriptSegment(room_id=room.id, booth_id=booth.id, language_code="en", text="hello")
    db.add(seg)
    await db.flush()
    db.add(TranscriptTranslation(segment_id=seg.id, language_code="es", text="hola"))

    dev = DeveloperAccount(user_id=user.id, organization_name=slug)
    db.add(dev)
    await db.flush()
    client = OAuthClient(developer_account_id=dev.id, client_id=f"cid-{slug}", name=slug)
    db.add(client)
    await db.flush()
    tok = OAuthToken(
        client_id=client.id,
        user_id=user.id,
        event_id=ev.id,
        access_token_hash=generate_token(),
        expires_at=now + timedelta(hours=1),
    )
    db.add(tok)
    await db.flush()
    db.add_all(
        [
            OAuthAuditLog(
                token_id=tok.id,
                client_id=client.id,
                event_id=ev.id,
                action="token.issued",
                request_path="/oauth/token",
                status_code=200,
            ),
            OAuthConsentGrant(client_id=client.id, user_id=user.id, event_id=ev.id),
            OAuthAuthorizationCode(
                client_id=client.id,
                user_id=user.id,
                event_id=ev.id,
                code_hash=generate_token(),
                code_challenge="c" * 43,
                code_challenge_method="S256",
                redirect_uri="https://example.com/cb",
                expires_at=now + timedelta(minutes=5),
            ),
        ]
    )
    await db.flush()
    return {
        "event": ev.id,
        "room": room.id,
        "booth": booth.id,
        "user": user.id,
        "segment": seg.id,
        "token": tok.id,
        "client": client.id,
    }


async def _scoped_row_counts(db: AsyncSession, ids: dict) -> dict[str, int]:
    """Count rows still tied to this event, room, booth or segment."""
    from sqlalchemy import text

    queries = {
        "events": ("events", "id = :event"),
        "rooms": ("rooms", "event_id = :event"),
        "booths": ("booths", "event_id = :event"),
        "invite_tokens": ("invite_tokens", "booth_id = :booth"),
        "booth_memberships": ("booth_memberships", "booth_id = :booth"),
        "booth_translation_languages": ("booth_translation_languages", "booth_id = :booth"),
        "room_translation_languages": ("room_translation_languages", "room_id = :room"),
        "event_api_keys": ("event_api_keys", "event_id = :event"),
        "usage_metrics": ("usage_metrics", "event_id = :event"),
        "transcript_segments": ("transcript_segments", "room_id = :room"),
        "transcript_translations": ("transcript_translations", "segment_id = :segment"),
        "event_memberships": ("event_memberships", "event_id = :event"),
        "room_memberships": ("room_memberships", "room_id = :room"),
        "oauth_tokens": ("oauth_tokens", "event_id = :event"),
        "oauth_consent_grants": ("oauth_consent_grants", "event_id = :event"),
        "oauth_authorization_codes": ("oauth_authorization_codes", "event_id = :event"),
        "oauth_audit_logs": ("oauth_audit_logs", "event_id = :event OR token_id = :token"),
    }
    out = {}
    for label, (table, where) in queries.items():
        out[label] = (await db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), ids)).scalar()
    return out


@pytest.mark.anyio
async def test_delete_event_removes_every_scoped_row(db: AsyncSession):
    """PRAGMA foreign_keys is 0, so ondelete= never fires and only ORM cascades run.

    Everything scoped to the event must still go, or the rows outlive it and a reused
    id later resolves them against unrelated data.
    """
    ids = await _seed_event_with_every_scoped_row(db, "purge")
    before = await _scoped_row_counts(db, ids)
    assert all(n == 1 for n in before.values()), before

    assert await delete_event(db, ids["event"]) is True
    after = await _scoped_row_counts(db, ids)
    assert after == dict.fromkeys(before, 0), {k: v for k, v in after.items() if v}


@pytest.mark.anyio
async def test_delete_event_keeps_the_oauth_audit_trail(db: AsyncSession):
    """oauth_audit_logs has SET NULL on every foreign key, so the row outlives the event.

    Deleting the rows instead would destroy an audit trail the schema asks us to keep.
    """
    from sqlalchemy import text

    ids = await _seed_event_with_every_scoped_row(db, "audit")
    assert await delete_event(db, ids["event"]) is True

    rows = (await db.execute(text("SELECT token_id, client_id, event_id, action FROM oauth_audit_logs"))).all()
    assert len(rows) == 1, rows
    token_id, client_id, event_id, action = rows[0]
    assert (token_id, event_id) == (None, None)
    assert action == "token.issued"
    # the client is not event scoped, so its reference must survive untouched
    assert client_id == ids["client"]


@pytest.mark.anyio
async def test_delete_event_with_its_own_relay_booth(db: AsyncSession):
    """rooms.relay_booth_id -> booths.id and booths.room_id -> rooms.id form a cycle.

    Clearing the pointer first is what lets the unit of work order the deletes at all.
    """
    ev = await create_event(db, slug="ev-own-relay", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(db, event_id=ev.id, room_id=room.id, language_code="en", language_name="English")
    room.relay_booth_id = booth.id
    await db.flush()

    assert await delete_event(db, ev.id) is True
    assert await get_event_by_id(db, ev.id) is None
    assert await get_room_by_id(db, room.id) is None
    assert await get_booth_by_id(db, booth.id) is None


@pytest.mark.anyio
async def test_delete_event_with_its_own_relay_booth_already_loaded(db: AsyncSession):
    """Same cycle, but with the relay_booth relationship already in the identity map."""
    from sqlalchemy import select as sa_select
    from sqlalchemy.orm import joinedload

    ev = await create_event(db, slug="ev-own-relay-loaded", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(db, event_id=ev.id, room_id=room.id, language_code="en", language_name="English")
    room.relay_booth_id = booth.id
    await db.flush()
    loaded = await db.execute(sa_select(Room).where(Room.id == room.id).options(joinedload(Room.relay_booth)))
    assert loaded.scalars().first().relay_booth is not None

    assert await delete_event(db, ev.id) is True
    assert await get_room_by_id(db, room.id) is None


@pytest.mark.anyio
async def test_delete_event_clears_a_cross_event_relay_pointer(db: AsyncSession):
    """admin_edit_room stores relay_booth_id unvalidated, so it can cross events.

    Scoping the clear by the room's event instead of by booth would leave the surviving
    event's room pointing at a booth that no longer exists.
    """
    survivor = await create_event(db, slug="ev-survivor", display_name="Survivor")
    doomed = await create_event(db, slug="ev-doomed", display_name="Doomed")
    survivor_room = await create_room(db, event_id=survivor.id, display_name="Survivor Hall")
    doomed_room = await create_room(db, event_id=doomed.id, display_name="Doomed Hall")
    doomed_booth = await create_booth(
        db, event_id=doomed.id, room_id=doomed_room.id, language_code="en", language_name="English"
    )
    survivor_room.relay_booth_id = doomed_booth.id
    await db.flush()

    assert await delete_event(db, doomed.id) is True
    fresh = await get_room_by_id(db, survivor_room.id)
    assert fresh is not None, "the surviving event's room must not be deleted"
    assert fresh.relay_booth_id is None


@pytest.mark.anyio
async def test_delete_event_removes_a_segment_reached_only_by_its_booth(db: AsyncSession):
    """transcript_segments reaches the event by room_id AND booth_id, and they can differ.

    admin_create_booth takes both ids from the URL without checking that the room belongs
    to the event, so a booth owned here can sit in another event's room. Predicating on
    room_id alone leaves the segment pointing at a booth id SQLite is free to reuse.
    """
    other = await create_event(db, slug="ev-seg-other", display_name="Other")
    other_room = await create_room(db, event_id=other.id, display_name="Other Hall")
    ev = await create_event(db, slug="ev-seg-owner", display_name="Owner")
    # booth owned by ev, sitting in the other event's room
    booth = await create_booth(db, event_id=ev.id, room_id=other_room.id, language_code="en", language_name="English")
    seg = TranscriptSegment(room_id=other_room.id, booth_id=booth.id, language_code="en", text="cross")
    db.add(seg)
    await db.flush()
    seg_id = seg.id

    assert await delete_event(db, ev.id) is True
    assert await get_booth_by_id(db, booth.id) is None
    assert (
        await db.execute(sa.select(TranscriptSegment).where(TranscriptSegment.id == seg_id))
    ).scalars().first() is None
    # the other event's room is untouched
    assert await get_room_by_id(db, other_room.id) is not None


@pytest.mark.anyio
async def test_delete_room_removes_its_transcripts_and_memberships(db: AsyncSession):
    """Anything delete_room strands can never be reached again.

    delete_event only finds these rows through the event's rooms, so a row left behind
    here outlives the event too, holding a room_id SQLite can hand out again.
    """
    from portal.database import create_user
    from portal.models import RoomMembership

    ev = await create_event(db, slug="ev-room-purge", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(db, event_id=ev.id, room_id=room.id, language_code="en", language_name="English")
    user = await create_user(db, email="rp@example.com", display_name="RP")
    db.add(RoomMembership(user_id=user.id, room_id=room.id, role="room_coordinator"))
    seg = TranscriptSegment(room_id=room.id, booth_id=booth.id, language_code="en", text="hi")
    db.add(seg)
    await db.flush()
    db.add(TranscriptTranslation(segment_id=seg.id, language_code="es", text="hola"))
    await db.flush()
    room_id, seg_id = room.id, seg.id

    assert await delete_room(db, room_id) is True
    left_seg = (await db.execute(sa.select(TranscriptSegment).where(TranscriptSegment.id == seg_id))).scalars().all()
    left_tr = (
        (await db.execute(sa.select(TranscriptTranslation).where(TranscriptTranslation.segment_id == seg_id)))
        .scalars()
        .all()
    )
    left_rm = (await db.execute(sa.select(RoomMembership).where(RoomMembership.room_id == room_id))).scalars().all()
    assert (left_seg, left_tr, left_rm) == ([], [], [])


@pytest.mark.anyio
async def test_delete_room_removes_booth_translation_languages(db: AsyncSession):
    """The booth delete in delete_room is a Core bulk delete, so no ORM cascade runs."""
    from portal.models import BoothTranslationLanguage

    ev = await create_event(db, slug="ev-btl", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(db, event_id=ev.id, room_id=room.id, language_code="en", language_name="English")
    db.add(BoothTranslationLanguage(booth_id=booth.id, language_code="es", language_name="Spanish"))
    await db.flush()

    assert await delete_room(db, room.id) is True
    left = (
        (await db.execute(sa.select(BoothTranslationLanguage).where(BoothTranslationLanguage.booth_id == booth.id)))
        .scalars()
        .all()
    )
    assert left == []


@pytest.mark.anyio
async def test_delete_room_clears_another_rooms_relay_pointer(db: AsyncSession):
    """A second room can relay from this room's booths; those pointers go too."""
    ev = await create_event(db, slug="ev-relay-rm", display_name="Ev")
    doomed = await create_room(db, event_id=ev.id, display_name="Doomed")
    other = await create_room(db, event_id=ev.id, display_name="Other")
    booth = await create_booth(db, event_id=ev.id, room_id=doomed.id, language_code="en", language_name="English")
    other.relay_booth_id = booth.id
    await db.flush()

    assert await delete_room(db, doomed.id) is True
    fresh = await get_room_by_id(db, other.id)
    assert fresh is not None
    assert fresh.relay_booth_id is None


@pytest.mark.anyio
async def test_delete_event_clears_a_cross_event_parent_token(db: AsyncSession):
    """oauth_tokens.parent_token_id is SET NULL, which SQLite never enforces.

    No current path builds a chain across events, so this guards the invariant rather
    than a reachable bug.
    """
    from portal.database import create_user
    from portal.models import DeveloperAccount, OAuthClient, OAuthToken

    doomed = await create_event(db, slug="ev-tok-doomed", display_name="Doomed")
    keep = await create_event(db, slug="ev-tok-keep", display_name="Keep")
    user = await create_user(db, email="tok@example.com", display_name="Tok")
    dev = DeveloperAccount(user_id=user.id, organization_name="Org")
    db.add(dev)
    await db.flush()
    client = OAuthClient(developer_account_id=dev.id, client_id="cid-tok", name="Tok")
    db.add(client)
    await db.flush()
    parent = OAuthToken(
        client_id=client.id,
        user_id=user.id,
        event_id=doomed.id,
        access_token_hash=generate_token(),
        expires_at=utc_now() + timedelta(hours=1),
    )
    db.add(parent)
    await db.flush()
    child = OAuthToken(
        client_id=client.id,
        user_id=user.id,
        event_id=keep.id,
        access_token_hash=generate_token(),
        parent_token_id=parent.id,
        expires_at=utc_now() + timedelta(hours=1),
    )
    db.add(child)
    await db.flush()
    child_id = child.id

    assert await delete_event(db, doomed.id) is True
    fresh = (await db.execute(sa.select(OAuthToken).where(OAuthToken.id == child_id))).scalars().first()
    assert fresh is not None, "the surviving event's token must not be deleted"
    assert fresh.parent_token_id is None


@pytest.mark.anyio
async def test_delete_event_leaves_another_events_rows_alone(db: AsyncSession):
    """A subquery reading the wrong column would purge every event, not just this one."""
    keep = await _seed_event_with_every_scoped_row(db, "keep")
    drop = await _seed_event_with_every_scoped_row(db, "drop")
    assert keep["event"] != drop["event"]

    keep_before = await _scoped_row_counts(db, keep)
    assert await delete_event(db, drop["event"]) is True

    assert await _scoped_row_counts(db, drop) == dict.fromkeys(keep_before, 0)
    assert await _scoped_row_counts(db, keep) == keep_before


@pytest.mark.anyio
async def test_cascade_delete_room_removes_booths(db: AsyncSession):
    ev = await create_event(db, slug="ev-cascade2", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="fr",
        language_name="French",
    )
    await delete_room(db, room.id)
    assert await get_booth_by_id(db, booth.id) is None


# ---------------------------------------------------------------------------
# Model __repr__
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_event_repr(db: AsyncSession):
    ev = await create_event(db, slug="repr-test", display_name="Test")
    assert "repr-test" in repr(ev)


@pytest.mark.anyio
async def test_room_repr(db: AsyncSession):
    ev = await create_event(db, slug="repr-room", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Main Hall")
    assert "Main Hall" in repr(room)


@pytest.mark.anyio
async def test_booth_repr(db: AsyncSession):
    ev = await create_event(db, slug="repr-booth", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    assert "en" in repr(booth)


@pytest.mark.anyio
async def test_invite_token_repr(db: AsyncSession):
    ev = await create_event(db, slug="repr-tok", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Hall")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    tok = await create_invite_token(db, booth_id=booth.id, role="interpreter")
    r = repr(tok)
    assert "interpreter" in r
    assert tok.token[:8] in r


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_events_limit(db: AsyncSession):
    for i in range(5):
        await create_event(db, slug=f"ev-lim-{i}", display_name=f"Ev {i}")
    events = await list_events(db, limit=2)
    assert len(events) == 2


@pytest.mark.anyio
async def test_list_events_offset(db: AsyncSession):
    for i in range(5):
        await create_event(db, slug=f"ev-off-{i}", display_name=f"Ev {i}")
    events = await list_events(db, limit=2, offset=3)
    assert len(events) == 2
    assert events[0].slug == "ev-off-3"  # 4th created event


@pytest.mark.anyio
async def test_list_rooms_pagination(db: AsyncSession):
    ev = await create_event(db, slug="ev-rm-pag", display_name="Ev")
    for i in range(5):
        await create_room(db, event_id=ev.id, display_name=f"Room {i}")
    rooms = await list_rooms_for_event(db, ev.id, limit=3)
    assert len(rooms) == 3


@pytest.mark.anyio
async def test_list_booths_pagination(db: AsyncSession):
    ev = await create_event(db, slug="ev-bth-pag", display_name="Ev")
    room = await create_room(db, event_id=ev.id, display_name="Room")
    langs = ["en", "fr", "de", "es", "it"]
    for i, lang in enumerate(langs):
        await create_booth(db, event_id=ev.id, room_id=room.id, language_code=lang, language_name=f"Lang {i}")
    booths = await list_booths_for_event(db, ev.id, limit=2)
    assert len(booths) == 2


@pytest.mark.anyio
async def test_list_users_pagination(db: AsyncSession):
    for i in range(5):
        await create_user(db, email=f"u{i}@test.com", display_name=f"U{i}", password_hash="x")
    users = await list_users(db, limit=3, offset=1)
    assert len(users) == 3
    assert users[0].email == "u1@test.com"


@pytest.mark.anyio
async def test_list_events_default_limit_does_not_break(db: AsyncSession):
    for i in range(3):
        await create_event(db, slug=f"ev-def-{i}", display_name=f"Ev {i}")
    events = await list_events(db)
    assert len(events) == 3
