from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import hashlib
import hmac
import re
import urllib.parse

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


def _allow_form(client, event_id, challenge="challenge", user_id=None, event_binding=None, scope=None):
    """The consent form a browser posts, with the server-issued event binding (#925).

    ``event_binding`` overrides the correct MAC so a test can replay a tampered form.
    """
    from portal.routers.oauth import _event_binding_mac

    if event_binding is None and user_id is not None:
        event_binding = _event_binding_mac(event_id, client.client_id, str(user_id))
    return {
        "client_id": client.client_id,
        "redirect_uri": "http://localhost/callback",
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state123",
        "event_id": str(event_id),
        "event_binding": event_binding or "",
        "scope": scope if scope is not None else "events:write events:read",
        "action": "allow",
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
                data=_allow_form(client, event.id, user_id=setup_oauth_clients["user"].id),
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
                data=_allow_form(client, event.id, user_id=setup_oauth_clients["unrelated_user"].id),
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
                data=_allow_form(client, event.id, user_id=setup_oauth_clients["user"].id),
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
                data=_allow_form(client, event.id, user_id=setup_oauth_clients["unrelated_user"].id),
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
                data=_allow_form(
                    client, event.id, user_id=setup_oauth_clients["user"].id, scope="rooms:read"
                ),
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
            "event": event.slug,
        }

        async with _client() as c:
            # First exchange
            response1 = await c.post("/oauth/token", data=data)
            assert response1.status_code == 200

            # Second exchange (replay)
            response2 = await c.post("/oauth/token", data=data)
            assert response2.status_code == 400
            assert response2.json()["error"] == "invalid_grant"

    @pytest.mark.anyio
    async def test_unrelated_token_cannot_claim_ownerless_event(self, setup_oauth_clients):
        """
        Ensure a token scoped to Event A cannot claim ownership of an ownerless Event B.
        """
        from datetime import datetime, timedelta, timezone

        from portal.database import get_session
        from portal.routers.oauth import hash_token

        async with get_session() as session:
            # Create an ownerless event
            event_b = Event(slug="ownerless-event", display_name="Ownerless Event")
            session.add(event_b)
            await session.flush()

            event_a = setup_oauth_clients["event"]
            client = setup_oauth_clients["confidential_client"]
            user = setup_oauth_clients["user"]

            token_record = OAuthToken(
                client_id=client.id,
                user_id=user.id,
                event_id=event_a.id,
                scopes=["events:write"],
                access_token_hash=hash_token("test_access_token_events_write"),
                refresh_token_hash=hash_token("test_refresh_token_events_write"),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            session.add(token_record)
            await session.flush()
            session.expunge_all()
            await session.commit()

        headers = {"Authorization": "Bearer test_access_token_events_write"}
        async with _client() as c:
            response = await c.post(
                "/api/v1/events/",
                headers=headers,
                json={
                    "slug": "ownerless-event",
                    "name": "Hijacked Event",
                    "organizer_emails": ["attacker@example.com"],
                },
            )

        assert response.status_code == 403
        assert "Not authorized to manage memberships" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_admin_redirect_room_disambiguation(self, setup_oauth_clients):
        """
        Verify that room lookup by-slug resolves numeric room ID collisions properly.
        """
        from portal.database import get_session
        from portal.models import Room

        event = setup_oauth_clients["event"]
        async with get_session() as session:
            # Create Room A with id=10, eventyay_room_id="20"
            room_a = Room(id=10, event_id=event.id, display_name="Room A", eventyay_room_id="20")
            # Create Room B with id=20, eventyay_room_id="10"
            room_b = Room(id=20, event_id=event.id, display_name="Room B", eventyay_room_id="10")
            session.add(room_a)
            session.add(room_b)
            await session.flush()
            session.expunge_all()
            await session.commit()

        async with _client() as c:
            # 1. Unambiguous by-eventyay URL for eventyay_room_id="10" (which is Room B)
            res1 = await c.get(f"/workspace/events/by-slug/{event.slug}/rooms/by-eventyay/10")
            assert res1.status_code == 303
            assert f"/workspace/events/{event.id}/rooms/20/" in res1.headers["location"]

            # 2. Query param ?by=eventyay for eventyay_room_id="10" (Room B)
            res2 = await c.get(f"/workspace/events/by-slug/{event.slug}/rooms/10?by=eventyay")
            assert res2.status_code == 303
            assert f"/workspace/events/{event.id}/rooms/20/" in res2.headers["location"]

            # 3. Query param ?by=id for internal id=10 (Room A)
            res3 = await c.get(f"/workspace/events/by-slug/{event.slug}/rooms/10?by=id")
            assert res3.status_code == 303
            assert f"/workspace/events/{event.id}/rooms/10/" in res3.headers["location"]


# ── Clicking Allow gives no role; the token exchange does (fix for #740) ──


async def _role(user_id, event_id):
    from sqlalchemy import select

    from portal.database import get_session

    async with get_session() as session:
        membership = await session.scalar(
            select(EventMembership).where(EventMembership.user_id == user_id, EventMembership.event_id == event_id)
        )
        return membership.role if membership else None


@pytest.mark.anyio
@pytest.mark.parametrize("event_key", ["event", "ownerless_event"])
async def test_allow_alone_does_not_make_anyone_event_owner(setup_oauth_clients, event_key):
    """A signed-in stranger who opens the Allow page for someone else's event gains nothing."""
    from portal.auth import create_user_token

    stranger = setup_oauth_clients["unrelated_user"]
    event = setup_oauth_clients[event_key]
    cookies = {"user_token": create_user_token(user_id=stranger.id, email=stranger.email)}
    async with _client() as c:
        for key in ("confidential_client", "public_client"):
            await c.post(
                "/oauth/authorize",
                data=_allow_form(setup_oauth_clients[key], event.id, user_id=stranger.id),
                cookies=cookies,
            )
    assert await _role(stranger.id, event.id) is None


@pytest.mark.anyio
async def test_allow_alone_does_not_upgrade_an_existing_role(setup_oauth_clients):
    from portal.auth import create_user_token
    from portal.database import get_session

    stranger = setup_oauth_clients["unrelated_user"]
    event = setup_oauth_clients["event"]
    async with get_session() as session:
        session.add(EventMembership(user_id=stranger.id, event_id=event.id, role="interpreter"))
    cookies = {"user_token": create_user_token(user_id=stranger.id, email=stranger.email)}
    async with _client() as c:
        res = await c.post(
            "/oauth/authorize",
            data=_allow_form(setup_oauth_clients["confidential_client"], event.id, user_id=stranger.id),
            cookies=cookies,
        )
    assert res.status_code == 303
    assert await _role(stranger.id, event.id) == "interpreter"


@pytest.mark.anyio
async def test_token_exchange_with_the_client_secret_makes_the_organizer_owner(setup_oauth_clients):
    """The normal Eventyay flow still works: the server-side exchange gives ownership."""
    import base64
    import hashlib
    import urllib.parse

    from portal.auth import create_user_token

    organizer = setup_oauth_clients["unrelated_user"]
    event = setup_oauth_clients["ownerless_event"]
    client = setup_oauth_clients["confidential_client"]
    verifier = "organizer-verifier-organizer-verifier-0123"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    cookies = {"user_token": create_user_token(user_id=organizer.id, email=organizer.email)}
    async with _client() as c:
        allow = await c.post(
            "/oauth/authorize",
            data=_allow_form(client, event.id, challenge, user_id=organizer.id),
            cookies=cookies,
        )
        code = urllib.parse.parse_qs(urllib.parse.urlparse(allow.headers["location"]).query)["code"][0]
        assert await _role(organizer.id, event.id) is None

        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost/callback",
            "client_id": client.client_id,
            "code_verifier": verifier,
            "event": event.slug,
        }
        wrong_secret = await c.post("/oauth/token", data={**token_data, "client_secret": "wrong"})
        assert wrong_secret.status_code == 401
        assert await _role(organizer.id, event.id) is None

        ok = await c.post("/oauth/token", data={**token_data, "client_secret": "test_secret"})
    assert ok.status_code == 200
    assert await _role(organizer.id, event.id) == "event_owner"


# ── The client can bind the code to the event it started the flow for (#925) ──


async def _code_for(c, client, user, event_id):
    import base64
    import hashlib
    import urllib.parse

    from portal.auth import create_user_token

    verifier = "event-binding-verifier-event-binding-0123"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}
    allow = await c.post(
        "/oauth/authorize", data=_allow_form(client, event_id, challenge, user_id=user.id), cookies=cookies
    )
    code = urllib.parse.parse_qs(urllib.parse.urlparse(allow.headers["location"]).query)["code"][0]
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://localhost/callback",
        "client_id": client.client_id,
        "client_secret": "test_secret",
        "code_verifier": verifier,
    }


@pytest.mark.anyio
async def test_token_exchange_rejects_a_code_for_another_event(setup_oauth_clients):
    """Eventyay started the flow for the attacker's event; the browser swapped in the victim's."""
    attacker = setup_oauth_clients["unrelated_user"]
    own_event = setup_oauth_clients["event"]
    victim_event = setup_oauth_clients["ownerless_event"]
    client = setup_oauth_clients["confidential_client"]
    async with _client() as c:
        token_data = await _code_for(c, client, attacker, victim_event.id)
        res = await c.post("/oauth/token", data={**token_data, "event": own_event.slug})
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_grant"
    assert await _role(attacker.id, victim_event.id) is None


@pytest.mark.anyio
async def test_token_exchange_with_the_matching_event_returns_it(setup_oauth_clients):
    organizer = setup_oauth_clients["unrelated_user"]
    event = setup_oauth_clients["ownerless_event"]
    client = setup_oauth_clients["confidential_client"]
    async with _client() as c:
        token_data = await _code_for(c, client, organizer, event.id)
        res = await c.post("/oauth/token", data={**token_data, "event": event.slug})
        assert res.status_code == 200
        assert res.json()["event"] == event.slug
        refreshed = await c.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": res.json()["refresh_token"],
                "client_id": client.client_id,
                "client_secret": "test_secret",
            },
        )
    assert refreshed.status_code == 200
    assert refreshed.json()["event"] == event.slug
    assert await _role(organizer.id, event.id) == "event_owner"


@pytest.mark.anyio
async def test_confidential_token_exchange_without_event_is_refused(setup_oauth_clients):
    """A confidential client must name the event it started the flow for.

    Leaving ``event`` out was the remaining way to reach the hidden-field swap: the
    server had nothing to compare the code's event against, so it granted ownership.
    """
    organizer = setup_oauth_clients["unrelated_user"]
    event = setup_oauth_clients["ownerless_event"]
    client = setup_oauth_clients["confidential_client"]
    async with _client() as c:
        token_data = await _code_for(c, client, organizer, event.id)
        res = await c.post("/oauth/token", data=token_data)
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_request"
    assert await _role(organizer.id, event.id) is None


@pytest.mark.anyio
async def test_public_token_exchange_without_event_still_works(setup_oauth_clients):
    """Public clients grant no ownership, so they keep working without ``event``."""
    owner = setup_oauth_clients["user"]
    event = setup_oauth_clients["event"]
    client = setup_oauth_clients["public_client"]
    async with _client() as c:
        token_data = await _code_for(c, client, owner, event.id)
        token_data.pop("client_secret")
        res = await c.post("/oauth/token", data=token_data)
    assert res.status_code == 200
    assert res.json()["event"] == event.slug


# ── The consent form's event cannot be swapped in the browser (#925) ──


@pytest.mark.anyio
async def test_consent_rejects_a_swapped_event_id(setup_oauth_clients):
    """Replaying the victim's event_id with the MAC issued for the attacker's event fails."""
    from portal.auth import create_user_token
    from portal.routers.oauth import _event_binding_mac

    attacker = setup_oauth_clients["unrelated_user"]
    own_event = setup_oauth_clients["event"]
    victim_event = setup_oauth_clients["ownerless_event"]
    client = setup_oauth_clients["confidential_client"]
    cookies = {"user_token": create_user_token(user_id=attacker.id, email=attacker.email)}

    # The MAC the server issued for the attacker's own event, replayed against the victim's.
    stale_mac = _event_binding_mac(own_event.id, client.client_id, str(attacker.id))
    async with _client() as c:
        res = await c.post(
            "/oauth/authorize",
            data=_allow_form(client, victim_event.id, event_binding=stale_mac),
            cookies=cookies,
        )
    assert res.status_code == 400
    assert await _role(attacker.id, victim_event.id) is None


@pytest.mark.anyio
async def test_consent_rejects_a_missing_event_binding(setup_oauth_clients):
    from portal.auth import create_user_token

    attacker = setup_oauth_clients["unrelated_user"]
    victim_event = setup_oauth_clients["ownerless_event"]
    client = setup_oauth_clients["confidential_client"]
    cookies = {"user_token": create_user_token(user_id=attacker.id, email=attacker.email)}
    async with _client() as c:
        res = await c.post(
            "/oauth/authorize", data=_allow_form(client, victim_event.id), cookies=cookies
        )
    assert res.status_code == 400
    assert await _role(attacker.id, victim_event.id) is None


@pytest.mark.anyio
async def test_event_binding_mac_is_keyed_with_the_server_secret(monkeypatch):
    """Pin the MAC to an independently computed keyed vector.

    Every other binding test takes its MAC from ``_event_binding_mac`` itself, so
    swapping it for an unkeyed digest would keep them all passing while letting
    anyone forge a consent binding.
    """
    from portal.config import settings
    from portal.routers.oauth import _event_binding_mac

    monkeypatch.setattr(settings, "jwt_secret", "known-test-secret-for-binding-vector")
    message = b"7:some-client:42"

    expected = hmac.new(
        b"known-test-secret-for-binding-vector", message, hashlib.sha256
    ).hexdigest()
    produced = _event_binding_mac(7, "some-client", "42")

    assert produced == expected
    assert produced != hashlib.sha256(message).hexdigest(), "an unkeyed digest would be forgeable"


@pytest.mark.anyio
async def test_consent_rejects_an_unkeyed_digest_of_the_same_tuple(setup_oauth_clients):
    """The forgeable digest an attacker could compute without the secret is refused."""
    from portal.auth import create_user_token

    attacker = setup_oauth_clients["unrelated_user"]
    event = setup_oauth_clients["ownerless_event"]
    client = setup_oauth_clients["confidential_client"]

    forged = hashlib.sha256(f"{event.id}:{client.client_id}:{attacker.id}".encode()).hexdigest()
    cookies = {"user_token": create_user_token(user_id=attacker.id, email=attacker.email)}
    async with _client() as c:
        res = await c.post(
            "/oauth/authorize",
            data=_allow_form(client, event.id, event_binding=forged),
            cookies=cookies,
        )
    assert res.status_code == 400
    assert await _role(attacker.id, event.id) is None


@pytest.mark.anyio
async def test_consent_binding_is_bound_to_the_client(setup_oauth_clients):
    """A MAC minted for one client does not authorise consent through another."""
    from portal.auth import create_user_token
    from portal.routers.oauth import _event_binding_mac

    user = setup_oauth_clients["user"]
    event = setup_oauth_clients["event"]
    confidential = setup_oauth_clients["confidential_client"]
    public = setup_oauth_clients["public_client"]
    cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}

    mac_for_confidential = _event_binding_mac(event.id, confidential.client_id, str(user.id))
    async with _client() as c:
        res = await c.post(
            "/oauth/authorize",
            data=_allow_form(public, event.id, event_binding=mac_for_confidential),
            cookies=cookies,
        )
    assert res.status_code == 400


@pytest.mark.anyio
async def test_consent_binding_alone_does_not_stop_a_chosen_event(setup_oauth_clients):
    """The binding stops form tampering, not the attacker's choice of event.

    ``get_effective_scopes`` grants a confidential client every scope for any
    event, so an attacker can simply name the victim's event in the authorize
    URL and the server mints a valid binding for it. Nothing is tampered with
    here and consent still succeeds, which is why the token exchange has to
    check the event the client itself started the flow for.
    """
    from portal.auth import create_user_token

    attacker = setup_oauth_clients["unrelated_user"]
    victim_event = setup_oauth_clients["ownerless_event"]
    client = setup_oauth_clients["confidential_client"]
    cookies = {"user_token": create_user_token(user_id=attacker.id, email=attacker.email)}

    async with _client() as c:
        page = await c.get(
            "/oauth/authorize",
            params={
                "client_id": client.client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "state": "state123",
                "event": victim_event.slug,
                "scope": "events:write events:read",
            },
            cookies=cookies,
        )
        assert page.status_code == 200, "the server renders consent for an event the attacker does not own"
        mac = re.search(r'name="event_binding" value="([0-9a-f]{64})"', page.text).group(1)

        allow = await c.post(
            "/oauth/authorize",
            data=_allow_form(client, victim_event.id, event_binding=mac),
            cookies=cookies,
        )
        assert allow.status_code == 303, "a server-issued binding is valid, so consent succeeds"
        code = urllib.parse.parse_qs(urllib.parse.urlparse(allow.headers["location"]).query)["code"][0]

        # Eventyay started the flow for a different event, so it sends that one.
        res = await c.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://localhost/callback",
                "client_id": client.client_id,
                "client_secret": "test_secret",
                "code_verifier": "event-binding-verifier-event-binding-0123",
                "event": setup_oauth_clients["event"].slug,
            },
        )

    assert res.status_code == 400
    assert await _role(attacker.id, victim_event.id) is None, (
        "only the token-exchange event check stops this path"
    )


@pytest.mark.anyio
async def test_consent_binding_is_not_transferable_between_users(setup_oauth_clients):
    """A MAC minted for one signed-in user does not authorise another user's consent."""
    from portal.auth import create_user_token
    from portal.routers.oauth import _event_binding_mac

    owner = setup_oauth_clients["user"]
    attacker = setup_oauth_clients["unrelated_user"]
    event = setup_oauth_clients["ownerless_event"]
    client = setup_oauth_clients["confidential_client"]

    owners_mac = _event_binding_mac(event.id, client.client_id, str(owner.id))
    cookies = {"user_token": create_user_token(user_id=attacker.id, email=attacker.email)}
    async with _client() as c:
        res = await c.post(
            "/oauth/authorize",
            data=_allow_form(client, event.id, event_binding=owners_mac),
            cookies=cookies,
        )
    assert res.status_code == 400
    assert await _role(attacker.id, event.id) is None


@pytest.mark.anyio
async def test_authorize_get_redirects_an_unauthenticated_visitor_to_login(setup_oauth_clients):
    """No cookie must reach the binding, which dereferences user["sub"].

    ``authorize_get`` returns early when ``user`` is None, so the signed-out
    case is a redirect rather than a crash.
    """
    event = setup_oauth_clients["event"]
    client = setup_oauth_clients["confidential_client"]

    async with _client() as c:
        res = await c.get(
            "/oauth/authorize",
            params={
                "client_id": client.client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "state": "state123",
                "event": event.slug,
                "scope": "events:write events:read",
            },
        )

    assert res.status_code == 303
    assert res.headers["location"].startswith("/login?next=")


@pytest.mark.anyio
async def test_consent_page_issues_a_binding_the_post_accepts(setup_oauth_clients):
    """End to end: the MAC rendered into the real consent page is the one POST accepts."""
    from portal.auth import create_user_token

    owner = setup_oauth_clients["user"]
    event = setup_oauth_clients["event"]
    client = setup_oauth_clients["confidential_client"]
    cookies = {"user_token": create_user_token(user_id=owner.id, email=owner.email)}

    async with _client() as c:
        page = await c.get(
            "/oauth/authorize",
            params={
                "client_id": client.client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "state": "state123",
                "event": event.slug,
                "scope": "events:write events:read",
            },
            cookies=cookies,
        )
        assert page.status_code == 200
        match = re.search(r'name="event_binding" value="([0-9a-f]{64})"', page.text)
        assert match, "consent page must render an event_binding"

        res = await c.post(
            "/oauth/authorize",
            data=_allow_form(client, event.id, event_binding=match.group(1)),
            cookies=cookies,
        )
    assert res.status_code == 303


@pytest.mark.anyio
async def test_event_binding_mac_is_keyed_and_rejects_the_unkeyed_digest():
    """The binding is an HMAC under the configured secret (Sourcery #3).

    An unkeyed SHA-256 digest of the same tuple must not match, so an attacker
    who learns the tuple cannot forge a binding without the server's secret.
    """
    from portal.config import settings
    from portal.routers.oauth import _event_binding_mac

    event_id, client_id, user_sub = 7, "client_abc", "42"
    msg = f"{event_id}:{client_id}:{user_sub}".encode()

    produced = _event_binding_mac(event_id, client_id, user_sub)

    expected = hmac.new(
        settings.effective_jwt_secret.encode(), msg, hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(produced, expected)

    unkeyed = hashlib.sha256(msg).hexdigest()
    assert not hmac.compare_digest(produced, unkeyed)

    other_key = hmac.new(b"not-the-server-secret", msg, hashlib.sha256).hexdigest()
    assert not hmac.compare_digest(produced, other_key)
