"""Tests for the OAuth /oauth/token endpoint (PR #602).

Covers:
- Invalid client (nonexistent, inactive)
- Unsupported grant type
- Expired authorization code
- Successful authorization-code exchange (tokens, audit, code marked used, TTL, scopes)
- Refresh-token family isolation: reuse of a compromised token only revokes its
  own chain; an independent family for the same client/user/event survives.
- Rollback: a failure after the code is claimed must not leave the DB partially
  updated (code permanently used, orphaned token, orphaned audit row).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# A. Invalid client
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_token_exchange_nonexistent_client():
    """Requesting a token with a client_id that does not exist returns invalid_client."""
    async with _client() as http:
        response = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "does-not-exist",
                "code": "irrelevant",
                "redirect_uri": "https://example.com/cb",
                "code_verifier": "irrelevant",
            },
        )
    assert response.status_code == 400
    body = response.json()
    assert body.get("error") == "invalid_client"
    assert "traceback" not in str(body).lower()
    assert "sqlalchemy" not in str(body).lower()


@pytest.mark.anyio
async def test_token_exchange_inactive_client():
    """Requesting a token with an inactive client returns invalid_client."""
    from portal.database import create_user, get_session
    from portal.models import DeveloperAccount, OAuthClient

    async with get_session() as s:
        user = await create_user(s, email="dev2@example.com", display_name="Dev2")
        dev = DeveloperAccount(user_id=user.id, status="approved")
        s.add(dev)
        await s.flush()
        client = OAuthClient(
            developer_account_id=dev.id,
            client_id="inactive-client",
            name="Inactive",
            status="inactive",
            is_confidential=False,
            scopes_requested=["events:write"],
        )
        s.add(client)
        await s.commit()

    async with _client() as http:
        response = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "inactive-client",
                "code": "x",
                "redirect_uri": "https://example.com/cb",
                "code_verifier": "y",
            },
        )
    assert response.status_code == 400
    assert response.json().get("error") == "invalid_client"


# ---------------------------------------------------------------------------
# B. Unsupported grant type
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_token_exchange_unsupported_grant_type():
    """An unrecognised grant_type returns unsupported_grant_type."""
    from portal.database import create_user, get_session
    from portal.models import DeveloperAccount, OAuthClient

    async with get_session() as s:
        user = await create_user(s, email="dev3@example.com", display_name="Dev3")
        dev = DeveloperAccount(user_id=user.id, status="approved")
        s.add(dev)
        await s.flush()
        client = OAuthClient(
            developer_account_id=dev.id,
            client_id="grant-test-client",
            name="Grant Test",
            is_confidential=False,
            scopes_requested=["events:write"],
        )
        s.add(client)
        await s.commit()

    async with _client() as http:
        response = await http.post(
            "/oauth/token",
            data={
                "grant_type": "password",
                "client_id": "grant-test-client",
            },
        )
    assert response.status_code == 400
    assert response.json().get("error") == "unsupported_grant_type"


# ---------------------------------------------------------------------------
# C. Expired authorization code
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_token_exchange_expired_auth_code():
    """An expired authorization code is rejected with invalid_grant."""
    from portal.database import create_event, create_user, get_session
    from portal.models import DeveloperAccount, OAuthAuthorizationCode, OAuthClient

    async with get_session() as s:
        user = await create_user(s, email="exp@example.com", display_name="Exp")
        event = await create_event(s, slug="exp-event", display_name="Exp Event")
        dev = DeveloperAccount(user_id=user.id, status="approved")
        s.add(dev)
        await s.flush()
        client = OAuthClient(
            developer_account_id=dev.id,
            client_id="exp-client",
            name="Exp Client",
            is_confidential=False,
            scopes_requested=["events:write"],
        )
        s.add(client)
        await s.flush()

        raw_code = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        s.add(
            OAuthAuthorizationCode(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                scopes=["events:write"],
                code_hash=_hash(raw_code),
                redirect_uri="https://example.com/cb",
                code_challenge=challenge,
                code_challenge_method="S256",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                used=False,
            )
        )
        await s.commit()
        client_id_str = client.client_id

    async with _client() as http:
        response = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id_str,
                "code": raw_code,
                "redirect_uri": "https://example.com/cb",
                "code_verifier": verifier,
            },
        )
    assert response.status_code == 400
    body = response.json()
    assert body.get("error") == "invalid_grant"
    assert "traceback" not in str(body).lower()


# ---------------------------------------------------------------------------
# D. Successful authorization-code exchange
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_token_exchange_success_authorization_code():
    """
    A valid authorization-code exchange:
    - returns access_token, refresh_token, token_type=Bearer, expires_in=3600
    - scope matches what was stored on the auth code
    - OAuthToken record is created and not revoked
    - Authorization code is marked used
    - OAuthAuditLog record with action=token_exchange_code is created
    """
    from sqlalchemy import select

    from portal.database import create_event, create_user, get_session
    from portal.models import (
        DeveloperAccount,
        OAuthAuditLog,
        OAuthAuthorizationCode,
        OAuthClient,
        OAuthToken,
    )

    async with get_session() as s:
        user = await create_user(s, email="success@example.com", display_name="Success")
        event = await create_event(s, slug="success-event", display_name="Success Event")
        dev = DeveloperAccount(user_id=user.id, status="approved")
        s.add(dev)
        await s.flush()
        client = OAuthClient(
            developer_account_id=dev.id,
            client_id="success-client",
            name="Success Client",
            is_confidential=False,
            scopes_requested=["events:write", "events:read"],
        )
        s.add(client)
        await s.flush()

        raw_code = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        code_scopes = ["events:write"]
        s.add(
            OAuthAuthorizationCode(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                scopes=code_scopes,
                code_hash=_hash(raw_code),
                redirect_uri="https://example.com/cb",
                code_challenge=challenge,
                code_challenge_method="S256",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                used=False,
            )
        )
        await s.commit()
        client_id_str = client.client_id
        client_db_id = client.id
        user_id = user.id
        event_id = event.id

    async with _client() as http:
        response = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id_str,
                "code": raw_code,
                "redirect_uri": "https://example.com/cb",
                "code_verifier": verifier,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()

    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert set(body["scope"].split(" ")) == set(code_scopes)

    async with get_session() as s:
        code_res = await s.execute(
            select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code_hash == _hash(raw_code))
        )
        auth_code = code_res.scalars().first()
        assert auth_code is not None
        assert auth_code.used is True

        token_res = await s.execute(
            select(OAuthToken).where(OAuthToken.access_token_hash == _hash(body["access_token"]))
        )
        token = token_res.scalars().first()
        assert token is not None
        assert token.revoked is False
        assert token.client_id == client_db_id
        assert token.user_id == user_id
        assert token.event_id == event_id
        assert set(token.scopes) == set(code_scopes)

        audit_res = await s.execute(
            select(OAuthAuditLog).where(
                OAuthAuditLog.token_id == token.id,
                OAuthAuditLog.action == "token_exchange_code",
            )
        )
        audit = audit_res.scalars().first()
        assert audit is not None
        assert audit.status_code == 200


# ---------------------------------------------------------------------------
# E. Refresh-token family isolation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_refresh_token_reuse_only_revokes_compromised_family():
    """
    Two independent refresh-token families share the same client/user/event.

    Family A: A1 -> A2 (already revoked) -> A3 (current)
    Family B: B1 -> B2 (current)

    Replaying A2's refresh token triggers reuse detection.
    Only Family A must be revoked; Family B must remain valid.
    """
    from sqlalchemy import select

    from portal.database import create_event, create_user, get_session
    from portal.models import DeveloperAccount, OAuthClient, OAuthToken

    async with get_session() as s:
        user = await create_user(s, email="family@example.com", display_name="Family")
        event = await create_event(s, slug="family-event", display_name="Family Event")
        dev = DeveloperAccount(user_id=user.id, status="approved")
        s.add(dev)
        await s.flush()
        client = OAuthClient(
            developer_account_id=dev.id,
            client_id="family-client",
            name="Family Client",
            is_confidential=False,
            scopes_requested=["events:write"],
        )
        s.add(client)
        await s.flush()

        def _make_token(parent_id=None, revoked=False):
            raw_access = secrets.token_urlsafe(32)
            raw_refresh = secrets.token_urlsafe(32)
            tok = OAuthToken(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                scopes=["events:write"],
                access_token_hash=_hash(raw_access),
                refresh_token_hash=_hash(raw_refresh),
                parent_token_id=parent_id,
                revoked=revoked,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            return tok, raw_refresh

        # Family A: A1 -> A2 (revoked) -> A3
        A1, _ = _make_token()
        s.add(A1)
        await s.flush()
        A2, _ = _make_token(parent_id=A1.id, revoked=True)
        s.add(A2)
        await s.flush()
        A3, _ = _make_token(parent_id=A2.id)
        s.add(A3)
        await s.flush()

        # Store a known refresh token on A2 so we can replay it
        a2_raw_refresh = secrets.token_urlsafe(32)
        A2.refresh_token_hash = _hash(a2_raw_refresh)

        # Family B: B1 -> B2
        B1, _ = _make_token()
        s.add(B1)
        await s.flush()
        B2, _ = _make_token(parent_id=B1.id)
        s.add(B2)
        await s.flush()

        client_id_str = client.client_id
        a1_id, a2_id, a3_id = A1.id, A2.id, A3.id
        b1_id, b2_id = B1.id, B2.id
        await s.commit()

    # Replay A2's refresh token (already revoked — this triggers reuse detection)
    async with _client() as http:
        response = await http.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id_str,
                "refresh_token": a2_raw_refresh,
            },
        )

    assert response.status_code == 400
    body = response.json()
    assert body.get("error") == "invalid_grant"
    assert "reuse" in body.get("error_description", "").lower()

    async with get_session() as s:
        a1 = await s.get(OAuthToken, a1_id)
        a2 = await s.get(OAuthToken, a2_id)
        a3 = await s.get(OAuthToken, a3_id)
        b1 = await s.get(OAuthToken, b1_id)
        b2 = await s.get(OAuthToken, b2_id)

        # Entire Family A revoked
        assert a1.revoked is True, "A1 (root) must be revoked"
        assert a2.revoked is True, "A2 was already revoked"
        assert a3.revoked is True, "A3 (live token) must be revoked"

        # Family B untouched
        assert b1.revoked is False, "B1 must NOT be revoked"
        assert b2.revoked is False, "B2 must NOT be revoked"


# ---------------------------------------------------------------------------
# F. Rollback: partial failure after code claim
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_token_exchange_rollback_on_failure():
    """
    If token creation raises after the authorization code is atomically claimed,
    the transaction rolls back and the code is not permanently locked out.
    """
    from unittest.mock import patch

    from sqlalchemy import select

    from portal.database import create_event, create_user, get_session
    from portal.models import (
        DeveloperAccount,
        OAuthAuditLog,
        OAuthAuthorizationCode,
        OAuthClient,
        OAuthToken,
    )

    async with get_session() as s:
        user = await create_user(s, email="rollback@example.com", display_name="Rollback")
        event = await create_event(s, slug="rollback-event", display_name="Rollback Event")
        dev = DeveloperAccount(user_id=user.id, status="approved")
        s.add(dev)
        await s.flush()
        client = OAuthClient(
            developer_account_id=dev.id,
            client_id="rollback-client",
            name="Rollback Client",
            is_confidential=False,
            scopes_requested=["events:write"],
        )
        s.add(client)
        await s.flush()

        raw_code = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        s.add(
            OAuthAuthorizationCode(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                scopes=["events:write"],
                code_hash=_hash(raw_code),
                redirect_uri="https://example.com/cb",
                code_challenge=challenge,
                code_challenge_method="S256",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                used=False,
            )
        )
        await s.commit()
        client_id_str = client.client_id
        client_db_id = client.id

    # Patch OAuthToken.__init__ to raise, simulating a crash during token creation
    def broken_init(self, *args, **kwargs):
        raise RuntimeError("Simulated DB failure during token creation")

    with patch.object(OAuthToken, "__init__", broken_init):
        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as http:
            response = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id_str,
                    "code": raw_code,
                    "redirect_uri": "https://example.com/cb",
                    "code_verifier": verifier,
                },
            )

    # Endpoint must not return 200
    assert response.status_code in (400, 500)

    # The rolled-back transaction must not have permanently consumed the code
    async with get_session() as s:
        code_res = await s.execute(
            select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code_hash == _hash(raw_code))
        )
        auth_code = code_res.scalars().first()
        assert auth_code is not None
        assert auth_code.used is False, (
            "Authorization code must not be permanently marked used after a rolled-back transaction"
        )

        # No orphaned OAuthToken records
        token_res = await s.execute(
            select(OAuthToken).where(OAuthToken.client_id == client_db_id)
        )
        assert token_res.scalars().first() is None, "No orphaned token records should exist"

        # No orphaned OAuthAuditLog records from the failed transaction
        audit_res = await s.execute(
            select(OAuthAuditLog).where(OAuthAuditLog.client_id == client_db_id)
        )
        assert audit_res.scalars().first() is None, "No orphaned audit log records should exist"
