from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.booth_state import BoothRegistry
from portal.database import configure, dispose, get_session, init_db
from portal.models import (
    BoothMembership,
    BoothTranslationLanguage,
    DBBooth,
    DeveloperAccount,
    Event,
    EventMembership,
    InviteToken,
    OAuthAuditLog,
    OAuthClient,
    OAuthToken,
    Room,
    RoomMembership,
    TranscriptSegment,
    TranscriptTranslation,
    User,
)
from portal.roles import EVENT_OWNER, INTERPRETER, ROOM_COORDINATOR
from portal.routers import api_v1

HEADERS = {"Authorization": "Bearer room-delete-test-token"}
PATH = "/api/v1/events/delete-event/rooms/main"


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(api_v1, "booths", BoothRegistry())
    yield
    await dispose()


@pytest.fixture
async def room_access():
    async with get_session() as session:
        user = User(email="room-owner@example.com", display_name="Room owner")
        event = Event(slug="delete-event", display_name="Delete event")
        other_event = Event(slug="other-event", display_name="Other event")
        session.add_all([user, event, other_event])
        await session.flush()
        account = DeveloperAccount(user_id=user.id, status="approved")
        session.add(account)
        await session.flush()
        client = OAuthClient(developer_account_id=account.id, client_id="delete-client", name="Delete app")
        session.add(client)
        await session.flush()
        session.add_all(
            [
                EventMembership(user_id=user.id, event_id=event.id, role=EVENT_OWNER),
                OAuthToken(
                    client_id=client.id,
                    user_id=user.id,
                    event_id=event.id,
                    scopes=["rooms:write"],
                    access_token_hash=hashlib.sha256(b"room-delete-test-token").hexdigest(),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                ),
            ]
        )
        room = Room(event_id=event.id, eventyay_room_id="main", display_name="Main")
        relaying_room = Room(event_id=event.id, eventyay_room_id="relay", display_name="Relaying room")
        other_room = Room(event_id=other_event.id, eventyay_room_id="main", display_name="Other room")
        session.add_all([room, relaying_room, other_room])
        await session.flush()
        booth = DBBooth(event_id=event.id, room_id=room.id, language_code="de", language_name="German")
        other_booth = DBBooth(
            event_id=other_event.id, room_id=other_room.id, language_code="fr", language_name="French"
        )
        session.add_all([booth, other_booth])
        await session.flush()
        relaying_room.relay_booth_id = booth.id
        other_room.relay_booth_id = other_booth.id
        session.add_all(
            [
                RoomMembership(user_id=user.id, room_id=room.id, role=ROOM_COORDINATOR),
                BoothMembership(user_id=user.id, booth_id=booth.id, role=INTERPRETER),
                InviteToken(token="room-delete-invite", booth_id=booth.id, role=INTERPRETER),
                BoothTranslationLanguage(booth_id=booth.id, language_code="en", language_name="English"),
                RoomMembership(user_id=user.id, room_id=other_room.id, role=ROOM_COORDINATOR),
                BoothMembership(user_id=user.id, booth_id=other_booth.id, role=INTERPRETER),
                InviteToken(token="other-room-invite", booth_id=other_booth.id, role=INTERPRETER),
                BoothTranslationLanguage(booth_id=other_booth.id, language_code="en", language_name="English"),
            ]
        )
        segment = TranscriptSegment(room_id=room.id, booth_id=booth.id, language_code="de", text="Hallo")
        other_segment = TranscriptSegment(
            room_id=other_room.id, booth_id=other_booth.id, language_code="fr", text="Bonjour"
        )
        session.add_all([segment, other_segment])
        await session.flush()
        session.add_all(
            [
                TranscriptTranslation(segment_id=segment.id, language_code="en", text="Hello"),
                TranscriptTranslation(segment_id=other_segment.id, language_code="en", text="Good morning"),
            ]
        )
        return room.id, booth.id, relaying_room.id, other_room.id, other_booth.id


@pytest.mark.anyio
@pytest.mark.parametrize("self_relay", [False, True])
async def test_delete_room_clears_relay_references_and_dependents(room_access, self_relay):
    room_id, booth_id, relaying_room_id, other_room_id, other_booth_id = room_access
    if self_relay:
        async with get_session() as session:
            room = await session.get(Room, room_id)
            room.relay_booth_id = booth_id
    await api_v1.booths.create_booth("delete-event", "de", "German", room_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(PATH, headers=HEADERS)

    assert response.status_code == 204
    assert response.content == b""
    assert api_v1.booths.get_booth_sync(f"delete-event-{room_id}-de") is None
    async with get_session() as session:
        assert await session.get(Room, room_id) is None
        assert await session.get(DBBooth, booth_id) is None
        assert (await session.get(Room, relaying_room_id)).relay_booth_id is None
        assert (await session.get(Room, other_room_id)).relay_booth_id == other_booth_id
        assert await session.get(DBBooth, other_booth_id) is not None
        # Only the other room's dependents remain, so an over-broad delete fails here.
        for column, kept_id in (
            (RoomMembership.room_id, other_room_id),
            (BoothMembership.booth_id, other_booth_id),
            (InviteToken.booth_id, other_booth_id),
            (BoothTranslationLanguage.booth_id, other_booth_id),
            (TranscriptSegment.room_id, other_room_id),
        ):
            assert (await session.scalars(select(column))).all() == [kept_id]
        translated_rooms = await session.scalars(
            select(TranscriptSegment.room_id).join(
                TranscriptTranslation, TranscriptTranslation.segment_id == TranscriptSegment.id
            )
        )
        assert translated_rooms.all() == [other_room_id]
        audit = await session.scalar(select(OAuthAuditLog).where(OAuthAuditLog.action == "room.deleted"))
        assert audit is not None
        assert audit.status_code == 204


@pytest.mark.anyio
async def test_delete_room_rejects_live_booth_without_changing_room_or_registry(room_access):
    room_id, booth_id, *_ = room_access
    async with get_session() as session:
        room = await session.get(Room, room_id)
        room.relay_booth_id = booth_id
    await api_v1.booths.create_booth("delete-event", "de", "German", room_id)
    booth = api_v1.booths.get_booth_sync(f"delete-event-{room_id}-de")
    booth.ingest_status = "connected"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(PATH, headers=HEADERS)

    assert response.status_code == 409
    assert api_v1.booths.get_booth_sync(booth.booth_id) is booth
    async with get_session() as session:
        assert (await session.get(Room, room_id)).relay_booth_id == booth_id
        assert await session.get(DBBooth, booth_id) is not None
        assert await session.scalar(select(OAuthAuditLog).where(OAuthAuditLog.action == "room.deleted")) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "authorized", "expected"),
    [
        (PATH, False, 401),
        ("/api/v1/events/other-event/rooms/main", True, 404),
        ("/api/v1/events/delete-event/rooms/missing", True, 404),
    ],
)
async def test_rejected_room_deletion_preserves_data(room_access, path, authorized, expected):
    room_id, booth_id, _, other_room_id, other_booth_id = room_access
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(path, headers=HEADERS if authorized else {})

    assert response.status_code == expected
    async with get_session() as session:
        assert await session.get(Room, room_id) is not None
        assert await session.get(DBBooth, booth_id) is not None
        assert (await session.get(Room, other_room_id)).relay_booth_id == other_booth_id
