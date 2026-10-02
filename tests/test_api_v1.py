"""Regression tests for PATCH /api/v1/events/{event_slug}/api_keys (issue #572).

Covers encrypt-on-create, key rotation, and partial payloads that leave other
provider ciphertexts unchanged.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from portal.crypto import decrypt_val, encrypt_val
from portal.database import configure, create_event, create_user, dispose, get_session, init_db
from portal.models import DeveloperAccount, Event, EventMembership, OAuthClient, OAuthToken


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
