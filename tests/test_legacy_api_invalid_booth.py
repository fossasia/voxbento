"""Booth coordinates in legacy API paths are validated and normalized before they are used."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.config import settings
from portal.database import configure, create_booth, create_event, create_room, dispose, get_session, init_db
from portal.models import DBBooth

client = TestClient(app, raise_server_exceptions=False)

ROUTES = [
    ("delete", "/api/events/{slug}/rooms/1/booths/{lang}"),
    ("delete", "/api/events/{slug}/eventyay-rooms/hall/booths/{lang}"),
    ("post", "/api/events/{slug}/rooms/1/booths/{lang}/transcription/start"),
    ("post", "/api/events/{slug}/rooms/1/booths/{lang}/transcription/stop"),
    ("get", "/api/events/{slug}/booths/{lang}/whip-url?participant_id=p1&room_id=1"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize(
    ("slug", "lang", "message"),
    [
        ("pycon", "english", "Language code"),
        ("Bad_Slug!", "en", "Event slug"),
    ],
)
def test_invalid_booth_coordinates_are_a_bad_request(method, path, slug, lang, message):
    response = getattr(client, method)(path.format(slug=slug, lang=lang))

    assert response.status_code == 400
    assert message in response.json()["detail"]


@pytest.fixture
async def event_with_booth():
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as session:
        event = await create_event(session, slug="pycon", display_name="PyCon")
        room = await create_room(session, event_id=event.id, display_name="Hall")
        await create_booth(session, event_id=event.id, room_id=room.id, language_code="en", language_name="English")
        await session.commit()
        room_id = room.id
    yield room_id
    await dispose()


@pytest.fixture
async def authorized_client(monkeypatch):
    monkeypatch.setattr(settings, "booth_access_token", "secret")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": "Bearer secret"}
    ) as ac:
        yield ac


@pytest.mark.anyio
async def test_whip_url_rejects_invalid_language_after_access_check(event_with_booth, authorized_client):
    """With an existing event and a valid credential, the WHIP URL route used to reach make_booth_id and fail."""
    response = await authorized_client.get(
        f"/api/events/pycon/booths/english/whip-url?participant_id=p1&room_id={event_with_booth}"
    )

    assert response.status_code == 400
    assert "Language code" in response.json()["detail"]


@pytest.mark.anyio
async def test_delete_booth_uses_the_normalized_language_code(event_with_booth, authorized_client):
    """An uppercase code in the path deletes the stored (lowercase) booth, not only the in-memory one."""
    response = await authorized_client.delete(f"/api/events/PyCon/rooms/{event_with_booth}/booths/EN")

    assert response.status_code == 204
    async with get_session() as session:
        assert await session.scalar(select(DBBooth).where(DBBooth.room_id == event_with_booth)) is None
