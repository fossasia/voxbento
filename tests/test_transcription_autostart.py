from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from portal.transcription.worker import ensure_booth_transcription
from portal.websockets.manager import Session, _handle_update_state


@pytest.mark.anyio
async def test_ensure_booth_transcription_starts_when_enabled():
    booth = MagicMock()
    booth.transcription_enabled = True
    booth.transcription_provider = "local"
    booth.transcription_model = "tiny"
    booth.event = MagicMock()

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=booth)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("portal.database.get_session", return_value=session),
        patch("portal.transcription.worker.start_transcription_worker", new_callable=AsyncMock) as start,
        patch("portal.transcription.providers.base.get_api_key", return_value=None),
    ):
        await ensure_booth_transcription("baremini-29-hi")

    start.assert_awaited_once()
    args = start.await_args.args
    assert args[0] == "baremini"
    assert args[1] == "hi"
    assert args[2] == "baremini-29-hi"
    assert start.await_args.kwargs.get("room_id") == 29


@pytest.mark.anyio
async def test_ensure_booth_transcription_skips_when_disabled():
    booth = MagicMock()
    booth.transcription_enabled = False

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=booth)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("portal.database.get_session", return_value=session),
        patch("portal.transcription.worker.start_transcription_worker", new_callable=AsyncMock) as start,
    ):
        await ensure_booth_transcription("baremini-29-hi")

    start.assert_not_awaited()


@pytest.mark.anyio
async def test_ingest_connected_starts_caption_worker():
    ws = MagicMock()
    ws.send_text = AsyncMock()
    session = Session(booth_id="baremini-29-hi", participant_id="p1", language="hi", channel_id="baremini/29/hi")
    scheduled = []

    def capture(coro, **kwargs):
        scheduled.append(coro)
        return MagicMock()

    ensure = AsyncMock()
    with (
        patch("portal.globals.booths.update_participant_state", new_callable=AsyncMock, return_value={}),
        patch("portal.websockets.manager.manager.broadcast", new_callable=AsyncMock),
        patch("portal.transcription.worker.ensure_booth_transcription", ensure),
        patch("portal.websockets.manager.asyncio.create_task", side_effect=capture),
    ):
        await _handle_update_state(ws, session, {"ingest_connected": True, "mic_active": True})
        assert len(scheduled) == 1
        await scheduled[0]

    ensure.assert_awaited_once_with("baremini-29-hi")


@pytest.mark.anyio
async def test_ingest_disconnected_stops_caption_worker():
    ws = MagicMock()
    ws.send_text = AsyncMock()
    session = Session(booth_id="baremini-29-hi", participant_id="p1", language="hi", channel_id="baremini/29/hi")
    scheduled = []

    def capture(coro, **kwargs):
        scheduled.append(coro)
        return MagicMock()

    stop = AsyncMock()
    with (
        patch("portal.globals.booths.update_participant_state", new_callable=AsyncMock, return_value={}),
        patch("portal.websockets.manager.manager.broadcast", new_callable=AsyncMock),
        patch("portal.transcription.worker.stop_transcription_worker", stop),
        patch("portal.websockets.manager.asyncio.create_task", side_effect=capture),
    ):
        await _handle_update_state(ws, session, {"ingest_connected": False})
        assert len(scheduled) == 1
        await scheduled[0]

    stop.assert_awaited_once_with("baremini-29-hi")


@pytest.mark.anyio
async def test_mic_only_update_does_not_start_or_stop_worker():
    ws = MagicMock()
    ws.send_text = AsyncMock()
    session = Session(booth_id="baremini-29-hi", participant_id="p1", language="hi", channel_id="baremini/29/hi")
    scheduled = []

    def capture(coro, **kwargs):
        scheduled.append(coro)
        return MagicMock()

    with (
        patch("portal.globals.booths.update_participant_state", new_callable=AsyncMock, return_value={}),
        patch("portal.websockets.manager.manager.broadcast", new_callable=AsyncMock),
        patch("portal.websockets.manager.asyncio.create_task", side_effect=capture),
    ):
        await _handle_update_state(ws, session, {"mic_active": True})

    assert scheduled == []


@pytest.fixture
async def autostart_db():
    from portal.database import configure, dispose, get_session, init_db
    from portal.models import DBBooth, Event, Room

    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as session:
        event = Event(slug="baremini", display_name="Baremini")
        session.add(event)
        await session.flush()
        room = Room(event_id=event.id, display_name="YouTube Stage", eventyay_room_id="90")
        session.add(room)
        await session.flush()
        booth = DBBooth(
            event_id=event.id,
            room_id=room.id,
            language_code="hi",
            language_name="Hindi",
            transcription_enabled=True,
            transcription_provider="local",
            transcription_model="tiny",
        )
        session.add(booth)
        await session.flush()
        yield {"event": event, "room": room, "booth": booth}
    await dispose()


@pytest.mark.anyio
async def test_ensure_starts_worker_for_db_booth(autostart_db):
    from portal.booth_identity import make_booth_id

    room = autostart_db["room"]
    booth_id = make_booth_id("baremini", room.id, "hi")
    with patch("portal.transcription.worker.start_transcription_worker", new_callable=AsyncMock) as start:
        await ensure_booth_transcription(booth_id)
    start.assert_awaited_once()
    assert start.await_args.args[2] == booth_id
    assert start.await_args.kwargs.get("room_id") == room.id


@pytest.mark.anyio
async def test_http_start_rejects_empty_bearer_when_access_token_required(autostart_db, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app
    from portal.config import settings

    monkeypatch.setattr(settings, "booth_access_token", "prod-secret")
    room = autostart_db["room"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/events/baremini/rooms/{room.id}/booths/hi/transcription/start",
            headers={"Authorization": "Bearer "},
            json={"event_slug": "baremini", "language_code": "hi"},
        )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_http_start_succeeds_with_user_jwt_cookie(autostart_db, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app
    from portal.auth import create_user_token
    from portal.config import settings

    monkeypatch.setattr(settings, "booth_access_token", "prod-secret")
    room = autostart_db["room"]
    tok = create_user_token(user_id=1, email="sirohi@test.com", is_admin=True)
    with patch("portal.routers.api.start_transcription_worker", new_callable=AsyncMock) as start:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/events/baremini/rooms/{room.id}/booths/hi/transcription/start",
                cookies={"user_token": tok},
                json={"event_slug": "baremini", "language_code": "hi"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "started"
        start.assert_awaited_once()


@pytest.mark.anyio
async def test_http_start_succeeds_with_interpreter_jwt_header(autostart_db, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app
    from portal.auth import create_participant_token
    from portal.config import settings

    monkeypatch.setattr(settings, "booth_access_token", "prod-secret")
    room = autostart_db["room"]
    tok = create_participant_token(
        booth_id=autostart_db["booth"].id,
        role="interpreter",
        event_slug="baremini",
        room_id=room.id,
        language_code="hi",
    )
    with patch("portal.routers.api.start_transcription_worker", new_callable=AsyncMock) as start:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/events/baremini/rooms/{room.id}/booths/hi/transcription/start",
                headers={"Authorization": f"Bearer {tok}"},
                json={"event_slug": "baremini", "language_code": "hi"},
            )
        assert response.status_code == 200, response.text
        start.assert_awaited_once()
