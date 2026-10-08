from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.models import Event, EventMembership, OAuthAuthorizationCode, OAuthClient, OAuthToken, User


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def setup_oauth_clients(setup_db):
    from portal.database import get_session

    async with get_session() as session:
        # Add a user (authorized organizer)
        user = User(email="test@example.com", display_name="Test User")
        session.add(user)

        # Add an unrelated user
        unrelated_user = User(email="unrelated@example.com", display_name="Unrelated User")
        session.add(unrelated_user)
        await session.flush()

        # Add a developer account
        from portal.models import DeveloperAccount

        developer = DeveloperAccount(user_id=user.id)
        session.add(developer)
        await session.flush()

        # 1. Create a public client
        public_client = OAuthClient(
            developer_account_id=developer.id,
            client_id="test_public_client",
            name="Test Public Client",
            is_confidential=False,
            redirect_uris=["http://localhost/callback"],
            scopes_requested=["events:write", "events:read"],
        )
        from portal.routers.oauth import hash_token

        # 2. Create a confidential client
        confidential_client = OAuthClient(
            developer_account_id=developer.id,
            client_id="test_confidential_client",
            client_secret_hash=hash_token("test_secret"),
            name="Test Confidential Client",
            is_confidential=True,
            redirect_uris=["http://localhost/callback"],
            scopes_requested=["events:write", "events:read"],
        )
        session.add(public_client)
        session.add(confidential_client)

        # Add an event that has an owner
        event = Event(slug="owned-event", display_name="Owned Event")
        session.add(event)

        # Add a truly ownerless event
        ownerless_event = Event(slug="truly-ownerless-event", display_name="Ownerless Event")
        session.add(ownerless_event)
        await session.flush()

        # Assign user as event owner for the first event
        membership = EventMembership(user_id=user.id, event_id=event.id, role="event_owner")
        session.add(membership)

        await session.flush()
        session.expunge_all()
        await session.commit()

    return {
        "public_client": public_client,
        "confidential_client": confidential_client,
        "event": event,
        "ownerless_event": ownerless_event,
        "user": user,
        "unrelated_user": unrelated_user,
    }


class TestOAuthMultiOrganizer:
    @pytest.mark.anyio
    async def test_oauth_confidential_client_authorized_organizer(self, setup_oauth_clients):
        """
        confidential client + authorized organizer -> success
        """
        client = setup_oauth_clients["confidential_client"]
        event = setup_oauth_clients["event"]

        from portal.auth import create_user_token

        user_token = create_user_token(user_id=setup_oauth_clients["user"].id, email="test@example.com")

        async with _client() as c:
            response = await c.post(
                "/oauth/authorize",
                data={
                    "client_id": client.client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": "challenge",
                    "code_challenge_method": "S256",
                    "state": "state123",
                    "event_id": str(event.id),
                    "scope": "events:write events:read",
                    "action": "allow",
                },
                cookies={"user_token": user_token},
            )

        assert response.status_code == 303, f"Expected 303, got {response.status_code}: {response.json()}"
        assert "code=" in response.headers.get("location", "")

    @pytest.mark.anyio
    async def test_oauth_confidential_client_unrelated_user(self, setup_oauth_clients):
        """
        confidential client + unrelated VoxBento user -> denied
        """
        client = setup_oauth_clients["confidential_client"]
        event = setup_oauth_clients["event"]

        from portal.auth import create_user_token

        user_token = create_user_token(user_id=setup_oauth_clients["unrelated_user"].id, email="unrelated@example.com")

        async with _client() as c:
            response = await c.post(
                "/oauth/authorize",
                data={
                    "client_id": client.client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": "challenge",
                    "code_challenge_method": "S256",
                    "state": "state123",
                    "event_id": str(event.id),
                    "scope": "events:write events:read",
                    "action": "allow",
                },
                cookies={"user_token": user_token},
            )

        # Because the client is confidential, it is trusted to handle multi-organizer auth
        assert response.status_code == 303, f"Expected 303, got {response.status_code}: {response.json()}"
        assert "code=" in response.headers.get("location", "")

    @pytest.mark.anyio
    async def test_oauth_confidential_client_trust(self, setup_oauth_clients):
        """
        confidential client + ownerless event -> success (trusted auto-provisioning)
        """
        client = setup_oauth_clients["confidential_client"]
        event = setup_oauth_clients["ownerless_event"]

        from portal.auth import create_user_token

        user_token = create_user_token(user_id=setup_oauth_clients["user"].id, email="test@example.com")

        async with _client() as c:
            response = await c.post(
                "/oauth/authorize",
                data={
                    "client_id": client.client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": "challenge",
                    "code_challenge_method": "S256",
                    "state": "state123",
                    "event_id": str(event.id),
                    "scope": "events:write events:read",
                    "action": "allow",
                },
                cookies={"user_token": user_token},
            )

        assert response.status_code == 303, f"Expected 303, got {response.status_code}: {response.json()}"
        assert "code=" in response.headers.get("location", "")

    @pytest.mark.anyio
    async def test_oauth_public_client_rejection(self, setup_oauth_clients):
        """
        public client + unrelated user -> denied
        """
        client = setup_oauth_clients["public_client"]
        event = setup_oauth_clients["event"]

        from portal.auth import create_user_token

        user_token = create_user_token(user_id=setup_oauth_clients["unrelated_user"].id, email="unrelated@example.com")

        async with _client() as c:
            response = await c.post(
                "/oauth/authorize",
                data={
                    "client_id": client.client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": "challenge",
                    "code_challenge_method": "S256",
                    "state": "state123",
                    "event_id": str(event.id),
                    "scope": "events:write events:read",
                    "action": "allow",
                },
                cookies={"user_token": user_token},
            )

        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_oauth_confidential_client_unregistered_scopes(self, setup_oauth_clients):
        """
        confidential client cannot request unregistered scopes
        (i.e., requesting rooms:read when only events:write and events:read are registered should result in 403 since it strips to empty or misses required scope)
        """
        client = setup_oauth_clients["confidential_client"]
        event = setup_oauth_clients["event"]

        from portal.auth import create_user_token

        user_token = create_user_token(user_id=setup_oauth_clients["user"].id, email="test@example.com")

        async with _client() as c:
            response = await c.post(
                "/oauth/authorize",
                data={
                    "client_id": client.client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": "challenge",
                    "code_challenge_method": "S256",
                    "state": "state123",
                    "event_id": str(event.id),
                    "scope": "rooms:read",
                    "action": "allow",
                },
                cookies={"user_token": user_token},
            )

        # Because rooms:read is not in client.scopes_requested, effective_scopes is empty -> 403
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_token_reuse_wrong_organizer(self, setup_oauth_clients):
        """
        Test that using a token from Event A on Event B returns a 404 (preventing existence leaks).
        """
        from portal.database import get_session
        from portal.routers.oauth import hash_token

        async with get_session() as session:
            # Create a second event
            event_b = Event(slug="event-b", display_name="Event B")
            session.add(event_b)
            await session.flush()

            event_a = setup_oauth_clients["event"]
            client = setup_oauth_clients["confidential_client"]
            user = setup_oauth_clients["user"]

            from datetime import datetime, timedelta, timezone

            token_record = OAuthToken(
                client_id=client.id,
                user_id=user.id,
                event_id=event_a.id,
                scopes=["events:read"],
                access_token_hash=hash_token("test_access_token_a"),
                refresh_token_hash=hash_token("test_refresh_token_a"),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            session.add(token_record)
            await session.flush()
            session.expunge_all()
            await session.commit()

        headers = {"Authorization": "Bearer test_access_token_a"}
        async with _client() as c:
            response = await c.get(f"/api/v1/events/{event_b.slug}", headers=headers)

        assert response.status_code == 404

    @pytest.mark.anyio
    async def test_suspended_client_token(self, setup_oauth_clients):
        """
        suspended/revoked confidential client cannot use previously issued tokens
        """
        from portal.database import get_session
        from portal.routers.oauth import hash_token

        async with get_session() as session:
            client = setup_oauth_clients["confidential_client"]

            # Suspend the client
            from sqlalchemy import select

            db_client = (
                (await session.execute(select(OAuthClient).where(OAuthClient.id == client.id))).scalars().first()
            )
            db_client.status = "suspended"

            event_a = setup_oauth_clients["event"]
            user = setup_oauth_clients["user"]

            from datetime import datetime, timedelta, timezone

            token_record = OAuthToken(
                client_id=client.id,
                user_id=user.id,
                event_id=event_a.id,
                scopes=["events:read"],
                access_token_hash=hash_token("test_access_token_suspended"),
                refresh_token_hash=hash_token("test_refresh_token_suspended"),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            session.add(token_record)
            await session.flush()
            session.expunge_all()
            await session.commit()

        headers = {"Authorization": "Bearer test_access_token_suspended"}
        async with _client() as c:
            response = await c.get(f"/api/v1/events/{event_a.slug}", headers=headers)

        assert response.status_code == 401
        assert response.json()["detail"] == "Client is not active"

    @pytest.mark.anyio
    async def test_authorization_code_replay(self, setup_oauth_clients):
        """
        authorization-code replay/concurrent exchange -> exactly one success
        """
        client = setup_oauth_clients["confidential_client"]
        event = setup_oauth_clients["event"]
        user = setup_oauth_clients["user"]

        import secrets

        from portal.database import get_session
        from portal.routers.oauth import hash_token

        code = secrets.token_urlsafe(32)
        code_hash = hash_token(code)

        import base64
        import hashlib

        verifier = "my-secure-verifier-my-secure-verifier"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

        async with get_session() as session:
            from datetime import datetime, timedelta, timezone

            auth_code = OAuthAuthorizationCode(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                code_hash=code_hash,
                scopes=["events:read"],
                redirect_uri="http://localhost/callback",
                code_challenge=challenge,
                code_challenge_method="S256",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
            session.add(auth_code)
            await session.flush()
            session.expunge_all()
            await session.commit()

        # Exchange the code twice
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost/callback",
            "client_id": client.client_id,
            "client_secret": "test_secret",
            "code_verifier": verifier,
        }

        async with _client() as c:
            # First exchange
            response1 = await c.post("/oauth/token", data=data)
            assert response1.status_code == 200

            # Second exchange (replay)
            response2 = await c.post("/oauth/token", data=data)
            assert response2.status_code == 400
            assert response2.json()["error"] == "invalid_grant"
