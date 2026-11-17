"""Deleting a booth, room or event ends what the booth runs in memory, not only its rows (#1310)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_app import app
from portal.auth import create_user_token
from portal.booth_identity import make_booth_id, make_mediamtx_path, parse_booth_id
from portal.database import configure, create_booth, create_event, create_room, dispose, get_session, init_db
from portal.globals import booths
from portal.models import DBBooth, DeveloperAccount, Event, EventMembership, OAuthClient, OAuthToken, Room, User
from portal.roles import EVENT_OWNER, INTERPRETER
from portal.routers import admin, api_v1
from portal.routers.oauth import hash_token
from portal.transcription.providers.base import ProviderConfig
from portal.transcription.worker import TranscriptionWorkerSession, active_workers

# These tests are not about CSRF; their requests carry the token a real page would send.
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("auto_csrf")]

ORIGIN = {"origin": "http://test"}
API_TOKEN = "booth-delete-cleanup-token"
API_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    for booth_id in booths.booth_ids():
        await booths.remove_booth(*parse_booth_id(booth_id))
    await booths.take_changes()
    active_workers.clear()
    await dispose()


@pytest.fixture
async def site():
    """Two events. ``main`` has rooms ``hall`` (es, fr, nl) and ``side`` (de); ``other`` has one room (it).

    Every booth is live except ``nl``, which nobody has opened.
    """
    async with get_session() as s:
        owner = User(email="owner@example.com", display_name="Owner", is_admin=True, email_verified=True)
        event_owner = User(email="event-owner@example.com", display_name="Event owner", email_verified=True)
        s.add_all([owner, event_owner])
        await s.flush()
        main = await create_event(s, slug="main", display_name="Main")
        other = await create_event(s, slug="other", display_name="Other")
        hall = await create_room(s, event_id=main.id, display_name="Hall")
        hall.eventyay_room_id = "hall"
        side = await create_room(s, event_id=main.id, display_name="Side")
        elsewhere = await create_room(s, event_id=other.id, display_name="Elsewhere")
        es = await create_booth(s, event_id=main.id, room_id=hall.id, language_code="es", language_name="Spanish")
        await create_booth(s, event_id=main.id, room_id=hall.id, language_code="fr", language_name="French")
        await create_booth(s, event_id=main.id, room_id=hall.id, language_code="nl", language_name="Dutch")
        await create_booth(s, event_id=main.id, room_id=side.id, language_code="de", language_name="German")
        await create_booth(s, event_id=other.id, room_id=elsewhere.id, language_code="it", language_name="Italian")
        account = DeveloperAccount(user_id=owner.id, status="approved")
        s.add_all(
            [
                account,
                EventMembership(user_id=owner.id, event_id=main.id, role=EVENT_OWNER),
                EventMembership(user_id=event_owner.id, event_id=main.id, role=EVENT_OWNER),
            ]
        )
        await s.flush()
        client = OAuthClient(developer_account_id=account.id, client_id="cleanup-client", name="Cleanup app")
        s.add(client)
        await s.flush()
        s.add(
            OAuthToken(
                client_id=client.id,
                user_id=owner.id,
                event_id=main.id,
                scopes=["events:write", "rooms:write", "booths:write"],
                access_token_hash=hash_token(API_TOKEN),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        ids = {
            "main": main.id,
            "other": other.id,
            "hall": hall.id,
            "side": side.id,
            "elsewhere": elsewhere.id,
            "es": es.id,
        }
    cookies = {"user_token": create_user_token(user_id=owner.id, email=owner.email, is_admin=True)}
    owner_cookies = {"user_token": create_user_token(user_id=event_owner.id, email=event_owner.email)}
    booth_ids = {
        "es": await _make_live("main", ids["hall"], "es"),
        "fr": await _make_live("main", ids["hall"], "fr"),
        "de": await _make_live("main", ids["side"], "de"),
        "it": await _make_live("other", ids["elsewhere"], "it"),
    }
    await booths.take_changes()  # saved for recovery, as the portal's saver would have done
    booth_ids["nl"] = make_booth_id("main", ids["hall"], "nl")
    return {**ids, "cookies": cookies, "owner_cookies": owner_cookies, "booth_ids": booth_ids}


async def _noop(*_args, **_kwargs) -> None:
    return None


async def _make_live(event_slug: str, room_id: int, code: str) -> str:
    """A booth on air: an interpreter, Go Live on, a chat message and a captions worker."""
    booth_id = make_booth_id(event_slug, room_id, code)
    channel = make_mediamtx_path(event_slug, room_id, code)
    participant, _ = await booths.join_participant(
        booth_id, "Ana", INTERPRETER, code, channel, room_id=room_id, joined_by=f"session-{code}"
    )
    await booths.set_broadcast_unlocked(booth_id, True, code, channel)
    await booths.add_chat_message(booth_id, participant.participant_id, "note from the old team", code, channel)
    active_workers[booth_id] = TranscriptionWorkerSession(
        event_slug, code, booth_id, _noop, "local", "tiny", ProviderConfig(api_key=None), room_id=room_id
    )
    return booth_id


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _assert_ended(booth_id: str, changes: dict) -> None:
    assert booths.get_booth_sync(booth_id) is None
    assert booth_id not in active_workers
    # None tells the recovery saver to delete the booth's live-state row.
    assert changes[booth_id] is None


def _assert_running(booth_id: str, changes: dict) -> None:
    booth = booths.get_booth_sync(booth_id)
    assert booth is not None and booth.broadcast_unlocked and booth.chat_messages
    assert booth_id in active_workers
    assert booth_id not in changes


async def test_deleting_a_booth_ends_only_that_booth(site):
    ids = site["booth_ids"]
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{site['main']}/rooms/{site['hall']}/booths/{site['es']}/delete",
            headers=ORIGIN,
            cookies=site["cookies"],
            follow_redirects=False,
        )
    assert resp.status_code == 303
    changes = await booths.take_changes()
    await _assert_ended(ids["es"], changes)
    for code in ("fr", "de", "it"):
        _assert_running(ids[code], changes)


async def test_an_event_owner_deleting_from_the_workspace_ends_the_booth(site):
    ids = site["booth_ids"]
    async with _client() as c:
        resp = await c.post(
            f"/workspace/events/{site['main']}/rooms/{site['hall']}/booths/{site['es']}/delete",
            headers=ORIGIN,
            cookies=site["owner_cookies"],
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/workspace/")
    changes = await booths.take_changes()
    await _assert_ended(ids["es"], changes)
    _assert_running(ids["fr"], changes)


async def test_deleting_a_room_ends_its_booths(site):
    ids = site["booth_ids"]
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{site['main']}/rooms/{site['hall']}/delete",
            headers=ORIGIN,
            cookies=site["cookies"],
            follow_redirects=False,
        )
    assert resp.status_code == 303
    changes = await booths.take_changes()
    for code in ("es", "fr", "nl"):  # nl was never opened: nothing to end, and nothing is created
        await _assert_ended(ids[code], changes)
    for code in ("de", "it"):
        _assert_running(ids[code], changes)


async def test_deleting_an_event_ends_its_booths(site):
    ids = site["booth_ids"]
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{site['main']}/delete", headers=ORIGIN, cookies=site["cookies"], follow_redirects=False
        )
    assert resp.status_code == 303
    changes = await booths.take_changes()
    for code in ("es", "fr", "de"):
        await _assert_ended(ids[code], changes)
    _assert_running(ids["it"], changes)


async def test_deleting_an_event_ends_another_events_booth_in_its_rooms(site):
    """Deleting the event deletes its rooms, so a booth of another event placed in one goes too."""
    async with get_session() as s:
        stray_row = await create_booth(
            s, event_id=site["other"], room_id=site["side"], language_code="pt", language_name="Portuguese"
        )
        stray_row_id = stray_row.id
    stray = await _make_live("other", site["side"], "pt")
    await booths.take_changes()
    async with _client() as c:
        await c.post(f"/admin/events/{site['main']}/delete", headers=ORIGIN, cookies=site["cookies"])
    async with get_session() as s:
        assert await s.get(DBBooth, stray_row_id) is None
    changes = await booths.take_changes()
    await _assert_ended(stray, changes)
    _assert_running(site["booth_ids"]["it"], changes)


async def test_booths_keep_running_until_the_event_delete_succeeds(site, monkeypatch):
    """A rolled-back delete leaves the event in place, so its booths must keep running."""
    ids = site["booth_ids"]
    real_delete = admin.delete_event
    attempts = []

    async def locked_once(session, event_id):
        attempts.append(event_id)
        if len(attempts) == 1:
            raise SQLAlchemyError("database is locked")
        return await real_delete(session, event_id)

    monkeypatch.setattr(admin, "delete_event", locked_once)
    async with _client() as c:
        failed = await c.post(
            f"/admin/events/{site['main']}/delete", headers=ORIGIN, cookies=site["cookies"], follow_redirects=False
        )
        assert failed.headers["location"].endswith("events/?error=delete_failed")
        changes = await booths.take_changes()
        for code in ("es", "fr", "de", "it"):
            _assert_running(ids[code], changes)

        retried = await c.post(
            f"/admin/events/{site['main']}/delete", headers=ORIGIN, cookies=site["cookies"], follow_redirects=False
        )
    assert retried.headers["location"].endswith("/events/")
    async with get_session() as session:
        assert await session.get(Event, site["main"]) is None
    changes = await booths.take_changes()
    for code in ("es", "fr", "de"):
        await _assert_ended(ids[code], changes)
    _assert_running(ids["it"], changes)


async def test_a_booth_added_back_after_a_delete_starts_off_air(site):
    async with _client() as c:
        await c.post(
            f"/admin/events/{site['main']}/rooms/{site['hall']}/booths/{site['es']}/delete",
            headers=ORIGIN,
            cookies=site["cookies"],
        )
        await c.post(
            f"/admin/events/{site['main']}/rooms/{site['hall']}/booths/",
            data={"language_code": "es", "language_name": "Spanish"},
            headers=ORIGIN,
            cookies=site["cookies"],
        )
        state = await c.get(f"/api/events/main/booths/es/state?room_id={site['hall']}", cookies=site["cookies"])
    assert state.status_code == 200
    assert state.json()["broadcast_unlocked"] is False
    assert state.json()["chat_messages"] == []


async def test_room_setup_ends_the_booths_it_removes(site):
    """The guided setup deletes booths no longer wanted; one publishing right now is kept."""
    ids = site["booth_ids"]
    fr = booths.get_booth_sync(ids["fr"])
    channel = make_mediamtx_path("main", site["hall"], "fr")
    await booths.update_participant_state(ids["fr"], fr.active_interpreter_id, "fr", channel, ingest_connected=True)
    await booths.take_changes()
    data = {"display_name": "Hall", "spoken_language": "en", "languages": ["nl"], "delivery_nl": "interpreter"}
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{site['main']}/rooms/{site['hall']}/setup",
            data=data,
            headers=ORIGIN,
            cookies=site["cookies"],
            follow_redirects=False,
        )
    assert resp.status_code == 303 and "kept_live=fr" in resp.headers["location"]
    changes = await booths.take_changes()
    await _assert_ended(ids["es"], changes)
    assert booths.get_booth_sync(ids["fr"]).ingest_status == "connected"
    assert ids["fr"] in active_workers


async def test_api_v1_booth_delete_ends_the_booth(site):
    ids = site["booth_ids"]
    async with _client() as c:
        resp = await c.delete("/api/v1/events/main/rooms/hall/booths/es", headers=API_HEADERS)
    assert resp.status_code == 200
    changes = await booths.take_changes()
    await _assert_ended(ids["es"], changes)
    _assert_running(ids["fr"], changes)


async def test_api_v1_room_delete_stops_its_captions(site):
    ids = site["booth_ids"]
    async with _client() as c:
        resp = await c.delete("/api/v1/events/main/rooms/hall", headers=API_HEADERS)
    assert resp.status_code == 204
    changes = await booths.take_changes()
    for code in ("es", "fr"):
        await _assert_ended(ids[code], changes)
    _assert_running(ids["de"], changes)


async def test_api_v1_event_delete_stops_its_captions(site):
    ids = site["booth_ids"]
    async with _client() as c:
        resp = await c.delete("/api/v1/events/main", headers=API_HEADERS)
    assert resp.status_code == 204
    changes = await booths.take_changes()
    for code in ("es", "fr", "de"):
        await _assert_ended(ids[code], changes)
    _assert_running(ids["it"], changes)
    async with get_session() as s:
        assert await s.get(Room, site["hall"]) is not None  # soft delete: the rows stay


async def test_api_v1_room_delete_that_fails_leaves_its_booths_running(site, monkeypatch):
    ids = site["booth_ids"]

    async def locked(*_args, **_kwargs):
        raise SQLAlchemyError("database is locked")

    monkeypatch.setattr(api_v1, "delete_db_room", locked)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.delete("/api/v1/events/main/rooms/hall", headers=API_HEADERS)
    assert resp.status_code == 500
    changes = await booths.take_changes()
    for code in ("es", "fr"):
        _assert_running(ids[code], changes)


async def test_a_booth_still_leaves_memory_when_stopping_its_captions_fails(site, monkeypatch):
    ids = site["booth_ids"]
    monkeypatch.setattr(admin, "stop_transcription_worker", AsyncMock(side_effect=RuntimeError("stop failed")))
    with pytest.raises(RuntimeError):
        await admin._end_deleted_booths([("main", site["hall"], "es")])
    assert booths.get_booth_sync(ids["es"]) is None


async def _place_stray_booth(site) -> tuple[str, int]:
    """A live booth of the ``other`` event placed in the ``hall`` room of ``main``."""
    async with get_session() as s:
        row = await create_booth(
            s, event_id=site["other"], room_id=site["hall"], language_code="pt", language_name="Portuguese"
        )
        row_id = row.id
    stray = await _make_live("other", site["hall"], "pt")
    await booths.take_changes()
    return stray, row_id


async def test_api_v1_room_delete_ends_another_events_booth_in_the_room(site):
    stray, row_id = await _place_stray_booth(site)
    async with _client() as c:
        resp = await c.delete("/api/v1/events/main/rooms/hall", headers=API_HEADERS)
    assert resp.status_code == 204
    async with get_session() as s:
        assert await s.get(DBBooth, row_id) is None
    changes = await booths.take_changes()
    await _assert_ended(stray, changes)
    _assert_running(site["booth_ids"]["it"], changes)


@pytest.mark.parametrize(
    "path", ["/api/v1/events/main/rooms/hall/booths/es", "/api/v1/events/main/rooms/hall", "/api/v1/events/main"]
)
async def test_api_v1_delete_ends_the_booth_only_after_the_commit(site, monkeypatch, path):
    """A delete that fails to commit is rolled back, so its booths must still be running then."""
    order = []
    real_commit = AsyncSession.commit

    async def commit(self):
        order.append("commit")
        await real_commit(self)

    async def stop(_booth_id):
        order.append("stop")

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(api_v1, "stop_transcription_worker", stop)
    async with _client() as c:
        resp = await c.delete(path, headers=API_HEADERS)
    assert resp.status_code in (200, 204)
    assert "stop" in order and order.index("commit") < order.index("stop")
