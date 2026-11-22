"""Tests for GET /api/v1/events/{event_slug}/attendance (#1362).

Verifies OAuth scope enforcement, RBAC, live and persisted visit merging,
filtering by day, room, and language, pagination, and CSV export with formula sanitization.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.attendance import Visit
from portal.database import (
    configure,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
)
from portal.globals import booths
from portal.models import (
    DBBooth,
    DeveloperAccount,
    Event,
    EventMembership,
    InterpreterAttendance,
    OAuthClient,
    OAuthToken,
    Room,
    User,
)

ACCESS_TOKEN = "test_attendance_oauth_token"
OTHER_ACCESS_TOKEN = "other_attendance_oauth_token"


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    # Clean up attendance tracker state between tests
    booths.attendance._open.clear()
    booths.attendance._closed.clear()
    yield
    await dispose()
    booths.attendance._open.clear()
    booths.attendance._closed.clear()


def _client():
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")


@pytest.fixture
async def seeded_attendance_environment():
    async with get_session() as session:
        owner = await create_user(session, email="owner@example.com", display_name="Event Owner")
        interpreter_user = await create_user(
            session, email="interpreter@example.com", display_name="Maria Interpreter"
        )

        dev_account = DeveloperAccount(user_id=owner.id, status="approved")
        session.add(dev_account)
        await session.flush()

        oauth_client = OAuthClient(
            developer_account_id=dev_account.id,
            client_id="attendance_test_client",
            name="Attendance Client",
            is_confidential=False,
            redirect_uris=["http://localhost/callback"],
            scopes_requested=["events:read", "rooms:read"],
        )
        session.add(oauth_client)
        await session.flush()

        event = await create_event(session, slug="conf2026", display_name="Conference 2026")
        session.add(EventMembership(user_id=owner.id, event_id=event.id, role="event_owner"))

        other_event = await create_event(session, slug="otherconf", display_name="Other Conference")
        session.add(EventMembership(user_id=owner.id, event_id=other_event.id, role="event_owner"))

        room = await create_room(session, event_id=event.id, display_name="Hall A")
        booth = DBBooth(event_id=event.id, room_id=room.id, language_code="es", language_name="Spanish")
        session.add(booth)
        await session.flush()

        # Valid token with events:read for event
        token = OAuthToken(
            client_id=oauth_client.id,
            user_id=owner.id,
            event_id=event.id,
            scopes=["events:read"],
            access_token_hash=hashlib.sha256(ACCESS_TOKEN.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        session.add(token)

        # Token with wrong scope (rooms:read only)
        token_wrong_scope = OAuthToken(
            client_id=oauth_client.id,
            user_id=owner.id,
            event_id=event.id,
            scopes=["rooms:read"],
            access_token_hash=hashlib.sha256("wrong_scope_token".encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        session.add(token_wrong_scope)

        # Token scoped to other_event
        token_other_event = OAuthToken(
            client_id=oauth_client.id,
            user_id=owner.id,
            event_id=other_event.id,
            scopes=["events:read"],
            access_token_hash=hashlib.sha256(OTHER_ACCESS_TOKEN.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        session.add(token_other_event)

        await session.commit()

        return {
            "event_slug": event.slug,
            "other_event_slug": other_event.slug,
            "room_id": room.id,
            "booth_id": booth.id,
            "owner_id": owner.id,
            "interpreter_id": interpreter_user.id,
            "token": ACCESS_TOKEN,
            "wrong_scope_token": "wrong_scope_token",
            "other_token": OTHER_ACCESS_TOKEN,
        }


@pytest.mark.anyio
async def test_attendance_unauthenticated_returns_401(seeded_attendance_environment):
    env = seeded_attendance_environment
    async with _client() as client:
        res = await client.get(f"/api/v1/events/{env['event_slug']}/attendance")
    assert res.status_code == 401


@pytest.mark.anyio
async def test_attendance_missing_scope_returns_403(seeded_attendance_environment):
    env = seeded_attendance_environment
    headers = {"Authorization": f"Bearer {env['wrong_scope_token']}"}
    async with _client() as client:
        res = await client.get(f"/api/v1/events/{env['event_slug']}/attendance", headers=headers)
    assert res.status_code == 403


@pytest.mark.anyio
async def test_attendance_mismatched_event_token_returns_404(seeded_attendance_environment):
    # IDOR check: token is for other_event, but querying event_slug
    env = seeded_attendance_environment
    headers = {"Authorization": f"Bearer {env['other_token']}"}
    async with _client() as client:
        res = await client.get(f"/api/v1/events/{env['event_slug']}/attendance", headers=headers)
    assert res.status_code == 404
    assert res.json()["detail"] == "Event not found"


@pytest.mark.anyio
async def test_attendance_soft_deleted_event_returns_404(seeded_attendance_environment):
    env = seeded_attendance_environment
    async with get_session() as session:
        evt = (
            await session.execute(select(Event).where(Event.slug == env["event_slug"]))
        ).scalars().first()
        evt.deleted_at = datetime.now(timezone.utc)
        await session.commit()

    headers = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as client:
        res = await client.get(f"/api/v1/events/{env['event_slug']}/attendance", headers=headers)
    assert res.status_code == 404


@pytest.mark.anyio
async def test_attendance_empty_returns_zero_metrics(seeded_attendance_environment):
    env = seeded_attendance_environment
    headers = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as client:
        res = await client.get(f"/api/v1/events/{env['event_slug']}/attendance", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["event_slug"] == env["event_slug"]
    assert data["summary"]["total_visits"] == 0
    assert data["summary"]["unique_interpreters"] == 0
    assert data["summary"]["total_present_seconds"] == 0
    assert data["summary"]["total_interpreting_seconds"] == 0
    assert data["pagination"]["total"] == 0
    assert data["visits"] == []


@pytest.mark.anyio
async def test_attendance_persisted_records_and_metrics(seeded_attendance_environment):
    env = seeded_attendance_environment
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        evt = (await session.execute(select(Event).where(Event.slug == env["event_slug"]))).scalars().first()
        v1 = InterpreterAttendance(
            id="visit_101",
            event_id=evt.id,
            room_id=env["room_id"],
            language_code="es",
            user_id=env["interpreter_id"],
            display_name="Maria Interpreter",
            role="interpreter",
            joined_at=now - timedelta(hours=2),
            left_at=now - timedelta(hours=1),
            last_seen_at=now - timedelta(hours=1),
            interpreting_seconds=1800,
        )
        session.add(v1)
        await session.commit()

    headers = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as client:
        res = await client.get(f"/api/v1/events/{env['event_slug']}/attendance", headers=headers)

    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["total_visits"] == 1
    assert data["summary"]["unique_interpreters"] == 1
    assert data["summary"]["total_present_seconds"] == 3600
    assert data["summary"]["total_interpreting_seconds"] == 1800
    assert len(data["visits"]) == 1

    visit = data["visits"][0]
    assert visit["id"] == "visit_101"
    assert visit["room_id"] == env["room_id"]
    assert visit["language_code"] == "es"
    assert visit["display_name"] == "Maria Interpreter"
    assert visit["user_id"] == env["interpreter_id"]
    assert visit["is_active"] is False
    assert visit["present_seconds"] == 3600
    assert visit["interpreting_seconds"] == 1800


@pytest.mark.anyio
async def test_attendance_merges_live_in_memory_visits(seeded_attendance_environment):
    env = seeded_attendance_environment
    now = datetime.now(timezone.utc)

    # Add a live visit directly to booths.attendance._open
    live_visit = Visit(
        id="live_visit_202",
        booth_id=f"{env['event_slug']}-{env['room_id']}-es",
        event_slug=env["event_slug"],
        room_id=env["room_id"],
        language_code="es",
        participant_id="part_abc",
        user_id=env["interpreter_id"],
        invite_public_id=None,
        display_name="Live Maria",
        role="interpreter",
        joined_at=now - timedelta(minutes=30),
        interpreting_seconds=900,
        interpreting_since=now - timedelta(minutes=5),
    )
    booths.attendance._open[(live_visit.booth_id, live_visit.participant_id)] = live_visit

    headers = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as client:
        res = await client.get(f"/api/v1/events/{env['event_slug']}/attendance", headers=headers)

    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["total_visits"] == 1
    visit = data["visits"][0]
    assert visit["id"] == "live_visit_202"
    assert visit["is_active"] is True
    assert visit["left_at"] is None
    # 30 minutes present = ~1800s
    assert 1790 <= visit["present_seconds"] <= 1810
    # 900s + 5m (300s) = ~1200s
    assert 1190 <= visit["interpreting_seconds"] <= 1210


@pytest.mark.anyio
async def test_attendance_live_visit_deduplicates_against_persisted_row(seeded_attendance_environment):
    env = seeded_attendance_environment
    now = datetime.now(timezone.utc)

    # Same ID in both DB and in-memory tracker (flushed earlier while still open)
    async with get_session() as session:
        evt = (await session.execute(select(Event).where(Event.slug == env["event_slug"]))).scalars().first()
        v_db = InterpreterAttendance(
            id="shared_visit_303",
            event_id=evt.id,
            room_id=env["room_id"],
            language_code="es",
            user_id=env["interpreter_id"],
            display_name="Maria Flushed",
            role="interpreter",
            joined_at=now - timedelta(minutes=20),
            left_at=None,
            last_seen_at=now - timedelta(minutes=5),
            interpreting_seconds=300,
        )
        session.add(v_db)
        await session.commit()

    live_visit = Visit(
        id="shared_visit_303",
        booth_id=f"{env['event_slug']}-{env['room_id']}-es",
        event_slug=env["event_slug"],
        room_id=env["room_id"],
        language_code="es",
        participant_id="part_shared",
        user_id=env["interpreter_id"],
        invite_public_id=None,
        display_name="Maria Live Updated",
        role="interpreter",
        joined_at=now - timedelta(minutes=20),
        interpreting_seconds=600,
    )
    booths.attendance._open[(live_visit.booth_id, live_visit.participant_id)] = live_visit

    headers = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as client:
        res = await client.get(f"/api/v1/events/{env['event_slug']}/attendance", headers=headers)

    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["total_visits"] == 1  # Not 2!
    visit = data["visits"][0]
    assert visit["id"] == "shared_visit_303"
    assert visit["display_name"] == "Maria Live Updated"
    assert visit["interpreting_seconds"] == 600


@pytest.mark.anyio
async def test_attendance_filtering_by_day_room_and_language(seeded_attendance_environment):
    env = seeded_attendance_environment

    async with get_session() as session:
        evt = (await session.execute(select(Event).where(Event.slug == env["event_slug"]))).scalars().first()
        room_b = await create_room(session, event_id=evt.id, display_name="Hall B")

        # Row 1: Target day, room A, es
        session.add(
            InterpreterAttendance(
                id="match_row",
                event_id=evt.id,
                room_id=env["room_id"],
                language_code="es",
                display_name="Matched Interpreter",
                role="interpreter",
                joined_at=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
                left_at=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc),
                last_seen_at=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc),
                interpreting_seconds=3600,
            )
        )
        # Row 2: Target day, room B, fr
        session.add(
            InterpreterAttendance(
                id="room_b_row",
                event_id=evt.id,
                room_id=room_b.id,
                language_code="fr",
                display_name="French Interpreter",
                role="interpreter",
                joined_at=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
                left_at=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc),
                last_seen_at=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc),
                interpreting_seconds=3600,
            )
        )
        # Row 3: Other day, room A, es
        session.add(
            InterpreterAttendance(
                id="other_day_row",
                event_id=evt.id,
                room_id=env["room_id"],
                language_code="es",
                display_name="Next Day Interpreter",
                role="interpreter",
                joined_at=datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc),
                left_at=datetime(2026, 11, 21, 11, 0, tzinfo=timezone.utc),
                last_seen_at=datetime(2026, 11, 21, 11, 0, tzinfo=timezone.utc),
                interpreting_seconds=3600,
            )
        )
        await session.commit()

    headers = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as client:
        # Filter by day only
        r_day = await client.get(
            f"/api/v1/events/{env['event_slug']}/attendance?day=2026-11-20", headers=headers
        )
        assert r_day.status_code == 200
        assert r_day.json()["summary"]["total_visits"] == 2

        # Filter by day and room_id
        r_room = await client.get(
            f"/api/v1/events/{env['event_slug']}/attendance?day=2026-11-20&room_id={env['room_id']}",
            headers=headers,
        )
        assert r_room.status_code == 200
        assert r_room.json()["summary"]["total_visits"] == 1
        assert r_room.json()["visits"][0]["id"] == "match_row"

        # Filter by language_code
        r_lang = await client.get(
            f"/api/v1/events/{env['event_slug']}/attendance?language_code=fr", headers=headers
        )
        assert r_lang.status_code == 200
        assert r_lang.json()["summary"]["total_visits"] == 1
        assert r_lang.json()["visits"][0]["id"] == "room_b_row"


@pytest.mark.anyio
async def test_attendance_pagination(seeded_attendance_environment):
    env = seeded_attendance_environment
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        evt = (await session.execute(select(Event).where(Event.slug == env["event_slug"]))).scalars().first()
        for i in range(5):
            session.add(
                InterpreterAttendance(
                    id=f"page_visit_{i}",
                    event_id=evt.id,
                    room_id=env["room_id"],
                    language_code="es",
                    display_name=f"Interpreter {i}",
                    role="interpreter",
                    joined_at=now - timedelta(hours=5 - i),
                    left_at=now - timedelta(hours=4 - i),
                    last_seen_at=now - timedelta(hours=4 - i),
                    interpreting_seconds=1000,
                )
            )
        await session.commit()

    headers = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as client:
        # Page 1: limit 2, offset 0
        p1 = await client.get(
            f"/api/v1/events/{env['event_slug']}/attendance?limit=2&offset=0", headers=headers
        )
        assert p1.status_code == 200
        d1 = p1.json()
        assert d1["pagination"]["total"] == 5
        assert d1["pagination"]["limit"] == 2
        assert d1["pagination"]["offset"] == 0
        assert [v["id"] for v in d1["visits"]] == ["page_visit_0", "page_visit_1"]

        # Page 2: limit 2, offset 2
        p2 = await client.get(
            f"/api/v1/events/{env['event_slug']}/attendance?limit=2&offset=2", headers=headers
        )
        assert p2.status_code == 200
        d2 = p2.json()
        assert [v["id"] for v in d2["visits"]] == ["page_visit_2", "page_visit_3"]


@pytest.mark.anyio
async def test_attendance_csv_export_and_formula_sanitization(seeded_attendance_environment):
    env = seeded_attendance_environment
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        evt = (await session.execute(select(Event).where(Event.slug == env["event_slug"]))).scalars().first()
        # Interpreter name starting with formula trigger '='
        session.add(
            InterpreterAttendance(
                id="csv_formula_row",
                event_id=evt.id,
                room_id=env["room_id"],
                language_code="es",
                display_name="=cmd|' /C calc'!A0",
                role="interpreter",
                joined_at=now - timedelta(minutes=60),
                left_at=now,
                last_seen_at=now,
                interpreting_seconds=1800,
            )
        )
        await session.commit()

    headers = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as client:
        res = await client.get(
            f"/api/v1/events/{env['event_slug']}/attendance?format=csv", headers=headers
        )

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in res.headers["content-disposition"]
    assert res.headers["cache-control"] == "no-store"

    content = res.content
    # Must start with UTF-8 BOM
    assert content.startswith(b"\xef\xbb\xbf")

    text = content.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0] == "Interpreter,Account or invite link,Room ID,Language,Joined (UTC),Left (UTC),Minutes in booth,Minutes interpreting"

    # Formula prefix '=' must be escaped with single quote "'"
    data_line = lines[1]
    assert data_line.startswith("'=cmd")
