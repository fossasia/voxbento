"""Invalid booth coordinates in legacy API paths are a 400, not an unhandled 500."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fastapi_app import app

client = TestClient(app, raise_server_exceptions=False)

ROUTES = [
    ("delete", "/api/events/{slug}/rooms/1/booths/{lang}"),
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
async def event_with_room():
    from portal.database import configure, create_event, create_room, dispose, get_session, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as session:
        event = await create_event(session, slug="pycon", display_name="PyCon")
        room = await create_room(session, event_id=event.id, display_name="Hall")
        await session.commit()
        room_id = room.id
    yield room_id
    await dispose()


@pytest.mark.anyio
async def test_whip_url_rejects_invalid_language_after_access_check(event_with_room, monkeypatch):
    """With an existing event and a valid credential, the WHIP URL route used to reach make_booth_id and fail."""
    from httpx import ASGITransport, AsyncClient

    from portal.config import settings

    monkeypatch.setattr(settings, "booth_access_token", "secret")
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as ac:
        response = await ac.get(
            f"/api/events/pycon/booths/english/whip-url?participant_id=p1&room_id={event_with_room}",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert "Language code" in response.json()["detail"]
