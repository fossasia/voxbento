from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    dispose,
    get_session,
    init_db,
    set_event_membership,
)
from portal.models import User
from portal.roles import INTERPRETER


@pytest.fixture
async def interpreter():
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as s:
        user = User(email="booth-path@example.com", display_name="Booth Path", email_verified=True)
        s.add(user)
        event = await create_event(s, slug="pycon2026", display_name="PyCon India 2026")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        await create_booth(s, event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
        await s.flush()
        await set_event_membership(s, user_id=user.id, event_id=event.id, role=INTERPRETER)
        yield {"user_token": create_user_token(user_id=user.id, email=user.email)}, room.id
    await dispose()


def client(cookies: dict[str, str]) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookies)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path", ["pycon2026/{room}/french", "pycon2026/{room}/zz", "py con/{room}/fr", "pycon_2026/{room}/fr"]
)
async def test_a_booth_address_that_cannot_exist_is_not_found(interpreter, path):
    cookies, room_id = interpreter

    async with client(cookies) as c:
        res = await c.get(f"/interpreter/{path.format(room=room_id)}")

    assert res.status_code == 404
    assert "There is no booth at this address." in res.text


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["pycon2026/{room}/FR", "PyCon2026/{room}/fr", "PYCON2026/{room}/Fr"])
async def test_capitals_in_a_booth_address_lead_to_the_booth(interpreter, path):
    cookies, room_id = interpreter

    async with client(cookies) as c:
        moved = await c.get(f"/interpreter/{path.format(room=room_id)}?language=French", follow_redirects=False)
        page = await c.get(f"/interpreter/{path.format(room=room_id)}", follow_redirects=True)

    assert moved.status_code == 308
    assert moved.headers["location"] == f"/interpreter/pycon2026/{room_id}/fr?language=French"
    assert page.status_code == 200
    assert "French" in page.text
    assert "Main Hall" in page.text


@pytest.mark.anyio
async def test_the_booth_address_as_given_still_opens_the_booth(interpreter):
    cookies, room_id = interpreter

    async with client(cookies) as c:
        res = await c.get(f"/interpreter/pycon2026/{room_id}/fr", follow_redirects=False)

    assert res.status_code == 200
    assert "Main Hall" in res.text


@pytest.mark.anyio
async def test_signing_in_is_still_asked_first(interpreter):
    async with client({}) as c:
        res = await c.get("/interpreter/pycon2026/1/french", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"].startswith("/login?next=")
