"""Regression tests for GET /api/v1/events/{slug} — Issue #619: AttributeError owner_id.

Covers the owner resolution fix plus the authorization policy the endpoint
relies on (``docs/threat_model.md`` §3):

owner resolution
- 200 with owner_id resolved from EventMembership (``Event`` has no owner_id column).
- owner_id is null when no event_owner membership exists.
- Lowest user_id wins deterministically when several event_owner rows exist.

resource visibility — unknown, soft-deleted and wrong-event all return the
same 404 so a token for one event cannot enumerate others
- Unknown slug → 404.
- Soft-deleted event → 404, byte-identical to the unknown-slug response.
- Wrong-event token on an existing event → 404, byte-identical to unknown slug.
- Unknown slug queried with a token scoped to a different event → still 404.

authentication / account state
- Missing bearer token → 401.
- Token correctly scoped but user holds no membership → 403 (the only 403 path).
- Previously valid token stops working once its user is deactivated → 403.

confidential clients (pre-existing behavior from PR #624, pinned here)
- Confidential clients bypass membership RBAC for their own event.
- The bypass does NOT cross event scope: wrong-event token is still 404.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("BOOTH_ACCESS_TOKEN", "")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_event(slug: str, display_name: str):
    from portal.database import create_event, get_session

    async with get_session() as s:
        event = await create_event(s, slug=slug, display_name=display_name)
    return event


async def _make_user(email: str, is_admin: bool = False):
    from portal.auth import hash_password
    from portal.database import create_user, get_session

    async with get_session() as s:
        user = await create_user(
            s,
            email=email,
            display_name="Test User",
            password_hash=hash_password("pass"),
            email_verified=True,
        )
        if is_admin:
            user.is_admin = True
    return user


async def _make_event_membership(user_id: int, event_id: int, role: str):
    from portal.database import get_session, set_event_membership

    async with get_session() as s:
        membership = await set_event_membership(s, user_id=user_id, event_id=event_id, role=role)
    return membership


async def _deactivate_user(user_id: int) -> None:
    from portal.database import get_session, update_user_active

    async with get_session() as s:
        await update_user_active(s, user_id, is_active=False)


async def _make_oauth_token(
    user_id: int,
    event_id: int,
    scopes: list[str],
    *,
    is_confidential: bool = False,
) -> str:
    """Insert a raw OAuthToken into the DB and return the raw (unhashed) token string.

    Defaults to a **public** client so ``_verify_token_rbac`` actually runs its
    membership checks. Confidential clients short-circuit RBAC by design
    (``docs/threat_model.md`` §2), so a confidential default would make the
    membership assertions below pass vacuously.
    """
    from portal.database import get_session
    from portal.models import DeveloperAccount, OAuthClient, OAuthToken

    raw_token = secrets.token_hex(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    async with get_session() as s:
        dev = DeveloperAccount(user_id=user_id, status="approved")
        s.add(dev)
        await s.flush()

        client = OAuthClient(
            developer_account_id=dev.id,
            client_id=secrets.token_hex(8),
            name="Test Client",
            redirect_uris=["https://example.com/callback"],
            scopes_requested=scopes,
            is_confidential=is_confidential,
            client_secret_hash=hashlib.sha256(b"secret").hexdigest() if is_confidential else None,
            status="active",
        )
        s.add(client)
        await s.flush()

        token = OAuthToken(
            client_id=client.id,
            user_id=user_id,
            event_id=event_id,
            scopes=scopes,
            access_token_hash=token_hash,
            expires_at=expires_at,
            revoked=False,
        )
        s.add(token)
        await s.commit()

    return raw_token


async def _get_event(slug: str, raw_token: str | None):
    headers = {"Authorization": f"Bearer {raw_token}"} if raw_token else {}
    async with _client() as client:
        return await client.get(f"/api/v1/events/{slug}", headers=headers)


# ---------------------------------------------------------------------------
# Owner resolution — issue #619
# ---------------------------------------------------------------------------


class TestGetEventOwnerResolution:
    @pytest.mark.anyio
    async def test_returns_owner_id_from_membership(self, setup_db):
        """owner_id must be resolved from EventMembership, not a (nonexistent) event.owner_id."""
        event = await _make_event("fossasia-2026", "FOSSASIA 2026")
        user = await _make_user("owner@example.com")
        await _make_event_membership(user.id, event.id, "event_owner")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"])

        resp = await _get_event(event.slug, raw_token)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == event.id
        assert data["slug"] == event.slug
        assert data["display_name"] == event.display_name
        assert data["owner_id"] == user.id
        assert "created_at" in data

    @pytest.mark.anyio
    async def test_owner_id_is_none_when_no_owner_membership(self, setup_db):
        """An event with no event_owner row must report owner_id as null, not 500.

        Uses a confidential client because that is the documented actor for
        ownerless events (``docs/threat_model.md`` §2 — auto-provisioning);
        a public client has no membership here and would correctly get 403.
        """
        event = await _make_event("no-owner-event", "No Owner Event")
        user = await _make_user("provisioner@example.com")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"], is_confidential=True)

        resp = await _get_event(event.slug, raw_token)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == event.id
        assert data["slug"] == event.slug
        assert data["owner_id"] is None

    @pytest.mark.anyio
    async def test_owner_id_is_deterministic_with_multiple_owners(self, setup_db):
        """With several event_owner rows the lowest user_id must win, every time."""
        event = await _make_event("multi-owner-event", "Multi Owner Event")
        user_1 = await _make_user("owner1@example.com")
        user_2 = await _make_user("owner2@example.com")
        await _make_event_membership(user_1.id, event.id, "event_owner")
        await _make_event_membership(user_2.id, event.id, "event_owner")

        expected_owner_id = min(user_1.id, user_2.id)
        raw_token = await _make_oauth_token(user_2.id, event.id, ["events:read"])

        # Repeat: a nondeterministic implementation can pass a single call by luck.
        for _ in range(3):
            resp = await _get_event(event.slug, raw_token)
            assert resp.status_code == 200, resp.text
            assert resp.json()["owner_id"] == expected_owner_id

    @pytest.mark.anyio
    async def test_other_membership_roles_are_not_treated_as_owner(self, setup_db):
        """Only role='event_owner' resolves owner_id — a coordinator must not."""
        event = await _make_event("coordinator-only", "Coordinator Only")
        user = await _make_user("coordinator@example.com")
        await _make_event_membership(user.id, event.id, "room_coordinator")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"], is_confidential=True)

        resp = await _get_event(event.slug, raw_token)

        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_id"] is None


# ---------------------------------------------------------------------------
# Resource visibility — docs/threat_model.md §3
# ---------------------------------------------------------------------------


class TestGetEventResourceVisibility:
    @pytest.mark.anyio
    async def test_returns_404_for_unknown_slug(self, setup_db):
        """A slug that doesn't exist must return 404."""
        event = await _make_event("real-event", "Real Event")
        user = await _make_user("user404@example.com")
        await _make_event_membership(user.id, event.id, "event_owner")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"])

        resp = await _get_event("does-not-exist", raw_token)

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Event not found"

    @pytest.mark.anyio
    async def test_soft_deleted_event_returns_404(self, setup_db):
        """A soft-deleted event (deleted_at is set) must return 404 even for its owner."""
        from portal.database import get_session
        from portal.models import Event

        event = await _make_event("deleted-event", "Deleted Event")
        user = await _make_user("owner-del@example.com")
        await _make_event_membership(user.id, event.id, "event_owner")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"])

        async with get_session() as s:
            evt = await s.get(Event, event.id)
            evt.deleted_at = datetime.now(timezone.utc)

        resp = await _get_event(event.slug, raw_token)

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Event not found"

    @pytest.mark.anyio
    async def test_wrong_event_token_returns_404(self, setup_db):
        """A token scoped to event-A requesting event-B must get 404, not 403.

        403 would confirm event-B exists. See docs/threat_model.md §3 and
        tests/test_oauth_multi_organizer.py::test_token_reuse_wrong_organizer.
        """
        event_a = await _make_event("event-alpha", "Event Alpha")
        event_b = await _make_event("event-beta", "Event Beta")
        user = await _make_user("crossevent@example.com")
        await _make_event_membership(user.id, event_a.id, "event_owner")
        await _make_event_membership(user.id, event_b.id, "event_owner")
        # Token is scoped to event_a only.
        raw_token = await _make_oauth_token(user.id, event_a.id, ["events:read"])

        resp = await _get_event(event_b.slug, raw_token)

        # Owner of BOTH events, yet still 404: the token's scope is what counts.
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Event not found"

    @pytest.mark.anyio
    async def test_unknown_slug_and_wrong_event_token_are_indistinguishable(self, setup_db):
        """The reviewer's case: an unknown slug and a real-but-wrong event must look identical.

        Otherwise a caller holding any token can enumerate the event namespace
        by diffing 403 against 404.
        """
        event_a = await _make_event("probe-source", "Probe Source")
        event_b = await _make_event("probe-target", "Probe Target")
        user = await _make_user("prober@example.com")
        await _make_event_membership(user.id, event_a.id, "event_owner")
        raw_token = await _make_oauth_token(user.id, event_a.id, ["events:read"])

        existing_other_event = await _get_event(event_b.slug, raw_token)
        unknown_slug = await _get_event("no-such-event-at-all", raw_token)

        assert existing_other_event.status_code == unknown_slug.status_code == 404
        assert existing_other_event.json() == unknown_slug.json()

    @pytest.mark.anyio
    async def test_soft_deleted_event_is_indistinguishable_from_unknown_slug(self, setup_db):
        """Soft-deletion must not become a side channel either."""
        from portal.database import get_session
        from portal.models import Event

        event = await _make_event("vanishing-event", "Vanishing Event")
        user = await _make_user("owner-vanish@example.com")
        await _make_event_membership(user.id, event.id, "event_owner")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"])

        async with get_session() as s:
            evt = await s.get(Event, event.id)
            evt.deleted_at = datetime.now(timezone.utc)

        deleted = await _get_event(event.slug, raw_token)
        unknown = await _get_event("never-existed", raw_token)

        assert deleted.status_code == unknown.status_code == 404
        assert deleted.json() == unknown.json()


# ---------------------------------------------------------------------------
# Authentication and account state
# ---------------------------------------------------------------------------


class TestGetEventAuthentication:
    @pytest.mark.anyio
    async def test_returns_401_without_token(self, setup_db):
        """Requests without an Authorization header must be rejected with 401."""
        await _make_event("open-event", "Open Event")

        resp = await _get_event("open-event", None)

        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_token_without_membership_returns_403(self, setup_db):
        """Correctly scoped token + no membership → 403.

        This is the one 403 the endpoint emits. It leaks nothing: the caller's
        token already names this event, so they know it exists.
        """
        event = await _make_event("members-only", "Members Only")
        user = await _make_user("nomembership@example.com")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"])

        resp = await _get_event(event.slug, raw_token)

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_deactivated_user_token_is_rejected(self, setup_db):
        """A still-valid, unexpired token must stop working once its user is deactivated.

        admin_toggle_user_active does not revoke OAuth tokens, so the account
        state has to be re-read from the User row on every request.
        """
        event = await _make_event("staffed-event", "Staffed Event")
        user = await _make_user("soon-disabled@example.com")
        await _make_event_membership(user.id, event.id, "event_owner")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"])

        # 1. The token works while the account is active.
        before = await _get_event(event.slug, raw_token)
        assert before.status_code == 200, before.text

        # 2. Deactivate the user — the token itself is untouched and unexpired.
        await _deactivate_user(user.id)

        # 3. The same token must now be refused.
        after = await _get_event(event.slug, raw_token)
        assert after.status_code == 403
        assert after.json()["detail"] == "User account is inactive"

    @pytest.mark.anyio
    async def test_deactivated_admin_token_is_rejected(self, setup_db):
        """Deactivation is checked before any privilege bypass, so admins are not exempt."""
        event = await _make_event("admin-event", "Admin Event")
        admin = await _make_user("disabled-admin@example.com", is_admin=True)
        await _make_event_membership(admin.id, event.id, "event_owner")
        raw_token = await _make_oauth_token(admin.id, event.id, ["events:read"])

        await _deactivate_user(admin.id)

        resp = await _get_event(event.slug, raw_token)

        assert resp.status_code == 403
        assert resp.json()["detail"] == "User account is inactive"


# ---------------------------------------------------------------------------
# Confidential clients — pre-existing behavior from PR #624, pinned here so
# this endpoint's authorization changes cannot widen it unnoticed.
# ---------------------------------------------------------------------------


class TestGetEventConfidentialClient:
    @pytest.mark.anyio
    async def test_confidential_client_bypasses_membership_rbac(self, setup_db):
        """Confidential clients are trusted to have verified permissions themselves."""
        event = await _make_event("m2m-event", "M2M Event")
        user = await _make_user("m2m@example.com")  # deliberately no membership
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"], is_confidential=True)

        resp = await _get_event(event.slug, raw_token)

        assert resp.status_code == 200, resp.text

    @pytest.mark.anyio
    async def test_confidential_client_wrong_event_token_returns_404(self, setup_db):
        """The confidential bypass must not cross event scope.

        Event-ID match is checked before the bypass, so a confidential token for
        event-A cannot read event-B.
        """
        event_a = await _make_event("m2m-alpha", "M2M Alpha")
        event_b = await _make_event("m2m-beta", "M2M Beta")
        user = await _make_user("m2m-cross@example.com")
        raw_token = await _make_oauth_token(user.id, event_a.id, ["events:read"], is_confidential=True)

        resp = await _get_event(event_b.slug, raw_token)

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Event not found"

    @pytest.mark.anyio
    async def test_confidential_client_cannot_read_soft_deleted_event(self, setup_db):
        """Soft-deletion outranks the confidential bypass."""
        from portal.database import get_session
        from portal.models import Event

        event = await _make_event("m2m-deleted", "M2M Deleted")
        user = await _make_user("m2m-del@example.com")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"], is_confidential=True)

        async with get_session() as s:
            evt = await s.get(Event, event.id)
            evt.deleted_at = datetime.now(timezone.utc)

        resp = await _get_event(event.slug, raw_token)

        assert resp.status_code == 404
