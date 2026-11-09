"""OAuth apps and Room coordinators follow the roles table (#757).

Room coordinators (given per event, room or booth) may let an app run live sessions and read,
but never create or delete rooms or booths; that stays with Admins and Event owners.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
    set_booth_membership,
    set_event_membership,
    set_room_membership,
)
from portal.models import DeveloperAccount, OAuthClient, OAuthToken
from portal.roles import EVENT_OWNER, ROOM_COORDINATOR
from portal.routers.oauth import COORDINATOR_SCOPES, VALID_SCOPES, get_effective_scopes, hash_token


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def world():
    async with get_session() as s:
        dev = await create_user(s, email="dev@example.org", display_name="Dev")
        ev = await create_event(s, slug="summit", display_name="Summit")
        room = await create_room(s, event_id=ev.id, display_name="Main Hall")
        booth = await create_booth(s, event_id=ev.id, room_id=room.id, language_code="de", language_name="Deutsch")
        account = DeveloperAccount(user_id=dev.id)
        s.add(account)
        await s.flush()
        public = OAuthClient(
            developer_account_id=account.id,
            client_id="public-app",
            name="Public app",
            is_confidential=False,
            redirect_uris=["http://localhost/callback"],
            scopes_requested=list(VALID_SCOPES),
        )
        s.add(public)
        await s.flush()
        people = {}
        for level in ("event", "room", "booth"):
            user = await create_user(s, email=f"coord-{level}@example.org", display_name=f"Coord {level}")
            if level == "event":
                await set_event_membership(s, user_id=user.id, event_id=ev.id, role=ROOM_COORDINATOR)
            elif level == "room":
                await set_room_membership(s, user_id=user.id, room_id=room.id, role=ROOM_COORDINATOR)
            else:
                await set_booth_membership(s, user_id=user.id, booth_id=booth.id, role=ROOM_COORDINATOR)
            people[level] = user.id
        owner = await create_user(s, email="owner@example.org", display_name="Owner")
        await set_event_membership(s, user_id=owner.id, event_id=ev.id, role=EVENT_OWNER)
        people["owner"] = owner.id
        return {"event": ev.id, "room": room.id, "client": public.id, "people": people}


async def scopes_for(world, who):
    client = SimpleNamespace(is_confidential=False, scopes_requested=list(VALID_SCOPES))
    async with get_session() as s:
        return set(await get_effective_scopes(s, {"sub": str(world["people"][who])}, world["event"], list(VALID_SCOPES), client))


@pytest.mark.anyio
@pytest.mark.parametrize("level", ["event", "room", "booth"])
async def test_coordinators_given_at_any_level_get_only_live_session_scopes(world, level):
    granted = await scopes_for(world, level)
    assert granted == set(COORDINATOR_SCOPES)
    assert not granted & {"rooms:write", "booths:write", "events:write", "webhooks:manage"}


@pytest.mark.anyio
async def test_owners_get_every_scope_and_strangers_none(world):
    assert await scopes_for(world, "owner") == set(VALID_SCOPES)
    async with get_session() as s:
        stranger = await create_user(s, email="stranger@example.org", display_name="Stranger")
    world["people"]["stranger"] = stranger.id
    assert await scopes_for(world, "stranger") == set()


async def bearer(world, who, scopes):
    raw = f"token-{who}-{len(scopes)}"
    async with get_session() as s:
        s.add(
            OAuthToken(
                client_id=world["client"],
                user_id=world["people"][who],
                event_id=world["event"],
                scopes=scopes,
                access_token_hash=hash_token(raw),
                refresh_token_hash=hash_token(raw + "-refresh"),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
    return {"Authorization": f"Bearer {raw}"}


@pytest.mark.anyio
@pytest.mark.parametrize("level", ["event", "room", "booth"])
async def test_api_lets_coordinators_read_live_status_but_not_manage_booths(world, level):
    # Even a token that somehow carries booths:write is refused for a coordinator.
    headers = await bearer(world, level, ["sessions:read", "booths:write"])
    base = f"/api/v1/events/summit/rooms/{world['room']}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        status = await http.get(f"{base}/status", headers=headers)
        create = await http.post(f"{base}/booths/fr", headers=headers)
        delete = await http.delete(f"{base}/booths/de", headers=headers)
    assert status.status_code == 200
    assert create.status_code == 403
    assert delete.status_code == 403


@pytest.mark.anyio
async def test_api_lets_owners_manage_booths(world):
    headers = await bearer(world, "owner", ["booths:write"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        create = await http.post(f"/api/v1/events/summit/rooms/{world['room']}/booths/fr", headers=headers)
    assert create.status_code == 200
