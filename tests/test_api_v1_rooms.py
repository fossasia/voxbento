"""Tests for API v1 room sync and settings endpoints.

Covers room creation, PATCH/PUT updates, validation, identifier scoping, language and booth
reconciliation, active session deletion protection, and concurrent creation recovery.
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
    from portal.roles import EVENT_OWNER

    async with get_session() as s:
        user = await create_user(
            s,
            email="owner@example.com",
            display_name="Owner",
            password_hash=hash_password("password123"),
        )
        event = await create_event(s, slug="synccon", display_name="SyncCon 2026")
        await set_event_membership(s, user_id=user.id, event_id=event.id, role=EVENT_OWNER)

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
                scopes=[
                    "rooms:write",
                    "booths:read",
                    "booths:write",
                    "sessions:manage",
                    "transcripts:read",
                    "listeners:provision",
                ],
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
async def test_create_without_name_is_rejected():
    """Verify room creation fails with 400 when name is missing or empty."""
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
async def test_explicit_language_list_still_reconciles():
    """Verify explicitly providing target_languages reconciles list by removing omitted languages."""
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
async def test_update_without_name_keeps_the_existing_name():
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
        {"name": "Main Hall", "translation_provider": "unsupported-provider"},
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
async def test_patch_room_settings_updates_the_room_and_returns_changed_values():
    """Persist supported settings and return the canonical values that changed."""

    from sqlalchemy import select

    from portal.database import create_room, get_session
    from portal.models import OAuthAuditLog, Room

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
                "translation_model": "gpt-4o-mini",
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
            "floor_translation_model": "gpt-4o-mini",
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
        assert updated.floor_translation_model == "gpt-4o-mini"
        assert updated.display_name == "Main Hall Renamed"
        assert updated.jitsi_url == "https://meet.example.org/main-hall"
        audit = await s.scalar(
            select(OAuthAuditLog).where(
                OAuthAuditLog.action == "room.settings_updated",
                OAuthAuditLog.request_path == f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            )
        )
        assert audit is not None
        assert audit.event_id == event.id
        assert audit.status_code == 200


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
@pytest.mark.parametrize("field", ["floor_ai_interpreter_persona", "floor_ai_interpretation_style"])
async def test_patch_room_settings_bounds_ai_instruction_text(field):
    """Stored prompt configuration is bounded to the same size the prompt builder consumes."""

    from portal.database import create_room, get_session

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={field: "x" * 601},
            headers=_auth(),
        )

    assert response.status_code == 422


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
async def test_patch_room_settings_requires_model_when_translation_provider_changes():
    """A model from the previous provider must not be retained after a provider switch."""

    from portal.database import create_room, get_session
    from portal.models import Room

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room.floor_translation_provider = "openai"
        room.floor_translation_model = "gpt-4o-mini"
        room_id = room.id

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json={"translation_provider": "anthropic"},
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "translation_model is required" in response.json()["detail"]
    async with get_session() as s:
        stored = await s.get(Room, room_id)
        assert stored.floor_translation_provider == "openai"
        assert stored.floor_translation_model == "gpt-4o-mini"


@pytest.mark.anyio
async def test_patch_room_settings_rejects_translation_model_for_wrong_provider():
    """Supplying both fields must not pair a provider with another provider's model."""

    from portal.database import create_room, get_session

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"translation_provider": "anthropic", "translation_model": "gpt-4o-mini"},
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "Invalid translation model" in response.json()["detail"]


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
            f"/api/v1/events/{event.slug}/rooms/eventyay-24/settings",
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

        ambiguous_update = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_a_id}/settings",
            json={"name": "Ambiguous Update"},
            headers=_auth(),
        )
        internal_update = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/internal/{room_a_id}/settings",
            json={"name": "Room A Internal Update"},
            headers=_auth(),
        )
        eventyay_update = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/eventyay/{room_a_id}/settings",
            json={"name": "Room B Eventyay Update"},
            headers=_auth(),
        )

    assert ambiguous_update.status_code == 409, ambiguous_update.text
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
async def test_patch_room_settings_rejects_meeting_url_runtime_changes():
    """Meeting URLs belong to the locked source workflow and never appear in this API response."""

    from portal.database import create_room, get_session
    from portal.models import Room

    event = await _seed()
    async with get_session() as s:
        room = await create_room(
            s, event_id=event.id, display_name="Main Hall", jitsi_url="https://meet.example.org/main"
        )
        room_id = room.id

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json={"jitsi_url": "https://169.254.169.254/main?jwt=meeting-secret"},
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "meeting-secret" not in response.text
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
            json={
                "enable_transcription": True,
                "transcription_provider": "deepgram",
                "transcription_model": "nova-2",
                "source_language": "en",
            },
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
            json={
                "enable_transcription": True,
                "transcription_provider": "deepgram",
                "transcription_model": "nova-2",
                "source_language": "en",
            },
            headers=_auth(),
        )
    assert response.status_code == 200, response.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [("floor_source", "program_ingest"), ("meeting_agent_enabled", True)],
)
async def test_patch_room_settings_rejects_runtime_source_controls(field, value):
    """Source transitions must use their lock-and-cleanup workflows, not generic settings."""

    from portal.database import create_room, get_session
    from portal.models import Room

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room_id = room.id

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json={field: value},
            headers=_auth(),
        )

    assert response.status_code == 422
    async with get_session() as s:
        stored = await s.get(Room, room_id)
        assert stored.floor_source == "jitsi_bot"
        assert stored.meeting_agent_enabled is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "enable_transcription": True,
                "transcription_provider": "none",
                "transcription_model": "none",
                "source_language": "en",
            },
            "provider 'none'",
        ),
        (
            {
                "enable_transcription": True,
                "transcription_provider": "local",
                "transcription_model": "tiny",
            },
            "source language",
        ),
    ],
)
async def test_patch_room_settings_rejects_unusable_transcription_state(payload, message):
    """An enabled transcription pipeline must have a real provider and source language."""

    from portal.database import create_room, get_session

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json=payload,
            headers=_auth(),
        )

    assert response.status_code == 422
    assert message in response.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_cannot_clear_language_while_transcription_is_enabled():
    """Clearing the source language must not leave an enabled pipeline that cannot start."""

    from portal.database import create_room, get_session
    from portal.models import Room

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room.floor_transcription_enabled = True
        room.floor_transcription_provider = "local"
        room.floor_transcription_model = "tiny"
        room.floor_language_code = "en"
        room_id = room.id

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json={"source_language": None},
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "source language" in response.json()["detail"]
    async with get_session() as s:
        assert (await s.get(Room, room_id)).floor_language_code == "en"


@pytest.mark.anyio
async def test_patch_room_settings_rejects_floor_as_source_language():
    """The reserved floor channel name is not an ISO room source language."""

    from portal.database import create_room, get_session

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"source_language": "floor"},
            headers=_auth(),
        )

    assert response.status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"enable_translation": True, "source_language": "en"}, "provider and model"),
        (
            {
                "enable_translation": True,
                "translation_provider": "openai",
                "translation_model": "gpt-4o-mini",
                "source_language": "en",
            },
            "API key",
        ),
    ],
)
async def test_patch_room_settings_rejects_unusable_translation_state(payload, message):
    """Enabled translation must have a model and any provider credentials it needs."""

    from portal.database import create_room, get_session

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json=payload,
            headers=_auth(),
        )

    assert response.status_code == 422
    assert message in response.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_requires_translation_target_language():
    """Enabled translation must have somewhere to deliver translated text."""

    from portal.crypto import encrypt_val
    from portal.database import create_room, get_session
    from portal.models import Event

    event = await _seed()
    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.encrypted_translation_openai_api_key = encrypt_val("translation-key")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={
                "enable_translation": True,
                "translation_provider": "openai",
                "translation_model": "gpt-4o-mini",
                "source_language": "en",
            },
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "target language" in response.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_requires_transcription_for_translation():
    """Floor translation depends on floor captions as its source text."""

    from portal.crypto import encrypt_val
    from portal.database import create_room, get_session
    from portal.models import Event, RoomTranslationLanguage

    event = await _seed()
    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.encrypted_translation_openai_api_key = encrypt_val("translation-key")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="fr", language_name="French"))

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={
                "enable_translation": True,
                "translation_provider": "openai",
                "translation_model": "gpt-4o-mini",
                "source_language": "en",
            },
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "floor transcription" in response.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_preserves_translation_model_language_guard(monkeypatch):
    """PATCH must use the same built-in-model language checks as room upserts."""

    from portal.config import settings
    from portal.database import create_room, get_session
    from portal.models import RoomTranslationLanguage

    monkeypatch.setattr(settings, "local_translation_models", "nllb-200-distilled-600M")
    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="az", language_name="Azerbaijani"))

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={
                "enable_translation": True,
                "translation_provider": "local",
                "translation_model": "nllb-200-distilled-600M",
                "source_language": "en",
            },
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "doesn't support Azerbaijani" in response.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_requires_available_tts_and_invalidates_cache():
    """TTS needs an available provider and successful changes evict its runtime cache."""

    import time

    from portal.crypto import encrypt_val
    from portal.database import create_room, get_session
    from portal.models import Event, RoomTranslationLanguage
    from portal.tts import worker as tts_worker

    event = await _seed()
    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.encrypted_translation_openai_api_key = encrypt_val("translation-key")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room.floor_language_code = "en"
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        room.floor_translation_provider = "openai"
        room.floor_translation_model = "gpt-4o-mini"
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="en", language_name="English"))
        room_id = room.id

    payload = {"floor_tts_enabled": True, "floor_tts_provider": "deepgram"}
    async with _client() as c:
        unavailable = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json=payload,
            headers=_auth(),
        )
    assert unavailable.status_code == 422
    assert "deepgram" in unavailable.json()["detail"].lower()

    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.encrypted_deepgram_api_key = "corrupt-ciphertext"

    async with _client() as c:
        corrupt = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json=payload,
            headers=_auth(),
        )
    assert corrupt.status_code == 422

    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.encrypted_deepgram_api_key = encrypt_val("deepgram-key")

    tts_worker._config_cache[room_id] = (time.monotonic() + 300, {"voice": "M1"})
    async with _client() as c:
        updated = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json=payload,
            headers=_auth(),
        )

    assert updated.status_code == 200, updated.text
    assert room_id not in tts_worker._config_cache

    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.encrypted_elevenlabs_api_key = encrypt_val("elevenlabs-key")
    async with _client() as c:
        elevenlabs = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/settings",
            json={"floor_tts_provider": "elevenlabs"},
            headers=_auth(),
        )
    assert elevenlabs.status_code == 200, elevenlabs.text


@pytest.mark.anyio
async def test_patch_room_settings_rejects_tts_when_saved_translation_has_no_key():
    """Enabling TTS must validate the translation pipeline that supplies its text."""

    from portal.crypto import encrypt_val
    from portal.database import create_room, get_session
    from portal.models import Event, RoomTranslationLanguage

    event = await _seed()
    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.encrypted_deepgram_api_key = encrypt_val("deepgram-key")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room.floor_language_code = "en"
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        room.floor_translation_provider = "openai"
        room.floor_translation_model = "gpt-4o-mini"
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="en", language_name="English"))

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"floor_tts_enabled": True, "floor_tts_provider": "deepgram"},
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "translation API key" in response.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_requires_ai_voice_target_for_tts():
    """Global TTS cannot be enabled when every target is text-only."""

    from portal.crypto import encrypt_val
    from portal.database import create_room, get_session
    from portal.models import Event, RoomTranslationLanguage

    event = await _seed()
    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.encrypted_translation_openai_api_key = encrypt_val("translation-key")
        stored_event.encrypted_deepgram_api_key = encrypt_val("deepgram-key")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room.floor_language_code = "en"
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        room.floor_translation_provider = "openai"
        room.floor_translation_model = "gpt-4o-mini"
        s.add(
            RoomTranslationLanguage(
                room_id=room.id,
                language_code="fr",
                language_name="French",
                tts_enabled=False,
            )
        )

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"floor_tts_enabled": True, "floor_tts_provider": "deepgram"},
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "AI voice target" in response.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_rejects_tts_provider_that_cannot_speak_target_language():
    """Voice providers must support every target configured for AI voice."""

    from portal.crypto import encrypt_val
    from portal.database import create_room, get_session
    from portal.models import Event, RoomTranslationLanguage

    event = await _seed()
    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.encrypted_translation_openai_api_key = encrypt_val("translation-key")
        stored_event.encrypted_deepgram_api_key = encrypt_val("deepgram-key")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room.floor_language_code = "en"
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        room.floor_translation_provider = "openai"
        room.floor_translation_model = "gpt-4o-mini"
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="th", language_name="Thai"))

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"floor_tts_enabled": True, "floor_tts_provider": "deepgram"},
            headers=_auth(),
        )

    assert response.status_code == 422
    assert "can't speak Thai" in response.json()["detail"]


@pytest.mark.anyio
async def test_patch_room_settings_rejects_cross_event_relay_parent():
    """A relay booth and the room containing it must both belong to the target event."""

    from portal.database import create_booth, create_event, create_room, get_session
    from portal.models import Room

    event = await _seed()
    async with get_session() as s:
        target = await create_room(s, event_id=event.id, display_name="Target")
        foreign_event = await create_event(s, slug="foreign", display_name="Foreign")
        foreign_room = await create_room(s, event_id=foreign_event.id, display_name="Foreign Room")
        rogue_booth = await create_booth(
            s,
            event_id=event.id,
            room_id=foreign_room.id,
            language_code="fr",
            language_name="French",
        )
        target_id = target.id
        rogue_booth_id = rogue_booth.id

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{target_id}/settings",
            json={"relay_booth_id": rogue_booth_id},
            headers=_auth(),
        )

    assert response.status_code == 404
    async with get_session() as s:
        assert (await s.get(Room, target_id)).relay_booth_id is None


@pytest.mark.anyio
async def test_patch_room_settings_accepts_supported_openai_transcription_model():
    """Room settings and the OpenAI provider must share one supported-model contract."""

    from portal.crypto import encrypt_val
    from portal.database import create_room, get_session
    from portal.models import Event

    event = await _seed()
    async with get_session() as s:
        stored_event = await s.get(Event, event.id)
        stored_event.transcription_api_enabled = True
        stored_event.encrypted_openai_api_key = encrypt_val("openai-key")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    async with _client() as c:
        response = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={
                "enable_transcription": True,
                "transcription_provider": "openai",
                "transcription_model": "gpt-4o-transcribe",
                "source_language": "en",
            },
            headers=_auth(),
        )

    assert response.status_code == 200, response.text


@pytest.mark.anyio
async def test_patch_room_settings_bounds_database_identifiers():
    """IDs outside the signed database integer range are rejected before reaching SQLAlchemy."""

    from portal.database import create_room, get_session

    event = await _seed()
    async with get_session() as s:
        room = await create_room(s, event_id=event.id, display_name="Main Hall")

    oversized = 2**31
    async with _client() as c:
        relay = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/settings",
            json={"relay_booth_id": oversized},
            headers=_auth(),
        )
        internal = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/internal/{oversized}/settings",
            json={"name": "Nope"},
            headers=_auth(),
        )
        legacy = await c.patch(
            f"/api/v1/events/{event.slug}/rooms/{oversized}/settings",
            json={"name": "Nope"},
            headers=_auth(),
        )

    assert relay.status_code == 422
    assert internal.status_code == 422
    assert legacy.status_code == 404


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

    Uses a temporary file-backed SQLite database in WAL mode to model a true concurrent race
    where a competing session inserts and commits a room with (event_id, eventyay_room_id)
    before the endpoint's pending insert flushes. The endpoint hits SQLite's real UNIQUE constraint,
    rolls back the savepoint, and recovers by re-querying and updating the winner row.
    """
    from sqlalchemy import event as sa_event
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from fastapi_app import app
    from portal.auth import hash_password
    from portal.database import create_event, create_user, get_db_session, set_event_membership
    from portal.models import Base, DeveloperAccount, OAuthClient, OAuthToken, Room
    from portal.routers.api_v1 import _get_event_for_request, _lock_event_for_request

    # 1. Isolated temporary file-backed SQLite database with WAL mode
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

    # In PostgreSQL, write_lock=True uses row-level with_for_update() which permits concurrent room inserts.
    # In SQLite, PR #860 serializes event mutations via update(Event), which locks the entire file.
    # We bypass SQLite's artificial table lock to exercise the real UNIQUE constraint and savepoint rollback.
    async def _unlocked_get_event(db, slug, *, write_lock=True):
        return await _get_event_for_request(db, slug, write_lock=False)

    async def _unlocked_lock_event(db, evt, *, write_lock=True):
        return await _lock_event_for_request(db, evt, write_lock=False)

    try:
        with (
            patch("sqlalchemy.ext.asyncio.AsyncSession.flush", new=_concurrent_insert_and_real_flush),
            patch("portal.routers.api_v1._get_event_for_request", new=_unlocked_get_event),
            patch("portal.routers.api_v1._lock_event_for_request", new=_unlocked_lock_event),
        ):
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


@pytest.mark.anyio
async def test_booth_endpoints_reject_invalid_language_code_with_400():
    """Invalid language_code in path parameters must return 400 Bad Request, not 500."""
    event = await _seed()

    async with _client() as c:
        # Create a room first
        put_room = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall"},
            headers=_auth(),
        )
        assert put_room.status_code == 200
        room_id = put_room.json()["room_id"]

        endpoints = [
            ("get", f"/api/v1/events/{event.slug}/rooms/{room_id}/booths/invalid_lang"),
            ("post", f"/api/v1/events/{event.slug}/rooms/{room_id}/booths/invalid_lang"),
            ("delete", f"/api/v1/events/{event.slug}/rooms/{room_id}/booths/invalid_lang"),
            ("post", f"/api/v1/events/{event.slug}/rooms/{room_id}/booths/invalid_lang/transcription/start"),
            ("post", f"/api/v1/events/{event.slug}/rooms/{room_id}/booths/invalid_lang/transcription/stop"),
            ("get", f"/api/v1/events/{event.slug}/rooms/{room_id}/booths/invalid_lang/transcripts/export"),
        ]

        for method, url in endpoints:
            req = getattr(c, method)
            res = await req(url, headers=_auth())
            assert res.status_code == 400, f"Expected 400 for {method.upper()} {url}, got {res.status_code}: {res.text}"
            assert "Language code must be exactly two lowercase ASCII letters" in res.json()["detail"]


@pytest.mark.anyio
async def test_booth_endpoints_preserve_cross_event_404_with_invalid_language_code():
    """A token scoped to Event A must receive 404 Event not found for Event B even with invalid language_code."""
    from portal.database import create_event, create_room, get_session

    await _seed()

    async with get_session() as s:
        event_b = await create_event(s, slug="othercon", display_name="OtherCon 2026")
        room_b = await create_room(s, event_id=event_b.id, display_name="Other Hall")

    async with _client() as c:
        endpoints = [
            ("get", f"/api/v1/events/{event_b.slug}/rooms/{room_b.id}/booths/invalid_lang"),
            ("post", f"/api/v1/events/{event_b.slug}/rooms/{room_b.id}/booths/invalid_lang"),
            ("delete", f"/api/v1/events/{event_b.slug}/rooms/{room_b.id}/booths/invalid_lang"),
            ("post", f"/api/v1/events/{event_b.slug}/rooms/{room_b.id}/booths/invalid_lang/transcription/start"),
            ("post", f"/api/v1/events/{event_b.slug}/rooms/{room_b.id}/booths/invalid_lang/transcription/stop"),
            ("get", f"/api/v1/events/{event_b.slug}/rooms/{room_b.id}/booths/invalid_lang/transcripts/export"),
        ]

        for method, url in endpoints:
            req = getattr(c, method)
            res = await req(url, headers=_auth())
            assert res.status_code == 404, f"Expected 404 for {method.upper()} {url}, got {res.status_code}: {res.text}"
            assert res.json()["detail"] == "Event not found"


@pytest.mark.anyio
async def test_booth_endpoints_normalise_language_code():
    """Uppercase or padded language codes are normalised before use downstream."""
    event = await _seed()

    async with _client() as c:
        put_room = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall"},
            headers=_auth(),
        )
        assert put_room.status_code == 200
        room_id = put_room.json()["room_id"]
        base = f"/api/v1/events/{event.slug}/rooms/{room_id}/booths"

        created = await c.post(f"{base}/EN", headers=_auth())
        assert created.status_code == 200, created.text
        assert created.json()["whip_path"] == f"{event.slug}/{room_id}/en"

        duplicate = await c.post(f"{base}/%20en%20", headers=_auth())
        assert duplicate.status_code == 409

        fetched = await c.get(f"{base}/En", headers=_auth())
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["language_code"] == "en"

        deleted = await c.delete(f"{base}/EN", headers=_auth())
        assert deleted.status_code == 200, deleted.text
        assert (await c.get(f"{base}/en", headers=_auth())).status_code == 404


@pytest.mark.anyio
async def test_create_booth_rejects_floor_language():
    """'floor' is reserved for the floor channel and cannot be created as a booth."""
    event = await _seed()

    async with _client() as c:
        put_room = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall"},
            headers=_auth(),
        )
        room_id = put_room.json()["room_id"]

        res = await c.post(
            f"/api/v1/events/{event.slug}/rooms/{room_id}/booths/FLOOR",
            headers=_auth(),
        )
        assert res.status_code == 400
        assert res.json()["detail"] == "'floor' is not a valid booth language for a room."
