"""Tests for event-scoped memberships, token management, and admin CRUD.

Covers:
- EventMembership CRUD (set, update, remove, list)
- Token generation, listing, and revocation via admin routes
- Admin event members page (GET + POST)
- Token generation form on booth detail page
- Revoked/expired tokens cannot be reused
- Account page shows event memberships
- End-to-end: create event → add member → generate token → revoke token
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"

from datetime import timedelta

import pytest

from portal.auth import create_admin_token, create_user_token, hash_password

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
def admin_cookie():
    return {"admin_token": create_admin_token()}


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_test_user(email="test@example.com", display_name="Test User", password="securepass123"):
    from portal.database import create_user, get_session

    pw_hash = hash_password(password)
    async with get_session() as s:
        user = await create_user(s, email=email, display_name=display_name, password_hash=pw_hash)
    return user


async def _seed_event_room_booth():
    from portal.database import create_booth, create_event, create_room, get_session

    async with get_session() as s:
        event = await create_event(s, slug="testcon", display_name="TestCon 2026")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        booth = await create_booth(
            s,
            event_id=event.id,
            room_id=room.id,
            language_code="en",
            language_name="English",
        )
    return event, room, booth


# ---------------------------------------------------------------------------
# Database-level EventMembership CRUD
# ---------------------------------------------------------------------------


class TestEventMembershipDB:
    @pytest.mark.anyio
    async def test_create_membership(self, setup_db):
        from portal.database import get_session, set_event_membership

        user = await _create_test_user()
        event, _, _ = await _seed_event_room_booth()
        async with get_session() as s:
            m = await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")
        assert m.role == "event_owner"
        assert m.user_id == user.id
        assert m.event_id == event.id

    @pytest.mark.anyio
    async def test_update_membership_role(self, setup_db):
        from portal.database import get_session, set_event_membership

        user = await _create_test_user()
        event, _, _ = await _seed_event_room_booth()
        async with get_session() as s:
            await set_event_membership(s, user_id=user.id, event_id=event.id, role="room_coordinator")
        async with get_session() as s:
            m = await set_event_membership(s, user_id=user.id, event_id=event.id, role="room_coordinator")
        assert m.role == "room_coordinator"

    @pytest.mark.anyio
    async def test_list_memberships_for_event(self, setup_db):
        from portal.database import get_session, list_memberships_for_event, set_event_membership

        u1 = await _create_test_user(email="a@test.com")
        u2 = await _create_test_user(email="b@test.com")
        event, _, _ = await _seed_event_room_booth()
        async with get_session() as s:
            await set_event_membership(s, user_id=u1.id, event_id=event.id, role="interpreter")
            await set_event_membership(s, user_id=u2.id, event_id=event.id, role="room_coordinator")
        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        assert len(memberships) == 2
        roles = {m.role for m in memberships}
        assert roles == {"interpreter", "room_coordinator"}

    @pytest.mark.anyio
    async def test_list_memberships_for_user(self, setup_db):
        from portal.database import (
            create_event,
            get_session,
            list_memberships_for_user,
            set_event_membership,
        )

        user = await _create_test_user()
        event1, _, _ = await _seed_event_room_booth()
        async with get_session() as s:
            event2 = await create_event(s, slug="other", display_name="Other Event")
        async with get_session() as s:
            await set_event_membership(s, user_id=user.id, event_id=event1.id, role="interpreter")
            await set_event_membership(s, user_id=user.id, event_id=event2.id, role="room_coordinator")
        async with get_session() as s:
            memberships = await list_memberships_for_user(s, user.id)
        assert len(memberships) == 2

    @pytest.mark.anyio
    async def test_remove_membership(self, setup_db):
        from portal.database import (
            get_session,
            list_memberships_for_event,
            remove_event_membership,
            set_event_membership,
        )

        user = await _create_test_user()
        event, _, _ = await _seed_event_room_booth()
        async with get_session() as s:
            m = await set_event_membership(s, user_id=user.id, event_id=event.id, role="interpreter")
            mid = m.id
        async with get_session() as s:
            result = await remove_event_membership(s, mid)
        assert result is True
        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        assert len(memberships) == 0

    @pytest.mark.anyio
    async def test_remove_nonexistent_membership(self, setup_db):
        from portal.database import get_session, remove_event_membership

        async with get_session() as s:
            result = await remove_event_membership(s, 9999)
        assert result is False


# ---------------------------------------------------------------------------
# Database-level token CRUD
# ---------------------------------------------------------------------------


class TestTokenDB:
    @pytest.mark.anyio
    async def test_create_invite_token(self, setup_db):
        from portal.database import create_invite_token, get_session

        _, _, booth = await _seed_event_room_booth()
        async with get_session() as s:
            tok = await create_invite_token(s, booth_id=booth.id, role="interpreter", label="Alice")
        assert len(tok.token) == 64
        assert tok.role == "interpreter"
        assert tok.label == "Alice"
        assert tok.is_used is False
        assert tok.is_expired is False

    @pytest.mark.anyio
    async def test_revoke_invite_token(self, setup_db):
        from portal.database import create_invite_token, get_session, revoke_invite_token

        _, _, booth = await _seed_event_room_booth()
        async with get_session() as s:
            tok = await create_invite_token(s, booth_id=booth.id, role="interpreter")
            token_str = tok.token
        async with get_session() as s:
            revoked = await revoke_invite_token(s, token_str)
        assert revoked is not None
        assert revoked.used_at is not None
        assert revoked.is_used is True

    @pytest.mark.anyio
    async def test_revoke_nonexistent_token(self, setup_db):
        from portal.database import get_session, revoke_invite_token

        async with get_session() as s:
            result = await revoke_invite_token(s, "nonexistent-token-string")
        assert result is None

    @pytest.mark.anyio
    async def test_revoked_token_cannot_be_redeemed(self, setup_db):
        from portal.database import (
            create_invite_token,
            get_session,
            redeem_invite_token,
            revoke_invite_token,
        )

        _, _, booth = await _seed_event_room_booth()
        async with get_session() as s:
            tok = await create_invite_token(s, booth_id=booth.id, role="interpreter")
            token_str = tok.token
        async with get_session() as s:
            await revoke_invite_token(s, token_str)
        async with get_session() as s:
            with pytest.raises(ValueError, match="already been used"):
                await redeem_invite_token(s, token_str)

    @pytest.mark.anyio
    async def test_expired_token_cannot_be_redeemed(self, setup_db):
        from portal.database import create_invite_token, get_session, redeem_invite_token
        from portal.models import utc_now

        _, _, booth = await _seed_event_room_booth()
        past = utc_now() - timedelta(hours=1)
        async with get_session() as s:
            tok = await create_invite_token(s, booth_id=booth.id, role="interpreter", expires_at=past)
            token_str = tok.token
        async with get_session() as s:
            with pytest.raises(ValueError, match="expired"):
                await redeem_invite_token(s, token_str)

    @pytest.mark.anyio
    async def test_list_tokens_for_booth(self, setup_db):
        from portal.database import create_invite_token, get_session, list_tokens_for_booth

        _, _, booth = await _seed_event_room_booth()
        async with get_session() as s:
            await create_invite_token(s, booth_id=booth.id, role="interpreter", label="T1")
            await create_invite_token(s, booth_id=booth.id, role="room_coordinator", label="T2")
        async with get_session() as s:
            tokens = await list_tokens_for_booth(s, booth.id)
        assert len(tokens) == 2


# ---------------------------------------------------------------------------
# Admin event members routes (HTTP)
# ---------------------------------------------------------------------------


class TestAdminEventMembersRoutes:
    @pytest.mark.anyio
    async def test_members_page_renders(self, setup_db, admin_cookie):
        event, _, _ = await _seed_event_room_booth()
        await _create_test_user(email="listed@test.com", display_name="Listed")
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/members/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"Members" in resp.content
        # Table should show empty state because no members are assigned
        assert b"No users have been assigned" in resp.content

    @pytest.mark.anyio
    async def test_members_page_404_for_missing_event(self, setup_db, admin_cookie):
        async with _client() as c:
            resp = await c.get("/admin/events/9999/members/", cookies=admin_cookie)
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_members_page_requires_admin(self, setup_db):
        event, _, _ = await _seed_event_room_booth()
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/members/")
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_add_member(self, setup_db, admin_cookie):
        from portal.database import get_session, list_memberships_for_event

        event, _, _ = await _seed_event_room_booth()
        user = await _create_test_user()
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/members/",
                data={"email": user.email, "role": "event_owner"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        assert len(memberships) == 1
        assert memberships[0].role == "event_owner"
        assert memberships[0].user_id == user.id

    @pytest.mark.anyio
    async def test_members_page_shows_assigned_role(self, setup_db, admin_cookie):
        from portal.database import get_session, set_event_membership

        event, _, _ = await _seed_event_room_booth()
        user = await _create_test_user()
        async with get_session() as s:
            await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/members/", cookies=admin_cookie)
        assert resp.status_code == 200
        # The badge for event_admin should be rendered
        assert b"badge-event_owner" in resp.content
        assert b"Remove" in resp.content

    @pytest.mark.anyio
    async def test_remove_member(self, setup_db, admin_cookie):
        from portal.database import get_session, list_memberships_for_event, set_event_membership

        event, _, _ = await _seed_event_room_booth()
        user = await _create_test_user()
        async with get_session() as s:
            m = await set_event_membership(s, user_id=user.id, event_id=event.id, role="interpreter")
            mid = m.id
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/members/{mid}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        assert len(memberships) == 0

    @pytest.mark.anyio
    async def test_set_none_removes_membership(self, setup_db, admin_cookie):
        from portal.database import get_session, list_memberships_for_event, set_event_membership

        event, _, _ = await _seed_event_room_booth()
        user = await _create_test_user()
        async with get_session() as s:
            await set_event_membership(s, user_id=user.id, event_id=event.id, role="interpreter")
        # POST with empty role removes the membership
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/members/",
                data={"email": user.email, "role": ""},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        assert len(memberships) == 0


# ---------------------------------------------------------------------------
# Admin token management routes (HTTP)
# ---------------------------------------------------------------------------


class TestAdminTokenRoutes:
    @pytest.mark.anyio
    async def test_generate_token(self, setup_db, admin_cookie):
        from portal.database import get_session, list_tokens_for_booth

        event, room, booth = await _seed_event_room_booth()
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/tokens/",
                data={"role": "interpreter", "label": "Alice"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            tokens = await list_tokens_for_booth(s, booth.id)
        assert len(tokens) == 1
        assert tokens[0].role == "interpreter"
        assert tokens[0].label == "Alice"

    @pytest.mark.anyio
    async def test_generate_token_with_expiry(self, setup_db, admin_cookie):
        from portal.database import get_session, list_tokens_for_booth

        event, room, booth = await _seed_event_room_booth()
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/tokens/",
                data={"role": "interpreter", "label": "Bob", "expires_hours": "24"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            tokens = await list_tokens_for_booth(s, booth.id)
        assert len(tokens) == 1
        assert tokens[0].expires_at is not None

    @pytest.mark.anyio
    async def test_generate_token_requires_admin(self, setup_db):
        event, room, booth = await _seed_event_room_booth()
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/tokens/",
                data={"role": "interpreter", "label": "Alice"},
            )
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_generate_token_without_role_is_noop(self, setup_db, admin_cookie):
        from portal.database import get_session, list_tokens_for_booth

        event, room, booth = await _seed_event_room_booth()
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/tokens/",
                data={"role": "", "label": "NoRole"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            tokens = await list_tokens_for_booth(s, booth.id)
        assert len(tokens) == 0

    @pytest.mark.anyio
    async def test_revoke_token_via_route(self, setup_db, admin_cookie):
        from portal.database import create_invite_token, get_invite_token, get_session

        event, room, booth = await _seed_event_room_booth()
        async with get_session() as s:
            tok = await create_invite_token(s, booth_id=booth.id, role="interpreter")
            token_str = tok.token
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/tokens/{token_str}/revoke",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            revoked = await get_invite_token(s, token_str)
        assert revoked.is_used is True

    @pytest.mark.anyio
    async def test_revoke_token_requires_admin(self, setup_db):
        event, room, booth = await _seed_event_room_booth()
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/tokens/fake-token/revoke",
            )
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_booth_detail_shows_token_form(self, setup_db, admin_cookie):
        event, room, booth = await _seed_event_room_booth()
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/",
                cookies=admin_cookie,
            )
        assert resp.status_code == 200
        assert b"Generate New Token" in resp.content
        assert b"Generate Token" in resp.content

    @pytest.mark.anyio
    async def test_booth_detail_shows_tokens(self, setup_db, admin_cookie):
        from portal.database import create_invite_token, get_session

        event, room, booth = await _seed_event_room_booth()
        async with get_session() as s:
            await create_invite_token(s, booth_id=booth.id, role="interpreter", label="TestTok")
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/",
                cookies=admin_cookie,
            )
        assert resp.status_code == 200
        assert b"TestTok" in resp.content
        assert b"Revoke" in resp.content
        assert b"Copy Link" in resp.content

    @pytest.mark.anyio
    async def test_booth_detail_shows_revoked_token(self, setup_db, admin_cookie):
        from portal.database import create_invite_token, get_session, revoke_invite_token

        event, room, booth = await _seed_event_room_booth()
        async with get_session() as s:
            tok = await create_invite_token(s, booth_id=booth.id, role="interpreter", label="RevokedTok")
            await revoke_invite_token(s, tok.token)
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/",
                cookies=admin_cookie,
            )
        assert resp.status_code == 200
        assert b"RevokedTok" in resp.content
        assert b"Revoked / Used" in resp.content


# ---------------------------------------------------------------------------
# Account page shows event memberships
# ---------------------------------------------------------------------------


class TestAccountMemberships:
    @pytest.mark.anyio
    async def test_account_shows_memberships(self, setup_db):
        from portal.database import get_session, set_event_membership

        user = await _create_test_user(email="member@test.com", display_name="Member")
        event, _, _ = await _seed_event_room_booth()
        async with get_session() as s:
            await set_event_membership(s, user_id=user.id, event_id=event.id, role="interpreter")
        token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
        async with _client() as c:
            resp = await c.get("/account", cookies={"user_token": token})
        assert resp.status_code == 200
        assert b"interpreter" in resp.content
        assert b"TestCon 2026" in resp.content

    @pytest.mark.anyio
    async def test_account_shows_no_memberships(self, setup_db):
        user = await _create_test_user(email="lonely@test.com", display_name="Lonely")
        token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
        async with _client() as c:
            resp = await c.get("/account", cookies={"user_token": token})
        assert resp.status_code == 200
        assert b"not been assigned" in resp.content


# ---------------------------------------------------------------------------
# Event detail page shows Members link
# ---------------------------------------------------------------------------


class TestEventDetailMembersLink:
    @pytest.mark.anyio
    async def test_event_detail_has_members_link(self, setup_db, admin_cookie):
        event, _, _ = await _seed_event_room_booth()
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"Manage Members" in resp.content
        assert f"/admin/events/{event.id}/members/".encode() in resp.content


# ---------------------------------------------------------------------------
# User list page (no global role dropdown)
# ---------------------------------------------------------------------------


class TestUserListNoGlobalRole:
    @pytest.mark.anyio
    async def test_user_list_no_role_dropdown(self, setup_db, admin_cookie):
        await _create_test_user()
        async with _client() as c:
            resp = await c.get("/admin/users/", cookies=admin_cookie)
        assert resp.status_code == 200
        # Should NOT have role-change form or super_admin option
        assert b"super_admin" not in resp.content
        assert b"role-select" not in resp.content
        # Should say roles are per-event
        assert b"per event" in resp.content.lower() or b"per-event" in resp.content.lower()


# ---------------------------------------------------------------------------
# End-to-end: full admin workflow
# ---------------------------------------------------------------------------


class TestEndToEndAdminWorkflow:
    @pytest.mark.anyio
    async def test_full_workflow(self, setup_db, admin_cookie):
        """E2E: create event → add user → assign role → generate token → revoke."""
        from portal.database import get_invite_token, get_session, list_memberships_for_event

        # 1. Create event
        async with _client() as c:
            resp = await c.post(
                "/admin/events/",
                data={
                    "slug": "e2e-event",
                    "display_name": "E2E Event",
                },
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

        # Find the event
        from portal.database import get_event_by_slug

        async with get_session() as s:
            event = await get_event_by_slug(s, "e2e-event")
        assert event is not None

        # 2. Create room
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/",
                data={
                    "display_name": "Room A",
                },
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

        from portal.database import list_rooms_for_event

        async with get_session() as s:
            rooms = await list_rooms_for_event(s, event.id)
        assert len(rooms) == 1
        room = rooms[0]

        # 3. Create booth
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/",
                data={
                    "language_code": "fr",
                    "language_name": "French",
                },
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

        from portal.database import list_booths_for_room

        async with get_session() as s:
            booths = await list_booths_for_room(s, room.id)
        assert len(booths) == 1
        booth = booths[0]

        # 4. Register a user
        async with _client() as c:
            resp = await c.post(
                "/register",
                data={
                    "email": "alice@e2e.com",
                    "display_name": "Alice",
                    "password": "securepass123",
                    "password_confirm": "securepass123",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 303

        from portal.database import get_user_by_email

        async with get_session() as s:
            alice = await get_user_by_email(s, "alice@e2e.com")
        assert alice is not None

        # 5. Assign per-event role
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/members/",
                data={
                    "email": alice.email,
                    "role": "event_owner",
                },
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        assert len(memberships) == 1
        assert memberships[0].role == "event_owner"

        # 6. Generate invite token for the booth
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/tokens/",
                data={"role": "interpreter", "label": "Alice FR booth"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

        from portal.database import list_tokens_for_booth

        async with get_session() as s:
            tokens = await list_tokens_for_booth(s, booth.id)
        assert len(tokens) == 1
        token_str = tokens[0].token
        assert tokens[0].label == "Alice FR booth"

        # 7. Verify booth detail page shows the token
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/",
                cookies=admin_cookie,
            )
        assert resp.status_code == 200
        assert b"Alice FR booth" in resp.content
        assert b"Revoke" in resp.content

        # 8. Revoke the token
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/tokens/{token_str}/revoke",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

        async with get_session() as s:
            revoked = await get_invite_token(s, token_str)
        assert revoked.is_used is True

        # 9. Verify Alice's account page shows the membership
        user_token = create_user_token(user_id=alice.id, email=alice.email, is_admin=alice.is_admin)
        async with _client() as c:
            resp = await c.get("/account", cookies={"user_token": user_token})
        assert resp.status_code == 200
        assert b"event_owner" in resp.content
        assert b"E2E Event" in resp.content

        # 10. Remove membership
        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        mid = memberships[0].id
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/members/{mid}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        assert len(memberships) == 0
