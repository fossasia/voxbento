"""Regression tests for PATCH /api/v1/events/{event_slug}/api_keys (issue #572).

Covers encrypt-on-create, key rotation, and partial payloads that leave other
provider ciphertexts unchanged.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from portal.crypto import decrypt_val, encrypt_val
from portal.database import (
    configure,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
    set_event_membership,
    set_room_membership,
)
from portal.models import DeveloperAccount, Event, EventMembership, OAuthClient, OAuthToken, Room
from portal.roles import EVENT_OWNER, ROOM_COORDINATOR


@pytest.fixture(autouse=True)
async def setup_db():
    """Set up an isolated database for each versioned API test."""
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def event_api_access() -> tuple[str, int]:
    """Create an event-owner OAuth token with permission to update event keys."""
    access_token = "event-api-test-access-token"
    async with get_session() as session:
        event = await create_event(session, slug="api-key-test", display_name="API Key Test")
        user = await create_user(
            session,
            email="event-owner@example.com",
            display_name="Event Owner",
        )
        developer_account = DeveloperAccount(user_id=user.id, status="approved")
        session.add(developer_account)
        await session.flush()

        oauth_client = OAuthClient(
            developer_account_id=developer_account.id,
            client_id="event-api-test-client",
            name="Event API Test Client",
        )
        session.add(oauth_client)
        await session.flush()

        session.add(EventMembership(user_id=user.id, event_id=event.id, role="event_owner"))
        session.add(
            OAuthToken(
                client_id=oauth_client.id,
                user_id=user.id,
                event_id=event.id,
                scopes=["events:write"],
                access_token_hash=hashlib.sha256(access_token.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        await session.commit()

    return access_token, event.id


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_update_event_api_keys_encrypts_initial_value_and_rotates_existing_key(event_api_access):
    """A first API key write and a rotation both store decryptable ciphertext."""
    access_token, event_id = event_api_access
    headers = {"Authorization": f"Bearer {access_token}"}

    async with _client() as client:
        response = await client.patch(
            "/api/v1/events/api-key-test/api_keys",
            json={"openai_api_key": "initial-openai-key"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        initial_ciphertext = event.encrypted_openai_api_key
        assert initial_ciphertext is not None
        assert initial_ciphertext != "initial-openai-key"
        assert decrypt_val(initial_ciphertext) == "initial-openai-key"

    async with _client() as client:
        response = await client.patch(
            "/api/v1/events/api-key-test/api_keys",
            json={"openai_api_key": "rotated-openai-key"},
            headers=headers,
        )
    assert response.status_code == 200

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.encrypted_openai_api_key != initial_ciphertext
        assert decrypt_val(event.encrypted_openai_api_key) == "rotated-openai-key"


@pytest.mark.anyio
async def test_update_event_api_keys_only_changes_fields_sent_in_payload(event_api_access):
    """Updating OpenAI leaves other encrypted provider keys byte-for-byte unchanged."""
    access_token, event_id = event_api_access
    headers = {"Authorization": f"Bearer {access_token}"}

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        event.encrypted_openai_api_key = encrypt_val("old-openai-key")
        event.encrypted_deepgram_api_key = encrypt_val("old-deepgram-key")
        event.encrypted_gemini_api_key = encrypt_val("old-gemini-key")
        await session.commit()
        old_openai_ciphertext = event.encrypted_openai_api_key
        old_deepgram_ciphertext = event.encrypted_deepgram_api_key
        old_gemini_ciphertext = event.encrypted_gemini_api_key

    async with _client() as client:
        response = await client.patch(
            "/api/v1/events/api-key-test/api_keys",
            json={"openai_api_key": "new-openai-key"},
            headers=headers,
        )
    assert response.status_code == 200

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.encrypted_openai_api_key != old_openai_ciphertext
        assert decrypt_val(event.encrypted_openai_api_key) == "new-openai-key"
        assert event.encrypted_deepgram_api_key == old_deepgram_ciphertext
        assert decrypt_val(event.encrypted_deepgram_api_key) == "old-deepgram-key"
        assert event.encrypted_gemini_api_key == old_gemini_ciphertext
        assert decrypt_val(event.encrypted_gemini_api_key) == "old-gemini-key"


@pytest.mark.anyio
async def test_update_event_api_keys_sarvam_encrypts_preserves_and_clears(event_api_access):
    """PATCH /api_keys encrypts sarvam_api_key, preserves omitted fields, and clears on empty string."""
    access_token, event_id = event_api_access
    headers = {"Authorization": f"Bearer {access_token}"}

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        event.encrypted_deepgram_api_key = encrypt_val("keep-deepgram-key")
        await session.commit()
        deepgram_ciphertext = event.encrypted_deepgram_api_key

    async with _client() as client:
        set_resp = await client.patch(
            "/api/v1/events/api-key-test/api_keys",
            json={"sarvam_api_key": "sarvam-v1-key"},
            headers=headers,
        )
    assert set_resp.status_code == 200

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.encrypted_sarvam_api_key is not None
        assert event.encrypted_sarvam_api_key != "sarvam-v1-key"
        assert decrypt_val(event.encrypted_sarvam_api_key) == "sarvam-v1-key"
        assert event.encrypted_deepgram_api_key == deepgram_ciphertext
        sarvam_ciphertext = event.encrypted_sarvam_api_key

    # Omitting sarvam_api_key preserves it byte-for-byte.
    async with _client() as client:
        omit_resp = await client.patch(
            "/api/v1/events/api-key-test/api_keys",
            json={"openai_api_key": "other-key"},
            headers=headers,
        )
    assert omit_resp.status_code == 200

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.encrypted_sarvam_api_key == sarvam_ciphertext

    # Empty string clears sarvam_api_key without touching other keys.
    async with _client() as client:
        clear_resp = await client.patch(
            "/api/v1/events/api-key-test/api_keys",
            json={"sarvam_api_key": ""},
            headers=headers,
        )
    assert clear_resp.status_code == 200

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.encrypted_sarvam_api_key is None
        assert event.encrypted_deepgram_api_key == deepgram_ciphertext


@pytest.mark.anyio
async def test_update_event_api_keys_sarvam_strips_padded_and_clears_whitespace(event_api_access):
    """PATCH /api_keys strips padding before encrypting and treats whitespace-only as cleared."""
    access_token, event_id = event_api_access
    headers = {"Authorization": f"Bearer {access_token}"}

    async with _client() as client:
        padded_resp = await client.patch(
            "/api/v1/events/api-key-test/api_keys",
            json={"sarvam_api_key": "  sarvam-padded-key  "},
            headers=headers,
        )
    assert padded_resp.status_code == 200

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert decrypt_val(event.encrypted_sarvam_api_key) == "sarvam-padded-key"

    async with _client() as client:
        ws_resp = await client.patch(
            "/api/v1/events/api-key-test/api_keys",
            json={"sarvam_api_key": "   \t  "},
            headers=headers,
        )
    assert ws_resp.status_code == 200

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.encrypted_sarvam_api_key is None


@pytest.mark.anyio
async def test_upsert_room_rejects_sarvam_with_unsupported_source_language(event_api_access):
    """PUT /rooms rejects enabling or keeping Sarvam transcription with an unsupported source_language."""
    from sqlalchemy import select

    access_token, event_id = event_api_access
    async with get_session() as session:
        token = (await session.execute(select(OAuthToken))).scalar_one()
        token.scopes = ["events:write", "rooms:write"]
        await session.commit()

    headers = {"Authorization": f"Bearer {access_token}"}
    async with _client() as client:
        bad_create = await client.put(
            "/api/v1/events/api-key-test/rooms/ext-room-1",
            json={
                "name": "Sarvam Room",
                "enable_transcription": True,
                "transcription_provider": "sarvam",
                "source_language": "fr",
            },
            headers=headers,
        )
        ok_create = await client.put(
            "/api/v1/events/api-key-test/rooms/ext-room-1",
            json={
                "name": "Sarvam Room",
                "enable_transcription": True,
                "transcription_provider": "sarvam",
                "source_language": "hi",
            },
            headers=headers,
        )
        bad_update = await client.put(
            "/api/v1/events/api-key-test/rooms/ext-room-1",
            json={"source_language": "es"},
            headers=headers,
        )
        explicit_null_enabled_bad = await client.put(
            "/api/v1/events/api-key-test/rooms/ext-room-1",
            json={"enable_transcription": None, "source_language": "fr"},
            headers=headers,
        )
        explicit_null_new_room = await client.put(
            "/api/v1/events/api-key-test/rooms/ext-room-null-create",
            json={
                "name": "Disabled By Null Default",
                "enable_transcription": None,
                "transcription_provider": "sarvam",
                "source_language": "fr",
            },
            headers=headers,
        )
    assert bad_create.status_code == 422
    assert "Sarvam AI does not support transcription language 'fr'" in bad_create.json()["detail"]
    assert ok_create.status_code == 200
    assert bad_update.status_code == 422
    assert "Sarvam AI does not support transcription language 'es'" in bad_update.json()["detail"]
    assert explicit_null_enabled_bad.status_code == 422
    assert "Sarvam AI does not support transcription language 'fr'" in explicit_null_enabled_bad.json()["detail"]
    assert explicit_null_new_room.status_code == 200

    async with get_session() as session:
        room = (
            await session.execute(
                select(Room).where(Room.event_id == event_id, Room.eventyay_room_id == "ext-room-1")
            )
        ).scalar_one()
        assert room.floor_language_code == "hi"
        assert room.floor_transcription_provider == "sarvam"
        null_room = (
            await session.execute(
                select(Room).where(Room.event_id == event_id, Room.eventyay_room_id == "ext-room-null-create")
            )
        ).scalar_one()
        assert null_room.floor_transcription_enabled is False


@pytest.mark.anyio
async def test_app_creating_an_event_for_another_organizer_adds_only_the_organizer():
    """#757: the organizer becomes Event owner; the app's developer gets no automatic access.

    The developer used to be added with a "support" role, which the event-role
    check rejects, so the whole event creation failed.
    """
    from sqlalchemy import select

    access_token = "app-creates-event-token"
    async with get_session() as session:
        developer = await create_user(session, email="dev@example.com", display_name="Developer")
        organizer = await create_user(session, email="organizer@example.com", display_name="Organizer")
        account = DeveloperAccount(user_id=developer.id, status="approved")
        session.add(account)
        await session.flush()
        app_client = OAuthClient(developer_account_id=account.id, client_id="eventyay-plugin", name="Eventyay plugin")
        session.add(app_client)
        seed = await create_event(session, slug="organizer-seed", display_name="Seed")
        session.add(EventMembership(user_id=organizer.id, event_id=seed.id, role="event_owner"))
        await session.flush()
        session.add(
            OAuthToken(
                client_id=app_client.id,
                user_id=organizer.id,
                event_id=seed.id,
                scopes=["events:write"],
                access_token_hash=hashlib.sha256(access_token.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        developer_id, organizer_id = developer.id, organizer.id

    async with _client() as client:
        response = await client.post(
            "/api/v1/events/",
            json={"slug": "organizers-summit", "name": "Organizer's Summit"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    assert response.status_code == 201, response.text

    async with get_session() as session:
        event = (await session.execute(select(Event).where(Event.slug == "organizers-summit"))).scalar_one()
        members = (await session.execute(select(EventMembership).where(EventMembership.event_id == event.id))).scalars().all()
    assert {(m.user_id, m.role) for m in members} == {(organizer_id, "event_owner")}
    assert developer_id not in {m.user_id for m in members}


@pytest.fixture
async def rbac_test_setup():
    """Setup event, room, and a public OAuth client for RBAC token testing (#853)."""
    async with get_session() as session:
        event = await create_event(session, slug="rbac-event", display_name="RBAC Event")
        room = await create_room(session, event_id=event.id, display_name="RBAC Room")
        dev_user = await create_user(session, email="dev-rbac@example.com", display_name="Dev")
        dev_account = DeveloperAccount(user_id=dev_user.id, status="approved")
        session.add(dev_account)
        await session.flush()

        client = OAuthClient(
            developer_account_id=dev_account.id,
            client_id="rbac-public-client",
            name="RBAC Public Client",
            is_confidential=False,
        )
        session.add(client)
        await session.commit()
        return event, room, client


async def _issue_token(client: OAuthClient, user_id: int, event_id: int, scopes: list[str]) -> str:
    raw_token = f"token-for-user-{user_id}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    async with get_session() as session:
        session.add(
            OAuthToken(
                client_id=client.id,
                user_id=user_id,
                event_id=event_id,
                scopes=scopes,
                access_token_hash=token_hash,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        await session.commit()
    return raw_token


@pytest.mark.anyio
async def test_verify_token_rbac_inactive_admin_forbidden(rbac_test_setup):
    """Deactivated admin is rejected upfront with 403 detail='User account is inactive' (#853)."""
    event, room, client = rbac_test_setup
    async with get_session() as session:
        user = await create_user(
            session,
            email="inactive-admin@example.com",
            display_name="Inactive Admin",
            is_admin=True,
            is_active=False,
        )
        user_id = user.id

    token = await _issue_token(client, user_id, event.id, ["events:read"])
    async with _client() as http:
        resp = await http.get(f"/api/v1/events/{event.slug}/rooms", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "User account is inactive"


@pytest.mark.anyio
async def test_verify_token_rbac_inactive_event_owner_forbidden(rbac_test_setup):
    """Deactivated event owner cannot fall through to membership check (#853)."""
    event, room, client = rbac_test_setup
    async with get_session() as session:
        user = await create_user(
            session,
            email="inactive-owner@example.com",
            display_name="Inactive Owner",
            is_admin=False,
            is_active=False,
        )
        await set_event_membership(session, user_id=user.id, event_id=event.id, role=EVENT_OWNER)
        user_id = user.id

    token = await _issue_token(client, user_id, event.id, ["events:read"])
    async with _client() as http:
        resp = await http.get(f"/api/v1/events/{event.slug}/rooms", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "User account is inactive"


@pytest.mark.anyio
async def test_verify_token_rbac_inactive_room_coordinator_forbidden(rbac_test_setup):
    """Deactivated room coordinator is rejected upfront on room endpoints (#853)."""
    event, room, client = rbac_test_setup
    async with get_session() as session:
        user = await create_user(
            session,
            email="inactive-coord@example.com",
            display_name="Inactive Coordinator",
            is_admin=False,
            is_active=False,
        )
        await set_room_membership(session, user_id=user.id, room_id=room.id, role=ROOM_COORDINATOR)
        user_id = user.id

    token = await _issue_token(client, user_id, event.id, ["booths:read"])
    async with _client() as http:
        resp = await http.get(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/booths",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "User account is inactive"


@pytest.mark.anyio
async def test_verify_token_rbac_allows_active_admin_without_membership(rbac_test_setup):
    """Active admin bypasses event-level RBAC check and accesses the resource (#853)."""
    event, room, client = rbac_test_setup
    async with get_session() as session:
        user = await create_user(
            session,
            email="active-admin@example.com",
            display_name="Active Admin",
            is_admin=True,
            is_active=True,
        )
        user_id = user.id

    token = await _issue_token(client, user_id, event.id, ["events:read"])
    async with _client() as http:
        resp = await http.get(f"/api/v1/events/{event.slug}/rooms", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.anyio
async def test_verify_token_rbac_allows_active_event_owner_and_room_coordinator(rbac_test_setup):
    """Active event owners and room coordinators retain proper RBAC access (#853)."""
    event, room, client = rbac_test_setup
    async with get_session() as session:
        owner = await create_user(
            session,
            email="active-owner@example.com",
            display_name="Active Owner",
            is_admin=False,
            is_active=True,
        )
        await set_event_membership(session, user_id=owner.id, event_id=event.id, role=EVENT_OWNER)
        coord = await create_user(
            session,
            email="active-coord@example.com",
            display_name="Active Coordinator",
            is_admin=False,
            is_active=True,
        )
        await set_room_membership(session, user_id=coord.id, room_id=room.id, role=ROOM_COORDINATOR)
        owner_id, coord_id = owner.id, coord.id

    owner_token = await _issue_token(client, owner_id, event.id, ["events:read"])
    coord_token = await _issue_token(client, coord_id, event.id, ["booths:read"])

    async with _client() as http:
        owner_resp = await http.get(f"/api/v1/events/{event.slug}/rooms", headers={"Authorization": f"Bearer {owner_token}"})
        coord_resp = await http.get(
            f"/api/v1/events/{event.slug}/rooms/{room.id}/booths",
            headers={"Authorization": f"Bearer {coord_token}"},
        )

    assert owner_resp.status_code == 200
    assert isinstance(owner_resp.json(), list)
    assert coord_resp.status_code == 200
    assert isinstance(coord_resp.json(), list)


@pytest.mark.anyio
async def test_verify_token_rbac_rejects_active_user_without_access(rbac_test_setup):
    """Active user who has no membership receives 'User lost RBAC access to this resource' (#853)."""
    event, room, client = rbac_test_setup
    async with get_session() as session:
        user = await create_user(
            session,
            email="active-outsider@example.com",
            display_name="Active Outsider",
            is_admin=False,
            is_active=True,
        )
        user_id = user.id

    token = await _issue_token(client, user_id, event.id, ["events:read"])
    async with _client() as http:
        resp = await http.get(f"/api/v1/events/{event.slug}/rooms", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "User lost RBAC access to this resource"

