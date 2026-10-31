"""Tests for the api_v1 room sync endpoint (PUT /api/v1/events/{slug}/rooms/{id}).

Covers the partial-update and validation behaviour: omitted fields must leave
existing data alone, and invalid values must be rejected before they reach the
database.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BOOTH_ACCESS_TOKEN", "")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pass")

import pytest

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
                scopes=["rooms:write", "booths:read", "listeners:provision"],
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
async def test_partial_update_keeps_languages_and_booths():
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
async def test_explicit_language_list_still_reconciles():
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
            json={"name": "Main Hall", "target_languages": ["fr"]},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert await _languages_and_booths(room_id) == (["fr"], ["fr"])


@pytest.mark.anyio
async def test_empty_language_list_clears_languages():
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
async def test_update_without_name_keeps_the_existing_name():
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
async def test_create_without_name_is_rejected():
    event = await _seed()

    async with _client() as c:
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-new",
            json={"target_languages": ["fr"]},
            headers=_auth(),
        )

    assert resp.status_code == 400
    assert "name" in resp.text


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
        {"name": "Main Hall", "translation_provider": "unsupported-provider"},
    ],
)
async def test_invalid_values_are_rejected(payload):
    event = await _seed()

    async with _client() as c:
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json=payload,
            headers=_auth(),
        )

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_patch_room_settings_updates_the_room_and_returns_changed_values():
    """Persist supported settings and return the canonical values that changed."""

    from portal.database import create_room, get_session
    from portal.models import Room

    event = await _seed()
    async with get_session() as s:
        room = await create_room(
            s,
            event_id=event.id,
            display_name="Main Hall",
            jitsi_url="https://meet.example.org/main-hall",
        )
        room_id = room.id

    async with _client() as c:
        resp = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json={
                "name": "  Main Hall Renamed  ",
        "audio_delay_ms": 2500,
        "enable_transcription": True,
        "transcription_provider": "local",
        "transcription_model": "base",
        "source_language": "en",
        "translation_provider": "openai",
            },
            headers=_auth(),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "success",
        "room_id": room_id,
        "settings": {
            "display_name": "Main Hall Renamed",
            "audio_delay_ms": 2500,
            "floor_transcription_enabled": True,
            "floor_transcription_provider": "local",
            "floor_transcription_model": "base",
            "floor_language_code": "en",
            "floor_translation_provider": "openai",
        },
    }

    async with get_session() as s:
        updated = await s.get(Room, room_id)
        assert updated is not None
        assert updated.audio_delay_ms == 2500
        assert updated.floor_transcription_enabled is True
        assert updated.floor_transcription_provider == "local"
        assert updated.floor_transcription_model == "base"
        assert updated.floor_language_code == "en"
        assert updated.floor_translation_provider == "openai"
        assert updated.display_name == "Main Hall Renamed"
        assert updated.jitsi_url == "https://meet.example.org/main-hall"


@pytest.mark.anyio
async def test_patch_room_settings_rejects_empty_and_unknown_payloads():
    """Reject empty patches and fields outside the room-settings allow-list."""

    from portal.database import create_room, get_session

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        empty = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={},
            headers=_auth(),
        )
        unknown = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"does_not_exist": True},
            headers=_auth(),
        )

    assert empty.status_code == 400
    assert "At least one room setting" in empty.json()["detail"]
    assert unknown.status_code == 422


@pytest.mark.anyio
async def test_patch_room_settings_rejects_a_transcription_model_for_the_wrong_provider():
    """Reject transcription models that are not supported by the selected provider."""

    from portal.database import create_room, get_session

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        resp = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"transcription_provider": "deepgram", "transcription_model": "base"},
            headers=_auth(),
        )

    assert resp.status_code == 422
    assert "Invalid transcription model" in resp.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_rejects_missing_transcription_provider(monkeypatch):
    """Return a validation error instead of crashing when a room lacks a provider."""

    from portal.database import create_room, get_session
    from portal.routers import api_v1

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    original_verify = api_v1._verify_token_rbac

    async def verify_room_without_provider(*args, **kwargs):
        verified_room = await original_verify(*args, **kwargs)
        assert verified_room is not None
        setattr(verified_room, "floor_transcription_provider", None)
        return verified_room

    monkeypatch.setattr(api_v1, "_verify_token_rbac", verify_room_without_provider)

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"enable_transcription": True},
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "valid transcription provider is required" in response.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_rejects_an_unknown_translation_provider():
    """Reject translation providers that have no registered worker implementation."""

    from portal.database import create_room, get_session

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"translation_provider": "unsupported-provider"},
            headers=_auth(),
        )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_patch_room_settings_resolves_eventyay_room_id():
    """Update a room when the path uses its Eventyay identifier instead of its primary key."""

    from portal.database import get_session
    from portal.models import Room

    event = await _seed()
    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/eventyay-24",
            json={"name": "Main Hall"},
            headers=_auth(),
        )
        assert created.status_code == 200
        internal_room_id = created.json()["room_id"]

        updated = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/eventyay/eventyay-24/settings",
            json={"audio_delay_ms": 1500},
            headers=_auth(),
        )

    assert updated.status_code == 200, updated.text
    assert updated.json()["room_id"] == internal_room_id

    async with get_session() as s:
        room = await s.get(Room, internal_room_id)
        assert room is not None
        assert room.audio_delay_ms == 1500


@pytest.mark.anyio
async def test_patch_room_settings_requires_an_explicit_identifier_kind_on_collisions():
    """Keep internal and Eventyay room IDs from selecting each other during updates."""

    from portal.database import get_session
    from portal.models import Room

    event = await _seed()
    async with _client() as c:
        room_a = await c.put(
            f"/api/v1/events/{event.slug}/rooms/2",
            json={"name": "Room A"},
            headers=_auth(),
        )
        assert room_a.status_code == 200
        room_a_id = room_a.json()["room_id"]

        room_b = await c.put(
            f"/api/v1/events/{event.slug}/rooms/{room_a_id}",
            json={"name": "Room B"},
            headers=_auth(),
        )
        assert room_b.status_code == 200
        room_b_id = room_b.json()["room_id"]
        assert room_a_id != room_b_id

        internal_update = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_a_id}/settings",
            json={"name": "Room A Internal Update"},
            headers=_auth(),
        )
        eventyay_update = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/eventyay/{room_a_id}/settings",
            json={"name": "Room B Eventyay Update"},
            headers=_auth(),
        )

    assert internal_update.status_code == 200, internal_update.text
    assert eventyay_update.status_code == 200, eventyay_update.text
    assert internal_update.json()["room_id"] == room_a_id
    assert eventyay_update.json()["room_id"] == room_b_id

    async with get_session() as s:
        internal_room = await s.get(Room, room_a_id)
        eventyay_room = await s.get(Room, room_b_id)
        assert internal_room is not None
        assert eventyay_room is not None
        assert internal_room.display_name == "Room A Internal Update"
        assert eventyay_room.display_name == "Room B Eventyay Update"


@pytest.mark.anyio
async def test_patch_room_settings_validates_url_before_persisting():
    """Reject an unsafe meeting URL without changing the existing room URL."""

    from portal.database import create_room, get_session
    from portal.models import Room

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall", jitsi_url="https://meet.example.org/main")
        room_id = room.id

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json={"jitsi_url": "http://external.example.org/main"},
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "https://" in response.json()["detail"]
    async with get_session() as s:
        room = await s.get(Room, room_id)
        assert room is not None
        assert room.jitsi_url == "https://meet.example.org/main"


@pytest.mark.anyio
async def test_patch_room_settings_requires_external_provider_configuration():
    """Require event-level API access and a provider key for enabled external transcription."""

    from portal.crypto import encrypt_val
    from portal.database import create_room, get_session
    from portal.models import Event

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"enable_transcription": True, "transcription_provider": "deepgram", "transcription_model": "nova-2"},
            headers=_auth(),
        )
    assert response.status_code == 422
    assert "transcription API" in response.json()["detail"]

    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        assert stored_event is not None
        stored_event.transcription_api_enabled = True
        stored_event.encrypted_deepgram_api_key = encrypt_val("test-deepgram-key")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"enable_transcription": True, "transcription_provider": "deepgram", "transcription_model": "nova-2"},
            headers=_auth(),
        )
    assert response.status_code == 200, response.text


@pytest.mark.anyio
async def test_room_endpoints_resolve_by_eventyay_room_id():
    """Eventyay routes specify eventyay_room_id (e.g. '24'), not internal primary key."""
    event = await _seed()

    async with _client() as c:
        # Create a room with eventyay_room_id="24"
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/24",
            json={"name": "Main Stage", "target_languages": ["fr", "es"]},
            headers=_auth(),
        )
        assert created.status_code == 200
        internal_room_id = created.json()["room_id"]
        # Verify internal room ID is numeric and different from string '24' if autoincrement started at 1
        assert isinstance(internal_room_id, int)

        # 1. Fetch listener token using Eventyay room ID "24"
        token_resp = await c.post(
            f"/api/v1/events/{event.slug}/rooms/24/listener-token",
            headers=_auth(),
        )
        assert token_resp.status_code == 200
        data = token_resp.json()
        assert "listener_token" in data and len(data["listener_token"]) > 20

        # 2. List booths using Eventyay room ID "24"
        booths_resp = await c.get(
            f"/api/v1/events/{event.slug}/rooms/24/booths",
            headers=_auth(),
        )
        assert booths_resp.status_code == 200
        booths_data = booths_resp.json()
        assert len(booths_data) == 2
        assert {b["language_code"] for b in booths_data} == {"fr", "es"}

        # 3. Both endpoints also work with internal numeric room ID
        token_resp_internal = await c.post(
            f"/api/v1/events/{event.slug}/rooms/{internal_room_id}/listener-token",
            headers=_auth(),
        )
        assert token_resp_internal.status_code == 200


@pytest.mark.anyio
async def test_room_endpoints_prioritize_eventyay_id_over_internal_id_collision():
    """When an Eventyay room ID numerically matches another room's internal ID, Eventyay ID takes priority."""
    event = await _seed()

    async with _client() as c:
        # Create Room 1 with eventyay_room_id="2"
        res1 = await c.put(
            f"/api/v1/events/{event.slug}/rooms/2",
            json={"name": "Room A", "target_languages": ["fr"]},
            headers=_auth(),
        )
        assert res1.status_code == 200
        room_a_internal_id = res1.json()["room_id"]  # typically 1 in fresh SQLite

        # Create Room 2 with eventyay_room_id=str(room_a_internal_id) (e.g. "1")
        res2 = await c.put(
            f"/api/v1/events/{event.slug}/rooms/{room_a_internal_id}",
            json={"name": "Room B", "target_languages": ["es"]},
            headers=_auth(),
        )
        assert res2.status_code == 200

        # Querying with room_id=str(room_a_internal_id) must resolve to Room B (matching eventyay_room_id),
        # NOT Room A (whose internal PK happens to match)
        booths_resp = await c.get(
            f"/api/v1/events/{event.slug}/rooms/{room_a_internal_id}/booths",
            headers=_auth(),
        )
        assert booths_resp.status_code == 200
        booths_data = booths_resp.json()
        assert len(booths_data) == 1
        assert booths_data[0]["language_code"] == "es"  # Room B's booth, not Room A's "fr"


@pytest.mark.anyio
async def test_room_endpoint_handles_non_ascii_digit_without_server_error():
    """Non-ASCII digits like '²' (where isdigit() is True but int() raises ValueError) return 404, not 500."""
    event = await _seed()

    async with _client() as c:
        res = await c.get(
            f"/api/v1/events/{event.slug}/rooms/²/booths",
            headers=_auth(),
        )
        assert res.status_code == 404
        assert res.json()["detail"] == "Room not found"
