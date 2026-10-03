"""Regression and contract tests for PUT /api/v1/events/{event_slug}/rooms/{eventyay_room_id}.

Covers:
- Room creation and update flows
- Partial update semantics and PR #528 compatibility (omitted fields, target_languages, name)
- Language and booth synchronization
- Active session deletion protection (409 Conflict)
- IntegrityError concurrent creation retry path
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("BOOTH_ACCESS_TOKEN", "")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pass")

import pytest
from sqlalchemy.exc import IntegrityError

ACCESS_TOKEN = "test-room-sync-token"


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed():
    """Create an event whose owner holds a rooms:write token."""
    from portal.auth import hash_password
    from portal.database import create_event, create_user, get_session, set_event_membership
    from portal.models import DeveloperAccount, OAuthClient, OAuthToken

    async with get_session() as s:
        user = await create_user(
            s,
            email="owner@example.com",
            display_name="Owner",
            password_hash=hash_password("password123"),
        )
        event = await create_event(s, slug="synccon", display_name="SyncCon 2026")
        await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")

        dev_account = DeveloperAccount(user_id=user.id, status="approved")
        s.add(dev_account)
        await s.flush()
        client = OAuthClient(developer_account_id=dev_account.id, client_id="sync-client", name="Sync")
        s.add(client)
        await s.flush()
        s.add(
            OAuthToken(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                scopes=["rooms:write"],
                access_token_hash=hashlib.sha256(ACCESS_TOKEN.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
    return event


def _auth():
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


async def _languages_and_booths(room_id: int):
    from sqlalchemy import select

    from portal.database import get_session
    from portal.models import DBBooth, RoomTranslationLanguage

    async with get_session() as s:
        langs = (
            (await s.execute(select(RoomTranslationLanguage).where(RoomTranslationLanguage.room_id == room_id)))
            .scalars()
            .all()
        )
        booths = (await s.execute(select(DBBooth).where(DBBooth.room_id == room_id))).scalars().all()
    return sorted(rl.language_code for rl in langs), sorted(b.language_code for b in booths)


@pytest.mark.anyio
async def test_create_room_success():
    """Verify creating a new room persists the room, returns canonical booths, and logs audit."""
    from sqlalchemy import select

    from portal.database import get_session
    from portal.models import OAuthAuditLog, Room

    event = await _seed()

    async with _client() as c:
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-create-1",
            json={
                "name": "Keynote Room",
                "target_languages": ["fr", "de"],
                "enable_transcription": True,
                "transcription_provider": "local",
                "transcription_model": "base",
                "source_language": "en",
                "enable_translation": True,
                "translation_provider": "openai",
                "translation_model": "gpt-4o-mini",
            },
            headers=_auth(),
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    room_id = data["room_id"]
    assert len(data["booths"]) == 2

    # Verify WHEP and WHIP URLs
    languages = [b["language"] for b in data["booths"]]
    assert sorted(languages) == ["de", "fr"]
    for b in data["booths"]:
        assert b["whip_path"] == f"{event.slug}/{room_id}/{b['language']}"
        assert b["whep_url"].endswith(f"/{event.slug}/{room_id}/{b['language']}/whep")

    # Verify persistence
    async with get_session() as s:
        room = await s.get(Room, room_id)
        assert room is not None
        assert room.display_name == "Keynote Room"
        assert room.eventyay_room_id == "room-create-1"
        assert room.floor_transcription_enabled is True
        assert room.floor_transcription_provider == "local"
        assert room.floor_transcription_model == "base"
        assert room.floor_language_code == "en"
        assert room.floor_translation_enabled is True
        assert room.floor_translation_provider == "openai"
        assert room.floor_translation_model == "gpt-4o-mini"

        # Verify audit log
        audit_res = await s.execute(
            select(OAuthAuditLog).where(
                OAuthAuditLog.action == "room.created",
                OAuthAuditLog.request_path == f"/api/v1/events/{event.slug}/rooms/room-create-1",
            )
        )
        audit = audit_res.scalar_one_or_none()
        assert audit is not None
        assert audit.status_code == 201


@pytest.mark.anyio
async def test_create_room_without_name_is_rejected():
    """Verify room creation fails with 400 when name is missing or empty."""
    event = await _seed()

    async with _client() as c:
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-missing-name",
            json={"target_languages": ["fr"]},
            headers=_auth(),
        )

    assert resp.status_code == 400
    assert "name is required" in resp.json()["detail"]


@pytest.mark.anyio
async def test_update_existing_room():
    """Verify updating an existing room preserves room ID and records room.updated audit log."""
    from sqlalchemy import select

    from portal.database import get_session
    from portal.models import OAuthAuditLog, Room

    event = await _seed()

    async with _client() as c:
        create_resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-update-1",
            json={"name": "Initial Name", "target_languages": ["es"]},
            headers=_auth(),
        )
        assert create_resp.status_code == 200
        room_id = create_resp.json()["room_id"]

        update_resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-update-1",
            json={
                "name": "Updated Name",
                "enable_transcription": True,
                "transcription_provider": "local",
            },
            headers=_auth(),
        )

    assert update_resp.status_code == 200
    assert update_resp.json()["room_id"] == room_id

    async with get_session() as s:
        room = await s.get(Room, room_id)
        assert room.display_name == "Updated Name"
        assert room.floor_transcription_enabled is True

        audit_res = await s.execute(
            select(OAuthAuditLog).where(
                OAuthAuditLog.action == "room.updated",
                OAuthAuditLog.request_path == f"/api/v1/events/{event.slug}/rooms/room-update-1",
            )
        )
        audit = audit_res.scalar_one_or_none()
        assert audit is not None
        assert audit.status_code == 200


@pytest.mark.anyio
async def test_partial_update_keeps_languages_and_booths():
    """Verify omitting target_languages leaves existing translation languages and booths unchanged."""
    event = await _seed()

    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": ["fr", "de"]},
            headers=_auth(),
        )
        assert created.status_code == 200
        room_id = created.json()["room_id"]
        assert await _languages_and_booths(room_id) == (["de", "fr"], ["de", "fr"])

        # A payload without target_languages must not remove anything.
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall Renamed"},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert await _languages_and_booths(room_id) == (["de", "fr"], ["de", "fr"])


@pytest.mark.anyio
async def test_explicit_language_list_reconciles_languages_and_booths():
    """Verify explicitly providing target_languages adds missing and deletes unrequested booths/languages."""
    event = await _seed()

    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": ["fr", "de"]},
            headers=_auth(),
        )
        room_id = created.json()["room_id"]

        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": ["fr", "es"]},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert await _languages_and_booths(room_id) == (["es", "fr"], ["es", "fr"])


@pytest.mark.anyio
async def test_empty_language_list_clears_languages():
    """Verify target_languages: [] clears all existing languages and booths."""
    event = await _seed()

    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": ["fr"]},
            headers=_auth(),
        )
        room_id = created.json()["room_id"]

        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": []},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert await _languages_and_booths(room_id) == ([], [])


@pytest.mark.anyio
async def test_update_without_name_keeps_existing_name():
    """Verify partial update without name preserves the existing room display name."""
    from portal.database import get_session
    from portal.models import Room

    event = await _seed()

    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall"},
            headers=_auth(),
        )
        room_id = created.json()["room_id"]

        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"target_languages": ["fr"]},
            headers=_auth(),
        )

    assert resp.status_code == 200
    async with get_session() as s:
        room = await s.get(Room, room_id)
        assert room.display_name == "Main Hall"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "x" * 201},
        {"name": "Main Hall", "description": "d" * 1001},
        {"name": "Main Hall", "target_languages": ["français"]},
        {"name": "Main Hall", "target_languages": ["x" * 40]},
        {"name": "Main Hall", "target_languages": ["zz"]},
        {"name": "Main Hall", "target_languages": ["floor"]},
    ],
)
async def test_invalid_values_are_rejected(payload):
    """Verify invalid payloads fail schema validation with 422."""
    event = await _seed()

    async with _client() as c:
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json=payload,
            headers=_auth(),
        )

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_active_session_deletion_guard():
    """Verify removing a target language with an active session raises 409 Conflict."""
    from portal.booth_identity import make_booth_id
    from portal.globals import booths

    event = await _seed()

    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-active-guard",
            json={"name": "Active Session Room", "target_languages": ["fr", "de"]},
            headers=_auth(),
        )
        room_id = created.json()["room_id"]

        # Simulate an active connected stream on the 'fr' booth
        booth_id = make_booth_id(event.slug, room_id, "fr")
        booth = booths._get_or_create_booth(booth_id, "French", "fr", room_id=room_id)
        booth.ingest_status = "connected"

        # Attempt to remove 'fr'
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-active-guard",
            json={"name": "Active Session Room", "target_languages": ["de"]},
            headers=_auth(),
        )

        assert resp.status_code == 409
        assert "Cannot remove language 'fr' while it has an active session running." in resp.json()["detail"]

        # Clean up in-memory booth status
        booth.ingest_status = "idle"


@pytest.mark.anyio
async def test_integrity_error_concurrent_creation_retry_path(tmp_path: Path):
    """Verify _create_or_update_base_room recovers from a real SQLite UNIQUE constraint violation.

    Uses a temporary file-backed SQLite database so independent connections/sessions
    model a true concurrent race where a competing session inserts and commits first,
    causing the endpoint's real flush to hit SQLite's UNIQUE constraint.
    """
    from sqlalchemy import event as sa_event
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from fastapi_app import app
    from portal.auth import hash_password
    from portal.database import create_event, create_user, get_db_session, set_event_membership
    from portal.models import Base, DeveloperAccount, OAuthClient, OAuthToken, Room

    # 1. Isolated temporary file-backed SQLite database
    db_file = tmp_path / "test_concurrent_rooms.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    file_engine = create_async_engine(db_url, poolclass=NullPool)

    @sa_event.listens_for(file_engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    async with file_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    file_session_factory = async_sessionmaker(file_engine, expire_on_commit=False)

    # 2. Seed event and OAuth credentials into the file-backed database
    async with file_session_factory() as s:
        async with s.begin():
            user = await create_user(
                s,
                email="owner@example.com",
                display_name="Owner",
                password_hash=hash_password("password123"),
            )
            event = await create_event(s, slug="synccon", display_name="SyncCon 2026")
            await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")

            dev_account = DeveloperAccount(user_id=user.id, status="approved")
            s.add(dev_account)
            await s.flush()
            client = OAuthClient(developer_account_id=dev_account.id, client_id="sync-client", name="Sync")
            s.add(client)
            await s.flush()
            s.add(
                OAuthToken(
                    client_id=client.id,
                    user_id=user.id,
                    event_id=event.id,
                    scopes=["rooms:write"],
                    access_token_hash=hashlib.sha256(ACCESS_TOKEN.encode()).hexdigest(),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            )

    # 3. Inject file-backed session into the endpoint
    async def _override_get_db_session():
        async with file_session_factory() as session:
            async with session.begin():
                yield session

    app.dependency_overrides[get_db_session] = _override_get_db_session

    original_flush = AsyncSession.flush

    # 4. Competing session commits before the endpoint's flush reaches SQLite
    async def _concurrent_insert_and_real_flush(session_self, *args, **kwargs):
        if not getattr(session_self, "_tested_collision", False):
            session_self._tested_collision = True
            async with file_session_factory() as competing_session:
                async with competing_session.begin():
                    competing_session.add(
                        Room(
                            event_id=event.id,
                            display_name="Concurrent Winner",
                            eventyay_room_id="room-concurrent-1",
                        )
                    )
        # Call the REAL flush so SQLite enforces UNIQUE(event_id, eventyay_room_id)
        return await original_flush(session_self, *args, **kwargs)

    try:
        with patch("sqlalchemy.ext.asyncio.AsyncSession.flush", new=_concurrent_insert_and_real_flush):
            async with _client() as c:
                resp = await c.put(
                    f"/api/v1/events/{event.slug}/rooms/room-concurrent-1",
                    json={"name": "Updated After Collision", "enable_transcription": True},
                    headers=_auth(),
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"

        # 5. Verify the existing recovery branch re-queried and updated the room
        async with file_session_factory() as s:
            room = await s.get(Room, data["room_id"])
            assert room is not None
            assert room.display_name == "Updated After Collision"
            assert room.floor_transcription_enabled is True
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        await file_engine.dispose()


@pytest.mark.anyio
async def test_integrity_error_when_room_still_missing_raises_500():
    """Verify _create_or_update_base_room raises 500 if IntegrityError occurs and room is still not found."""
    event = await _seed()

    async def _failing_flush(session_self, *args, **kwargs):
        raise IntegrityError("mock statement", "mock params", "mock orig")

    with patch("sqlalchemy.ext.asyncio.AsyncSession.flush", new=_failing_flush):
        async with _client() as c:
            resp = await c.put(
                f"/api/v1/events/{event.slug}/rooms/room-error-1",
                json={"name": "Will Fail"},
                headers=_auth(),
            )

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to upsert room"
