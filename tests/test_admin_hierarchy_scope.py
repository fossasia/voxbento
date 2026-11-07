"""Regression tests: admin and workspace resource deletion must honour hierarchy scope.

Ensures that handlers taking nested resource path parameters (event -> room -> booth -> subresource)
validate that each child resource actually belongs to the specified parent.
Covers:
- Booth deletion (admin_delete_booth)
- Booth membership removal (admin_remove_booth_member)
- Token revocation (admin_revoke_token)
- Room membership removal (admin_remove_room_member)
- Event membership removal (admin_remove_event_member)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from portal.auth import create_user_token
from portal.models import AuthToken, BoothMembership, EventMembership, InviteToken, RoomMembership


@pytest.fixture(autouse=True)
async def setup_db():
    """Configure an in-memory SQLite database and tear it down after each test."""
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def admin_cookie():
    """A real super-admin row plus its ``user_token`` cookie."""
    from portal.auth import hash_password
    from portal.database import create_user, get_session

    async with get_session() as s:
        user = await create_user(
            s,
            email="scope-super-admin@example.com",
            display_name="Super Admin",
            password_hash=hash_password("test-admin-pass"),
            is_admin=True,
        )
    return {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=True)}


@pytest.fixture
async def booth_scope():
    """Two events; ``owner`` only owns event A, ``coordinator`` only coordinates room A1."""
    from portal.database import (
        create_booth,
        create_event,
        create_invite_token,
        create_room,
        create_user,
        get_session,
        set_booth_membership,
        set_event_membership,
        set_room_membership,
    )

    async with get_session() as s:
        event_a = await create_event(s, slug="scope-a", display_name="Event A")
        event_b = await create_event(s, slug="scope-b", display_name="Event B")
        room_a1 = await create_room(s, event_id=event_a.id, display_name="A room 1")
        room_a2 = await create_room(s, event_id=event_a.id, display_name="A room 2")
        room_b = await create_room(s, event_id=event_b.id, display_name="B room")

        booth_a1 = await create_booth(
            s, event_id=event_a.id, room_id=room_a1.id, language_code="en", language_name="English"
        )
        booth_a2 = await create_booth(
            s, event_id=event_a.id, room_id=room_a2.id, language_code="de", language_name="German"
        )
        booth_b = await create_booth(
            s, event_id=event_b.id, room_id=room_b.id, language_code="fr", language_name="French"
        )

        invite_a1 = await create_invite_token(s, booth_id=booth_a1.id, role="interpreter")
        invite_a2 = await create_invite_token(s, booth_id=booth_a2.id, role="interpreter")
        invite_b = await create_invite_token(s, booth_id=booth_b.id, role="interpreter")

        owner = await create_user(s, email="scope-owner@example.com", display_name="Owner A")
        secondary_owner_a = await create_user(s, email="scope-owner-a2@example.com", display_name="Secondary Owner A")
        owner_b = await create_user(s, email="scope-owner-b@example.com", display_name="Owner B")
        coordinator = await create_user(s, email="scope-coordinator@example.com", display_name="Coordinator A1")
        member_a = await create_user(s, email="scope-member-a@example.com", display_name="Interpreter A")
        member_b = await create_user(s, email="scope-member-b@example.com", display_name="Interpreter B")

        em_a = await set_event_membership(s, user_id=owner.id, event_id=event_a.id, role="event_owner")
        em_b = await set_event_membership(s, user_id=owner_b.id, event_id=event_b.id, role="event_owner")
        rm_a1 = await set_room_membership(s, user_id=coordinator.id, room_id=room_a1.id, role="room_coordinator")
        rm_a2 = await set_room_membership(s, user_id=member_a.id, room_id=room_a2.id, role="room_coordinator")
        rm_b = await set_room_membership(s, user_id=member_b.id, room_id=room_b.id, role="room_coordinator")
        membership_a1 = await set_booth_membership(s, user_id=member_a.id, booth_id=booth_a1.id, role="interpreter")
        membership_a2 = await set_booth_membership(s, user_id=member_a.id, booth_id=booth_a2.id, role="interpreter")
        membership_b = await set_booth_membership(s, user_id=member_b.id, booth_id=booth_b.id, role="interpreter")

        return {
            "event_a": event_a.id,
            "event_b": event_b.id,
            "room_a1": room_a1.id,
            "room_a2": room_a2.id,
            "room_b": room_b.id,
            "booth_a1": booth_a1.id,
            "booth_a2": booth_a2.id,
            "booth_b": booth_b.id,
            "invite_a1": invite_a1.token,
            "invite_a2": invite_a2.token,
            "invite_b": invite_b.token,
            "membership_a1": membership_a1.id,
            "membership_a2": membership_a2.id,
            "membership_b": membership_b.id,
            "em_a": em_a.id,
            "em_b": em_b.id,
            "rm_a1": rm_a1.id,
            "rm_a2": rm_a2.id,
            "rm_b": rm_b.id,
            "secondary_owner_a_id": secondary_owner_a.id,
            "owner_cookie": {"user_token": create_user_token(user_id=owner.id, email=owner.email)},
            "coordinator_cookie": {"user_token": create_user_token(user_id=coordinator.id, email=coordinator.email)},
        }


def _client():
    """Create an async test client configured for the FastAPI app."""
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _booth_exists(booth_id: int) -> bool:
    """Return True if a booth row exists for the given booth ID."""
    from portal.database import get_session
    from portal.models import DBBooth

    async with get_session() as s:
        return await s.get(DBBooth, booth_id) is not None


# ===========================================================================
# 1. Security Invariants (BOLA / Cross-Scope Exploit Prevention)
# ===========================================================================


class TestSecurityInvariants:
    """Core tenant-isolation invariants. Must fail on baseline and pass on patched."""

    @pytest.mark.anyio
    async def test_owner_cross_event_booth_deletion_rejected(self, booth_scope):
        """Owner A cannot delete Booth B via Event A URL parameters."""
        from portal.database import get_session
        from portal.models import BoothMembership, DBBooth, InviteToken

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_b']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        async with get_session() as s:
            assert await s.get(DBBooth, scope["booth_b"]) is not None
            assert await s.get(InviteToken, scope["invite_b"]) is not None
            assert await s.get(BoothMembership, scope["membership_b"]) is not None
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_owner_cross_event_room_booth_deletion_rejected(self, booth_scope):
        """Owner A cannot delete Booth B via Event A URL with Room B."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_b']}/booths/{scope['booth_b']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert await _booth_exists(scope["booth_b"]) is True
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_owner_same_event_wrong_room_booth_deletion_rejected(self, booth_scope):
        """Owner A cannot delete Room A2's booth using Room A1 in the URL path."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a2']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert await _booth_exists(scope["booth_a2"]) is True
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_coordinator_cross_event_booth_deletion_rejected(self, booth_scope):
        """Room Coordinator A1 is denied by the event-owner gate and cannot delete Booth B."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_b']}/delete",
                cookies=scope["coordinator_cookie"],
                follow_redirects=False,
            )
        assert await _booth_exists(scope["booth_b"]) is True
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_coordinator_same_event_unauthorized_room_booth_deletion_rejected(self, booth_scope):
        """Room Coordinator A1 cannot delete Booth A2 (unauthorized room) via /admin with Room A1."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a2']}/delete",
                cookies=scope["coordinator_cookie"],
                follow_redirects=False,
            )
        assert await _booth_exists(scope["booth_a2"]) is True
        assert resp.status_code == 403


# ===========================================================================
# 2. Resource Validation
# ===========================================================================


class TestResourceValidation:
    """Nonexistent identifier handling."""

    @pytest.mark.anyio
    async def test_nonexistent_room_returns_404(self, booth_scope):
        """Reject booth deletion with 404 when the room ID does not exist."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/99999/booths/{scope['booth_a1']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert await _booth_exists(scope["booth_a1"]) is True
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_nonexistent_booth_returns_404(self, booth_scope):
        """Reject booth deletion with 404 when the booth ID does not exist."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/99999/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert await _booth_exists(scope["booth_a1"]) is True
        assert await _booth_exists(scope["booth_b"]) is True
        assert resp.status_code == 404


class TestMissingSubresources:
    """Missing parents and targets must return 404 without deleting sibling rows."""

    @pytest.mark.parametrize(
        ("path_template", "model", "target_key"),
        [
            (
                "/workspace/events/{event_a}/rooms/99999/members/{rm_a1}/delete",
                RoomMembership,
                "rm_a1",
            ),
            (
                "/workspace/events/{event_a}/rooms/{room_a1}/members/99999/delete",
                RoomMembership,
                "rm_a1",
            ),
            (
                "/workspace/events/{event_a}/rooms/{room_a1}/booths/99999/members/{membership_a1}/delete",
                BoothMembership,
                "membership_a1",
            ),
            (
                "/workspace/events/{event_a}/rooms/{room_a1}/booths/{booth_a1}/members/99999/delete",
                BoothMembership,
                "membership_a1",
            ),
            (
                "/workspace/events/{event_a}/rooms/{room_a1}/booths/{booth_a1}/tokens/{missing_token}/revoke",
                InviteToken,
                "invite_a1",
            ),
            (
                "/workspace/events/{event_a}/members/99999/delete",
                EventMembership,
                "em_a",
            ),
        ],
        ids=[
            "room-member-missing-room",
            "room-member-missing-membership",
            "booth-member-missing-booth",
            "booth-member-missing-membership",
            "token-missing-token",
            "event-member-missing-membership",
        ],
    )
    @pytest.mark.anyio
    async def test_missing_subresource_returns_404_without_mutation(
        self, booth_scope, path_template, model, target_key
    ):
        """Reject mutations with 404 when targeted subresource does not exist."""
        from portal.database import get_session

        scope = booth_scope
        path = path_template.format(missing_token="0" * 64, **scope)
        async with _client() as c:
            resp = await c.post(path, cookies=scope["owner_cookie"], follow_redirects=False)
        async with get_session() as s:
            row = await s.get(model, scope[target_key])
            assert row is not None
            if model is InviteToken:
                assert row.is_used is False
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_missing_event_returns_404_without_mutation(self, admin_cookie, booth_scope):
        """Reject member deletion with 404 when the event ID does not exist."""
        from portal.database import get_session

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/99999/members/{scope['em_a']}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        async with get_session() as s:
            assert await s.get(EventMembership, scope["em_a"]) is not None
        assert resp.status_code == 404


# ===========================================================================
# 3. Super-Admin Consistency
# ===========================================================================


class TestSuperAdminConsistency:
    """Super-admin global authorization consistency with URL parameters."""

    @pytest.mark.anyio
    async def test_super_admin_mismatched_event_returns_404(self, admin_cookie, booth_scope):
        """A super-admin has global auth, but mismatched event/room/booth paths must return 404."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_b']}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert await _booth_exists(scope["booth_b"]) is True
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_legitimate_super_admin_booth_deletion_succeeds(self, admin_cookie, booth_scope):
        """Global admin auth still permits a hierarchy-consistent deletion."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/"
        assert await _booth_exists(scope["booth_a1"]) is False
        assert await _booth_exists(scope["booth_a2"]) is True
        assert await _booth_exists(scope["booth_b"]) is True

    @pytest.mark.parametrize(
        ("path_template", "model", "target_key", "expected_status"),
        [
            (
                "/admin/events/{event_a}/rooms/{room_a1}/booths/{booth_a1}/members/{membership_b}/delete",
                BoothMembership,
                "membership_b",
                404,
            ),
            (
                "/admin/events/{event_a}/rooms/{room_a1}/booths/{booth_a1}/tokens/{invite_b}/revoke",
                InviteToken,
                "invite_b",
                404,
            ),
            (
                "/admin/events/{event_a}/rooms/{room_a1}/members/{rm_b}/delete",
                RoomMembership,
                "rm_b",
                404,
            ),
            (
                "/admin/events/{event_a}/members/{em_b}/delete",
                EventMembership,
                "em_b",
                404,
            ),
        ],
        ids=["booth-member", "token", "room-member", "event-member"],
    )
    @pytest.mark.anyio
    async def test_super_admin_cross_event_subresource_rejected(
        self, admin_cookie, booth_scope, path_template, model, target_key, expected_status
    ):
        """Global admin auth must not bypass the resource hierarchy checks."""
        from portal.database import get_session

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(path_template.format(**scope), cookies=admin_cookie, follow_redirects=False)
        async with get_session() as s:
            row = await s.get(model, scope[target_key])
            assert row is not None
            if model is InviteToken:
                assert row.is_used is False
        assert resp.status_code == expected_status


# ===========================================================================
# 4. Authorization & Operational Controls (Positive & Negative Controls)
# ===========================================================================


class TestAuthorizationControls:
    """Controls: Auth gates must remain enforced; legitimate deletes must succeed."""

    @pytest.mark.anyio
    async def test_foreign_event_owner_rejected_with_403(self, booth_scope):
        """Owner A posting to Event B's URL is stopped by auth middleware with 403."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_b']}/rooms/{scope['room_b']}/booths/{scope['booth_b']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 403
        assert await _booth_exists(scope["booth_b"]) is True

    @pytest.mark.anyio
    async def test_coordinator_cannot_access_unauthorized_room_directly_403(self, booth_scope):
        """Coordinator A1 posting to Room A2's URL directly is stopped by auth with 403."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{scope['event_a']}/rooms/{scope['room_a2']}/booths/{scope['booth_a2']}/delete",
                cookies=scope["coordinator_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 403
        assert await _booth_exists(scope["booth_a2"]) is True

    @pytest.mark.anyio
    async def test_legitimate_event_owner_booth_deletion_succeeds_and_cascades(self, booth_scope):
        """Owner A deletes Booth A1 in Room A1: succeeds (303) and cascades dependents."""
        from portal.database import get_session
        from portal.models import BoothMembership, DBBooth, InviteToken

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/"

        async with get_session() as s:
            assert await s.get(DBBooth, scope["booth_a1"]) is None
            assert await s.get(InviteToken, scope["invite_a1"]) is None
            assert await s.get(BoothMembership, scope["membership_a1"]) is None

            assert await s.get(DBBooth, scope["booth_a2"]) is not None
            assert await s.get(DBBooth, scope["booth_b"]) is not None
            assert await s.get(InviteToken, scope["invite_b"]) is not None
            assert await s.get(BoothMembership, scope["membership_b"]) is not None

    @pytest.mark.anyio
    async def test_coordinator_booth_deletion_denied(self, booth_scope):
        """Coordinators cannot delete booths; the event-owner gate returns 403 and mutates nothing."""
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/delete",
                cookies=scope["coordinator_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 403
        assert await _booth_exists(scope["booth_a1"]) is True
        assert await _booth_exists(scope["booth_a2"]) is True
        assert await _booth_exists(scope["booth_b"]) is True

    @pytest.mark.parametrize(
        ("path_template", "model", "target_key"),
        [
            (
                "/admin/events/{event_a}/rooms/{room_a1}/booths/{booth_a1}/members/{membership_a1}/delete",
                BoothMembership,
                "membership_a1",
            ),
            (
                "/admin/events/{event_a}/rooms/{room_a1}/booths/{booth_a1}/tokens/{invite_a1}/revoke",
                InviteToken,
                "invite_a1",
            ),
            (
                "/admin/events/{event_a}/rooms/{room_a1}/members/{rm_a1}/delete",
                RoomMembership,
                "rm_a1",
            ),
        ],
        ids=["booth-member", "token", "room-member"],
    )
    @pytest.mark.anyio
    async def test_coordinator_subresource_operations_denied(self, booth_scope, path_template, model, target_key):
        """Coordinators cannot manage team members or tokens; the event-owner gate returns 403."""
        from portal.database import get_session

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                path_template.format(**scope), cookies=scope["coordinator_cookie"], follow_redirects=False
            )
        assert resp.status_code == 403
        async with get_session() as s:
            row = await s.get(model, scope[target_key])
            assert row is not None
            if model is InviteToken:
                assert row.is_used is False


# ===========================================================================
# 5. Sibling Subresource Security & Invariants
# ===========================================================================


class TestSiblingSubresourceSecurity:
    """Security invariants for sibling subresource deletion and owner lockout."""

    @pytest.mark.anyio
    async def test_owner_cross_event_booth_member_deletion_rejected(self, booth_scope):
        """Owner A cannot delete Booth B's BoothMembership via Event A URL."""
        from portal.database import get_session
        from portal.models import BoothMembership

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/members/{scope['membership_b']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        async with get_session() as s:
            assert await s.get(BoothMembership, scope["membership_b"]) is not None
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_owner_cross_event_token_revocation_rejected(self, booth_scope):
        """Owner A cannot revoke Booth B's InviteToken via Event A URL."""
        from portal.database import get_session
        from portal.models import InviteToken

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/tokens/{scope['invite_b']}/revoke",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        async with get_session() as s:
            tok = await s.get(InviteToken, scope["invite_b"])
            assert tok is not None
            assert tok.is_used is False
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_owner_cross_event_room_member_deletion_rejected(self, booth_scope):
        """Owner A cannot delete Room B's RoomMembership via Event A URL."""
        from portal.database import get_session
        from portal.models import RoomMembership

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/members/{scope['rm_b']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        async with get_session() as s:
            assert await s.get(RoomMembership, scope["rm_b"]) is not None
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_owner_cross_event_event_member_deletion_rejected(self, booth_scope):
        """Owner A cannot delete Event B's EventMembership via Event A URL."""
        from portal.database import get_session
        from portal.models import EventMembership

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/members/{scope['em_b']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        async with get_session() as s:
            assert await s.get(EventMembership, scope["em_b"]) is not None
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_owner_same_event_wrong_room_booth_member_deletion_rejected(self, booth_scope):
        """Owner A cannot delete Booth A2's BoothMembership via Room A1 URL parameters."""
        from portal.database import get_session
        from portal.models import BoothMembership

        scope = booth_scope
        async with get_session() as s:
            assert await s.get(BoothMembership, scope["membership_a2"]) is not None

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a2']}/members/{scope['membership_a2']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        async with get_session() as s:
            assert await s.get(BoothMembership, scope["membership_a2"]) is not None
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_owner_same_event_wrong_room_token_revocation_rejected(self, booth_scope):
        """Owner A cannot revoke Booth A2's InviteToken via Room A1/Booth A1 URL parameters."""
        from portal.database import get_session
        from portal.models import InviteToken

        scope = booth_scope
        async with get_session() as s:
            tok_before = await s.get(InviteToken, scope["invite_a2"])
            assert tok_before is not None
            assert tok_before.is_used is False

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/tokens/{scope['invite_a2']}/revoke",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        async with get_session() as s:
            tok_after = await s.get(InviteToken, scope["invite_a2"])
            assert tok_after is not None
            assert tok_after.is_used is False
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_owner_same_event_wrong_room_room_member_deletion_rejected(self, booth_scope):
        """Owner A cannot delete Room A2's RoomMembership via Room A1 URL parameters."""
        from portal.database import get_session
        from portal.models import RoomMembership

        scope = booth_scope
        async with get_session() as s:
            assert await s.get(RoomMembership, scope["rm_a2"]) is not None

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/members/{scope['rm_a2']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        async with get_session() as s:
            assert await s.get(RoomMembership, scope["rm_a2"]) is not None
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_legitimate_event_member_deletion_succeeds(self, booth_scope):
        """Owner A can delete a legitimate EventMembership in Event A."""
        from portal.database import get_session, set_event_membership
        from portal.models import EventMembership

        scope = booth_scope
        async with get_session() as s:
            target_em = await set_event_membership(
                s, user_id=scope["secondary_owner_a_id"], event_id=scope["event_a"], role="room_coordinator"
            )
            target_em_id = target_em.id

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/members/{target_em_id}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/workspace/events/{scope['event_a']}/members/"
        async with get_session() as s:
            assert await s.get(EventMembership, target_em_id) is None
            assert await s.get(EventMembership, scope["em_a"]) is not None
            assert await s.get(EventMembership, scope["em_b"]) is not None

    @pytest.mark.anyio
    async def test_legitimate_booth_member_deletion_succeeds(self, booth_scope):
        """Owner A can delete a legitimate BoothMembership in Event A."""
        from portal.database import get_session
        from portal.models import BoothMembership

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/members/{scope['membership_a1']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/"
        )
        async with get_session() as s:
            assert await s.get(BoothMembership, scope["membership_a1"]) is None
            assert await s.get(BoothMembership, scope["membership_a2"]) is not None
            assert await s.get(BoothMembership, scope["membership_b"]) is not None

    @pytest.mark.anyio
    async def test_legitimate_room_member_deletion_succeeds(self, booth_scope):
        """Owner A can delete a legitimate RoomMembership in Event A."""
        from portal.database import get_session
        from portal.models import RoomMembership

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/members/{scope['rm_a1']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/"
        async with get_session() as s:
            assert await s.get(RoomMembership, scope["rm_a1"]) is None
            assert await s.get(RoomMembership, scope["rm_a2"]) is not None
            assert await s.get(RoomMembership, scope["rm_b"]) is not None

    @pytest.mark.anyio
    async def test_legitimate_token_revocation_succeeds(self, booth_scope):
        """Owner A can revoke a legitimate InviteToken in Event A."""
        from portal.database import get_session
        from portal.models import InviteToken

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/tokens/{scope['invite_a1']}/revoke",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/"
        )
        async with get_session() as s:
            tok = await s.get(InviteToken, scope["invite_a1"])
            assert tok is not None
            assert tok.is_used is True
            sibling_a2 = await s.get(InviteToken, scope["invite_a2"])
            sibling_b = await s.get(InviteToken, scope["invite_b"])
            assert sibling_a2 is not None and sibling_a2.is_used is False
            assert sibling_b is not None and sibling_b.is_used is False

    @pytest.mark.anyio
    async def test_owner_cross_event_token_creation_rejected(self, booth_scope):
        """Owner A cannot create a token for Booth B via mismatched URL parameters."""
        from portal.database import get_session, list_tokens_for_booth

        scope = booth_scope
        async with get_session() as s:
            tokens_before = await list_tokens_for_booth(s, scope["booth_b"])

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_b']}/tokens/",
                data={"role": "interpreter", "label": "malicious token"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            tokens_after = await list_tokens_for_booth(s, scope["booth_b"])
            assert len(tokens_after) == len(tokens_before)

    @pytest.mark.anyio
    async def test_owner_same_event_wrong_room_token_creation_rejected(self, booth_scope):
        """Owner A cannot create a token for Booth A2 via Room A1 URL parameters."""
        from portal.database import get_session, list_tokens_for_booth

        scope = booth_scope
        async with get_session() as s:
            tokens_before = await list_tokens_for_booth(s, scope["booth_a2"])

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a2']}/tokens/",
                data={"role": "interpreter", "label": "wrong room token"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            tokens_after = await list_tokens_for_booth(s, scope["booth_a2"])
            assert len(tokens_after) == len(tokens_before)

    @pytest.mark.anyio
    async def test_owner_cross_event_booth_member_add_rejected(self, booth_scope):
        """Owner A cannot add a member to Booth B via mismatched URL parameters."""
        from portal.database import get_session, list_memberships_for_booth

        scope = booth_scope
        async with get_session() as s:
            members_before = await list_memberships_for_booth(s, scope["booth_b"])

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_b']}/members/",
                data={"email": "new-interp@example.com", "role": "interpreter"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            members_after = await list_memberships_for_booth(s, scope["booth_b"])
            assert len(members_after) == len(members_before)

    @pytest.mark.anyio
    async def test_owner_cross_event_room_member_add_rejected(self, booth_scope):
        """Owner A cannot add a room coordinator to Room B via mismatched URL parameters."""
        from portal.database import get_session, list_memberships_for_room

        scope = booth_scope
        async with get_session() as s:
            members_before = await list_memberships_for_room(s, scope["room_b"])

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_b']}/members/",
                data={"email": "new-coord@example.com", "role": "room_coordinator"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            members_after = await list_memberships_for_room(s, scope["room_b"])
            assert len(members_after) == len(members_before)

    @pytest.mark.anyio
    async def test_owner_cross_event_booth_member_invite_rejected(self, booth_scope):
        """Owner A cannot trigger invite for Booth B member via mismatched URL parameters."""
        from portal.database import get_session

        scope = booth_scope
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as sent:
            async with _client() as c:
                resp = await c.post(
                    f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_b']}/members/{scope['membership_b']}/invite",
                    cookies=scope["owner_cookie"],
                    follow_redirects=False,
                )
        assert resp.status_code == 404
        sent.assert_not_awaited()
        async with get_session() as s:
            tokens = (await s.execute(select(AuthToken))).scalars().all()
            assert len(tokens) == 0
        async with get_session() as s:
            tokens = (await s.execute(select(AuthToken))).scalars().all()
            assert len(tokens) == 0

    @pytest.mark.anyio
    async def test_owner_cross_event_room_member_invite_rejected(self, booth_scope):
        """Owner A cannot trigger invite for Room B member via mismatched URL parameters."""
        from portal.database import get_session

        scope = booth_scope
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as sent:
            async with _client() as c:
                resp = await c.post(
                    f"/workspace/events/{scope['event_a']}/rooms/{scope['room_b']}/members/{scope['rm_b']}/invite",
                    cookies=scope["owner_cookie"],
                    follow_redirects=False,
                )
        assert resp.status_code == 404
        sent.assert_not_awaited()
        async with get_session() as s:
            tokens = (await s.execute(select(AuthToken))).scalars().all()
            assert len(tokens) == 0

    @pytest.mark.anyio
    async def test_owner_cross_event_event_member_invite_rejected(self, booth_scope):
        """Owner A cannot trigger invite for Event B member via mismatched URL parameters."""
        from portal.database import get_session

        scope = booth_scope
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as sent:
            async with _client() as c:
                resp = await c.post(
                    f"/workspace/events/{scope['event_a']}/members/{scope['em_b']}/invite",
                    cookies=scope["owner_cookie"],
                    follow_redirects=False,
                )
        assert resp.status_code == 404
        sent.assert_not_awaited()
        async with get_session() as s:
            tokens = (await s.execute(select(AuthToken))).scalars().all()
            assert len(tokens) == 0

    @pytest.mark.anyio
    async def test_legitimate_booth_member_invite_succeeds(self, booth_scope):
        """Owner A can invite a member of a booth in their own event and room."""
        scope = booth_scope
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as sent:
            async with _client() as c:
                resp = await c.post(
                    f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}"
                    f"/members/{scope['membership_a1']}/invite",
                    cookies=scope["owner_cookie"],
                    follow_redirects=False,
                )
        assert resp.status_code == 303
        assert "error=" not in resp.headers["location"]
        sent.assert_awaited_once()
        assert sent.await_args.args[2] == "scope-member-a@example.com"

    @pytest.mark.anyio
    async def test_booth_member_invite_rejects_room_from_another_event(self, booth_scope):
        """A booth recorded under Event A but in Event B's room cannot receive invitations."""
        from portal.database import create_booth, get_session, set_booth_membership

        scope = booth_scope
        async with get_session() as s:
            rogue_booth = await create_booth(
                s,
                event_id=scope["event_a"],
                room_id=scope["room_b"],
                language_code="it",
                language_name="Italian",
            )
            rogue_membership = await set_booth_membership(
                s,
                user_id=scope["secondary_owner_a_id"],
                booth_id=rogue_booth.id,
                role="interpreter",
            )
            rogue_booth_id = rogue_booth.id
            rogue_membership_id = rogue_membership.id

        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as sent:
            async with _client() as c:
                resp = await c.post(
                    f"/workspace/events/{scope['event_a']}/rooms/{scope['room_b']}/booths/{rogue_booth_id}"
                    f"/members/{rogue_membership_id}/invite",
                    cookies=scope["owner_cookie"],
                    follow_redirects=False,
                )
        assert resp.status_code == 404
        sent.assert_not_awaited()

    @pytest.mark.anyio
    async def test_legitimate_token_creation_succeeds(self, booth_scope):
        """Owner A can create a token for Booth A1."""
        from portal.database import get_session, list_tokens_for_booth

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/tokens/",
                data={"role": "interpreter", "label": "fresh token"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert "error=" not in resp.headers["location"]
        async with get_session() as s:
            tokens = await list_tokens_for_booth(s, scope["booth_a1"])
            assert any(t.label == "fresh token" and t.role == "interpreter" for t in tokens)

    @pytest.mark.anyio
    async def test_legitimate_booth_member_add_succeeds(self, booth_scope):
        """Owner A can add a member to Booth A1."""
        from portal.database import get_session, list_memberships_for_booth

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/members/",
                data={"email": "legit-interp@example.com", "role": "interpreter"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert "error=" not in resp.headers["location"]
        async with get_session() as s:
            members = await list_memberships_for_booth(s, scope["booth_a1"])
            assert any(m.user.email == "legit-interp@example.com" and m.role == "interpreter" for m in members)

    @pytest.mark.anyio
    async def test_cross_event_setup_add_booth_rejected(self, booth_scope):
        """Owner A cannot add a booth to Room B via the setup wizard."""
        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import DBBooth

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/setup/booths",
                data={"room_id": str(scope["room_b"]), "language_code": "it", "language_name": "Italian"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert "error=room_not_found" in resp.headers.get("location", "")
        assert "duplicate_language" not in resp.headers.get("location", "")
        async with get_session() as s:
            created = (
                (
                    await s.execute(
                        select(DBBooth).where(DBBooth.room_id == scope["room_b"], DBBooth.language_code == "it")
                    )
                )
                .scalars()
                .first()
            )
            assert created is None

    @pytest.mark.parametrize("room_id", ["999999", "not-a-room", "²", "9" * 100])
    @pytest.mark.anyio
    async def test_setup_add_booth_with_invalid_room_reports_error(self, booth_scope, room_id):
        """The setup wizard reports an unknown room instead of redirecting as if it succeeded."""
        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import DBBooth

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/setup/booths",
                data={"room_id": room_id, "language_code": "it", "language_name": "Italian"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=room_not_found" in resp.headers["location"]
            page = await c.get(resp.headers["location"], cookies=scope["owner_cookie"])
        assert "That room is not part of this event." in page.text
        async with get_session() as s:
            created = (
                (
                    await s.execute(
                        select(DBBooth).where(DBBooth.event_id == scope["event_a"], DBBooth.language_code == "it")
                    )
                )
                .scalars()
                .all()
            )
        assert created == []

    @pytest.mark.anyio
    async def test_setup_add_booth_without_room_reports_error(self, booth_scope):
        """An empty room selection must not look like a successful booth creation."""
        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import DBBooth

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/setup/booths",
                data={"room_id": "", "language_code": "it", "language_name": "Italian"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=room_required" in resp.headers["location"]
            page = await c.get(resp.headers["location"], cookies=scope["owner_cookie"])
        assert "Select a room before adding a booth." in page.text
        async with get_session() as s:
            created = (
                (
                    await s.execute(
                        select(DBBooth).where(DBBooth.event_id == scope["event_a"], DBBooth.language_code == "it")
                    )
                )
                .scalars()
                .all()
            )
        assert created == []

    @pytest.mark.anyio
    async def test_setup_add_booth_without_language_reports_error(self, booth_scope):
        """An empty language selection must not look like a successful booth creation."""
        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import DBBooth

        scope = booth_scope
        async with get_session() as s:
            before = (
                (
                    await s.execute(
                        select(DBBooth).where(DBBooth.room_id == scope["room_a2"], DBBooth.language_code == "it")
                    )
                )
                .scalars()
                .all()
            )
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/setup/booths",
                data={"room_id": str(scope["room_a2"]), "language_code": "", "language_name": "Italian"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=language_required" in resp.headers["location"]
            page = await c.get(resp.headers["location"], cookies=scope["owner_cookie"])
        assert "Choose a language before adding a booth." in page.text
        async with get_session() as s:
            after = (
                (
                    await s.execute(
                        select(DBBooth).where(DBBooth.room_id == scope["room_a2"], DBBooth.language_code == "it")
                    )
                )
                .scalars()
                .all()
            )
        assert len(after) == len(before) == 0

    @pytest.mark.anyio
    async def test_setup_add_booth_create_failure_reports_error(self, booth_scope, monkeypatch):
        """A database failure must be reported instead of looking successful."""
        from sqlalchemy import select
        from sqlalchemy.exc import OperationalError

        from portal.database import get_session
        from portal.models import DBBooth

        async def fail_create(**_kwargs):
            raise OperationalError("INSERT", {}, Exception("simulated booth creation failure"))

        monkeypatch.setattr("portal.routers.admin._create_booth_unless_duplicate", fail_create)
        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/setup/booths",
                data={"room_id": str(scope["room_a2"]), "language_code": "ko", "language_name": "Korean"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=booth_create_failed" in resp.headers["location"]
            page = await c.get(resp.headers["location"], cookies=scope["owner_cookie"])
        assert "The booth could not be created. Please try again." in page.text
        async with get_session() as s:
            created = (
                (
                    await s.execute(
                        select(DBBooth).where(DBBooth.room_id == scope["room_a2"], DBBooth.language_code == "ko")
                    )
                )
                .scalars()
                .all()
            )
        assert created == []

    @pytest.mark.anyio
    async def test_setup_add_booth_valid_then_duplicate(self, booth_scope):
        """Valid wizard creation still works, and duplicates still report duplicate_language."""
        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import DBBooth

        scope = booth_scope
        data = {"room_id": str(scope["room_a2"]), "language_code": "ja", "language_name": "Japanese"}
        async with _client() as c:
            first = await c.post(
                f"/workspace/events/{scope['event_a']}/setup/booths",
                data=data,
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            second = await c.post(
                f"/workspace/events/{scope['event_a']}/setup/booths",
                data=data,
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert first.status_code == 303
        assert "error=" not in first.headers["location"]
        assert f"room_id={scope['room_a2']}" in first.headers["location"]
        assert second.status_code == 303
        assert "error=duplicate_language" in second.headers["location"]
        async with get_session() as s:
            booths = (
                (
                    await s.execute(
                        select(DBBooth).where(DBBooth.room_id == scope["room_a2"], DBBooth.language_code == "ja")
                    )
                )
                .scalars()
                .all()
            )
        assert len(booths) == 1

    @pytest.mark.anyio
    async def test_cross_event_create_booth_rejected(self, booth_scope):
        """Owner A cannot create a booth under Room B via the direct booth creation route."""
        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import DBBooth

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_b']}/booths/",
                data={"language_code": "nl", "language_name": "Dutch"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            created = (
                (
                    await s.execute(
                        select(DBBooth).where(DBBooth.room_id == scope["room_b"], DBBooth.language_code == "nl")
                    )
                )
                .scalars()
                .first()
            )
            assert created is None

    @pytest.mark.anyio
    async def test_cross_event_relay_booth_assignment_rejected(self, booth_scope):
        """Owner A cannot assign a booth from Event B as relay booth for Room A1."""
        from portal.database import get_room_by_id, get_session

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/edit",
                data={"form_section": "relay", "relay_booth_id": str(scope["booth_b"])},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            room = await get_room_by_id(s, scope["room_a1"])
            assert room is not None
            assert room.relay_booth_id != scope["booth_b"]

    @pytest.mark.anyio
    async def test_nonexistent_relay_booth_assignment_rejected(self, booth_scope):
        """Assigning a nonexistent relay booth ID is rejected with 404."""
        from portal.database import get_room_by_id, get_session

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/edit",
                data={"form_section": "relay", "relay_booth_id": "999999"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            room = await get_room_by_id(s, scope["room_a1"])
            assert room is not None
            assert room.relay_booth_id != 999999

    @pytest.mark.anyio
    async def test_same_event_relay_booth_assignment_succeeds(self, booth_scope):
        """Owner A can assign a booth in the same event (same room or cross-room) as relay booth."""
        from portal.database import get_room_by_id, get_session

        scope = booth_scope
        async with _client() as c:
            # 1. Assign same-room booth A1
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/edit",
                data={"form_section": "relay", "relay_booth_id": str(scope["booth_a1"])},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            assert resp.status_code == 303
        async with get_session() as s:
            room = await get_room_by_id(s, scope["room_a1"])
            assert room.relay_booth_id == scope["booth_a1"]

        async with _client() as c:
            # 2. Assign same-event cross-room booth A2
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/edit",
                data={"form_section": "relay", "relay_booth_id": str(scope["booth_a2"])},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            assert resp.status_code == 303
        async with get_session() as s:
            room = await get_room_by_id(s, scope["room_a1"])
            assert room.relay_booth_id == scope["booth_a2"]

        async with _client() as c:
            # 3. Clear relay booth with 'none'
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/edit",
                data={"form_section": "relay", "relay_booth_id": "none"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            assert resp.status_code == 303
        async with get_session() as s:
            room = await get_room_by_id(s, scope["room_a1"])
            assert room.relay_booth_id is None

    @pytest.mark.anyio
    async def test_room_edit_transcription_section_updates_fields(self, booth_scope):
        """Updating room with form_section='transcription' updates transcription settings independently of relay."""
        from portal.database import get_room_by_id, get_session

        scope = booth_scope
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/edit",
                data={
                    "form_section": "transcription",
                    "floor_transcription_enabled": "on",
                    "floor_transcription_provider": "openai",
                    "floor_transcription_model": "whisper-1",
                    "floor_language_code": "es",
                },
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            assert resp.status_code == 303
        async with get_session() as s:
            room = await get_room_by_id(s, scope["room_a1"])
            assert room is not None
            assert room.floor_transcription_enabled is True
            assert room.floor_transcription_provider == "openai"
            assert room.floor_transcription_model == "whisper-1"
            assert room.floor_language_code == "es"

    @pytest.mark.anyio
    async def test_booth_detail_rejects_foreign_event_booth_in_room(self, booth_scope):
        """Event B's booth must not render through Event A's room URL."""
        from portal.database import create_booth, create_invite_token, create_user, get_session, set_booth_membership

        scope = booth_scope
        async with get_session() as s:
            rogue_booth = await create_booth(
                s,
                event_id=scope["event_b"],
                room_id=scope["room_a1"],
                language_code="it",
                language_name="Secret Italian",
            )
            rogue_token = await create_invite_token(s, booth_id=rogue_booth.id, role="interpreter")
            rogue_member = await create_user(s, email="rogue-secret@example.com", display_name="Rogue Member")
            await set_booth_membership(s, user_id=rogue_member.id, booth_id=rogue_booth.id, role="interpreter")
            rogue_booth_id = rogue_booth.id
            rogue_token_val = rogue_token.token

        async with _client() as c:
            resp = await c.get(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{rogue_booth_id}/",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            resp_b = await c.get(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_b']}/",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        assert rogue_token_val not in resp.text
        assert "rogue-secret@example.com" not in resp.text
        assert "Secret Italian" not in resp.text

        assert resp_b.status_code == 404
        assert scope["invite_b"] not in resp_b.text
        assert "scope-member-b@example.com" not in resp_b.text

    @pytest.mark.anyio
    async def test_booth_list_excludes_foreign_event_booth_in_room(self, booth_scope):
        """Event B's booth must not appear in Event A's room booth list."""
        from portal.database import create_booth, get_session

        scope = booth_scope
        async with get_session() as s:
            await create_booth(
                s,
                event_id=scope["event_b"],
                room_id=scope["room_a1"],
                language_code="it",
                language_name="Secret Italian",
            )

        async with _client() as c:
            resp = await c.get(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/",
                cookies=scope["owner_cookie"],
            )
        assert resp.status_code == 200
        assert "Secret Italian" not in resp.text
        assert "English" in resp.text

    @pytest.mark.anyio
    async def test_translation_settings_reject_booth_in_foreign_event_room(self, booth_scope):
        """Event A's booth in Event B's room must not be mutable through that room URL."""
        from portal.database import create_booth, get_booth_by_id, get_session

        scope = booth_scope
        async with get_session() as s:
            rogue_booth = await create_booth(
                s,
                event_id=scope["event_a"],
                room_id=scope["room_b"],
                language_code="it",
                language_name="Rogue Italian",
            )
            rogue_booth_id = rogue_booth.id

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_b']}/booths/"
                f"{rogue_booth_id}/translation-settings",
                data={
                    "translation_enabled": "true",
                    "translation_provider": "local",
                    "translation_model": "nllb-200-distilled-600M",
                },
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            fresh = await get_booth_by_id(s, rogue_booth_id)
            assert fresh is not None
            assert fresh.translation_enabled is False
            assert fresh.translation_provider is None

    @pytest.mark.anyio
    async def test_edit_booth_rejects_booth_in_foreign_event_room(self, booth_scope):
        """Event A's booth in Event B's room must not be editable through that room URL."""
        from portal.database import create_booth, get_booth_by_id, get_session

        scope = booth_scope
        async with get_session() as s:
            rogue_booth = await create_booth(
                s,
                event_id=scope["event_a"],
                room_id=scope["room_b"],
                language_code="it",
                language_name="Rogue Italian",
            )
            rogue_booth_id = rogue_booth.id

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_b']}/booths/{rogue_booth_id}/edit",
                data={"language_name": "Changed Italian"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            fresh = await get_booth_by_id(s, rogue_booth_id)
            assert fresh is not None
            assert fresh.language_name == "Rogue Italian"

    @pytest.mark.anyio
    async def test_transcription_settings_reject_booth_in_foreign_event_room(self, booth_scope):
        """Event A's booth in Event B's room must not be mutable through that room URL."""
        from portal.database import create_booth, get_booth_by_id, get_session

        scope = booth_scope
        async with get_session() as s:
            rogue_booth = await create_booth(
                s,
                event_id=scope["event_a"],
                room_id=scope["room_b"],
                language_code="it",
                language_name="Rogue Italian",
            )
            rogue_booth_id = rogue_booth.id

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_b']}/booths/"
                f"{rogue_booth_id}/transcription-settings",
                data={
                    "transcription_enabled": "true",
                    "transcription_provider": "local",
                    "transcription_model": "base",
                },
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            fresh = await get_booth_by_id(s, rogue_booth_id)
            assert fresh is not None
            assert fresh.transcription_enabled is False
            assert fresh.transcription_model == "tiny"

    @pytest.mark.anyio
    async def test_same_event_booth_mutations_succeed(self, booth_scope):
        """A valid event/room/booth chain still updates booth settings."""
        from portal.database import get_booth_by_id, get_session

        scope = booth_scope
        async with _client() as c:
            edit = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_a1']}/edit",
                data={"language_name": "English Updated"},
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            translation = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/"
                f"{scope['booth_a1']}/translation-settings",
                data={
                    "translation_enabled": "true",
                    "translation_provider": "local",
                    "translation_model": "nllb-200-distilled-600M",
                },
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
            transcription = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/"
                f"{scope['booth_a1']}/transcription-settings",
                data={
                    "transcription_enabled": "true",
                    "transcription_provider": "local",
                    "transcription_model": "base",
                },
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert edit.status_code == 303
        assert translation.status_code == 303
        assert transcription.status_code == 303
        async with get_session() as s:
            fresh = await get_booth_by_id(s, scope["booth_a1"])
            assert fresh is not None
            assert fresh.language_name == "English Updated"
            assert fresh.translation_enabled is True
            assert fresh.translation_provider == "local"
            assert fresh.transcription_enabled is True
            assert fresh.transcription_model == "base"

    @pytest.mark.parametrize(
        ("method", "subpath", "data"),
        [
            ("GET", "", None),
            ("POST", "delete", {}),
            ("POST", "edit", {"language_name": "Hacked"}),
            ("POST", "translation-settings", {"translation_enabled": "true"}),
            ("POST", "transcription-settings", {"transcription_enabled": "true"}),
            ("POST", "tokens/", {"role": "interpreter"}),
            ("POST", "members/", {"email": "intruder@example.com", "role": "interpreter"}),
        ],
        ids=["detail", "delete", "edit", "translation", "transcription", "token", "member"],
    )
    @pytest.mark.anyio
    async def test_booth_hierarchy_routes_reject_mismatched_booth(self, booth_scope, method, subpath, data):
        """All booth endpoints consistently reject mismatched booth hierarchy with 404."""
        scope = booth_scope
        suffix = f"/{subpath}" if subpath else "/"
        path = f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a1']}/booths/{scope['booth_b']}{suffix}"
        async with _client() as c:
            if method == "GET":
                resp = await c.get(path, cookies=scope["owner_cookie"], follow_redirects=False)
            else:
                resp = await c.post(path, data=data, cookies=scope["owner_cookie"], follow_redirects=False)
        assert resp.status_code == 404
