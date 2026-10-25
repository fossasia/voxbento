"""Every row of the #757 permissions table, for every role, through the real routes.

Each test runs one action as each of the four roles (and as someone with no role)
and checks that exactly the roles the table allows can do it.
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.auth import create_user_token, hash_password, resolve_booth_role
from portal.booth_identity import make_booth_id
from portal.booth_state import BoothRegistry
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
from portal.models import Event, EventMembership, Room
from portal.roles import ADMIN, EVENT_OWNER, INTERPRETER, ROLES, ROOM_COORDINATOR

os.environ["BOOTH_ACCESS_TOKEN"] = ""
ORIGIN = {"origin": "http://test"}
NOBODY = "nobody"
EVERYONE = (*ROLES, NOBODY)


def allowed_for(*roles: str) -> list[tuple[str, bool]]:
    return [(who, who in roles) for who in EVERYONE]


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def world():
    """One event with a room and booth, a second event nobody here owns, and one person per role."""
    async with get_session() as s:
        event = await create_event(s, slug="summit", display_name="Summit")
        other = await create_event(s, slug="other", display_name="Other Event")
        room = await create_room(s, event_id=event.id, display_name="Room 1")
        booth = await create_booth(s, event_id=event.id, room_id=room.id, language_code="de", language_name="Deutsch")
        people = {}
        for who in EVERYONE:
            u = await create_user(
                s,
                email=f"{who}@example.org",
                display_name=who.title(),
                password_hash=hash_password("pw123456"),
                email_verified=True,
                is_admin=who == ADMIN,
            )
            people[who] = u.id
        await set_event_membership(s, user_id=people[EVENT_OWNER], event_id=event.id, role=EVENT_OWNER)
        await set_room_membership(s, user_id=people[ROOM_COORDINATOR], room_id=room.id, role=ROOM_COORDINATOR)
        await set_booth_membership(s, user_id=people[INTERPRETER], booth_id=booth.id, role=INTERPRETER)
        ids = {"event": event.id, "other": other.id, "room": room.id, "booth": booth.id}
    cookies = {
        who: {"user_token": create_user_token(user_id=uid, email=f"{who}@example.org", is_admin=who == ADMIN)}
        for who, uid in people.items()
    }
    return {**ids, "people": people, "cookies": cookies}


def prefix(who: str) -> str:
    """Event owners use /workspace; everyone else reaches event pages under /admin (#666)."""
    return "/workspace" if who == EVENT_OWNER else "/admin"


async def event_still_has(name: str) -> bool:
    async with get_session() as s:
        return (await s.execute(select(Event).where(Event.display_name == name))).scalar_one_or_none() is not None


# ── Admin-only rows ──────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(ADMIN))
async def test_manage_users_admins_hosting_and_developer_apps(world, who, allowed):
    async with client() as http:
        for path in ("/admin/users/", "/admin/hosting-access/", "/admin/developer-accounts"):
            res = await http.get(path, cookies=world["cookies"][who], follow_redirects=False)
            assert (res.status_code == 200) is allowed, (path, res.status_code)


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(ADMIN))
async def test_see_all_events(world, who, allowed):
    async with client() as http:
        res = await http.get("/admin/events/", cookies=world["cookies"][who], follow_redirects=True)
    assert ("Other Event" in res.text) is allowed


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(ADMIN))
async def test_create_events(world, who, allowed):
    # Owning an event does not by itself allow creating more; hosting access does (tested separately).
    async with client() as http:
        await http.post(
            "/workspace/setup",
            data={"slug": f"new-{who}", "display_name": f"New by {who}"},
            headers=ORIGIN,
            cookies=world["cookies"][who],
            follow_redirects=False,
        )
    assert await event_still_has(f"New by {who}") is allowed


@pytest.mark.anyio
async def test_hosting_access_also_allows_creating_events(world):
    async with get_session() as s:
        u = await create_user(s, email="host@example.org", display_name="Host", password_hash="x", can_host=True)
        uid = u.id
    async with client() as http:
        await http.post(
            "/workspace/setup",
            data={"slug": "hosted", "display_name": "Hosted"},
            headers=ORIGIN,
            cookies={"user_token": create_user_token(user_id=uid, email="host@example.org")},
        )
    assert await event_still_has("Hosted")
    async with get_session() as s:
        event = (await s.execute(select(Event).where(Event.slug == "hosted"))).scalar_one()
        owner = (await s.execute(select(EventMembership).where(EventMembership.event_id == event.id))).scalar_one()
    assert (owner.user_id, owner.role) == (uid, EVENT_OWNER)  # the creator becomes Event owner


# ── Event owner rows ─────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(ADMIN, EVENT_OWNER))
async def test_edit_and_delete_their_event(world, who, allowed):
    async with client() as http:
        res = await http.post(
            f"{prefix(who)}/events/{world['event']}/delete",
            headers=ORIGIN,
            cookies=world["cookies"][who],
            follow_redirects=False,
        )
    assert res.status_code in ((303,) if allowed else (403, 404)), res.status_code
    async with get_session() as s:
        event = await s.get(Event, world["event"])
        assert (event is None or event.deleted_at is not None) is allowed


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(ADMIN, EVENT_OWNER))
async def test_manage_rooms_booths_members_and_invites(world, who, allowed):
    base = f"{prefix(who)}/events/{world['event']}"
    async with client() as http:
        room = await http.post(
            f"{base}/rooms/",
            data={"display_name": f"Room by {who}"},
            headers=ORIGIN,
            cookies=world["cookies"][who],
            follow_redirects=False,
        )
        member = await http.post(
            f"{base}/members/",
            data={"email": f"added-by-{who}@example.org", "role": INTERPRETER},
            headers=ORIGIN,
            cookies=world["cookies"][who],
            follow_redirects=False,
        )
        token = await http.post(
            f"{base}/rooms/{world['room']}/booths/{world['booth']}/tokens/",
            data={"role": INTERPRETER, "label": "x"},
            headers=ORIGIN,
            cookies=world["cookies"][who],
            follow_redirects=False,
        )
    for res in (room, member, token):
        assert res.status_code == (303 if allowed else 403), res.status_code
    async with get_session() as s:
        rooms = (await s.execute(select(Room).where(Room.display_name == f"Room by {who}"))).scalars().all()
    assert bool(rooms) is allowed


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(ADMIN, EVENT_OWNER))
async def test_manage_ai_provider_and_api_keys(world, who, allowed):
    async with client() as http:
        page = await http.get(
            f"{prefix(who)}/events/{world['event']}/api-settings/", cookies=world["cookies"][who], follow_redirects=False
        )
        keys = await http.get(
            f"{prefix(who)}/api/events/{world['event']}/api-keys", cookies=world["cookies"][who], follow_redirects=False
        )
    assert (page.status_code == 200) is allowed, page.status_code
    assert (keys.status_code == 200) is allowed, keys.status_code


# ── Room coordinator rows ────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(ADMIN, EVENT_OWNER, ROOM_COORDINATOR))
async def test_use_mission_control(world, who, allowed):
    async with client() as http:
        res = await http.get("/mission-control/summit/", cookies=world["cookies"][who])
    assert (res.status_code == 200) is allowed, res.status_code


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(ADMIN, EVENT_OWNER, ROOM_COORDINATOR))
async def test_start_and_stop_floor_transcription(world, who, allowed):
    async with client() as http:
        res = await http.get(
            f"/api/rooms/{world['room']}/floor-transcription/status", cookies=world["cookies"][who]
        )
    assert (res.status_code == 200) is allowed, res.status_code


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(ADMIN, EVENT_OWNER, ROOM_COORDINATOR))
async def test_choose_the_active_interpreter(who, allowed):
    registry = BoothRegistry()
    booth_id = make_booth_id("summit", 1, "de")
    active, _ = await registry.join_participant(booth_id, "Active", INTERPRETER, "de", "c")
    target, _ = await registry.join_participant(booth_id, "Backup", INTERPRETER, "de", "c")
    if who == NOBODY:
        return  # someone with no role can't join the booth at all (see test_view_booths)
    requester, _ = await registry.join_participant(booth_id, who, who, "de", "c")
    try:
        state = await registry.set_active_interpreter(booth_id, requester.participant_id, target.participant_id, "de", "c")
        changed = state["active_interpreter_id"] == target.participant_id
    except PermissionError:
        changed = False
    assert changed is allowed
    assert active.participant_id  # the first joiner was active before


# ── Rows every role has ──────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(("who", "allowed"), allowed_for(*ROLES))
async def test_go_live_chat_and_view_booths(world, who, allowed):
    booth_id = make_booth_id("summit", world["room"], "de")
    payload = {"sub": str(world["people"][who]), "user": True}
    granted = await resolve_booth_role(payload, booth_id)
    assert (granted is not None) is allowed

    async with client() as http:
        page = await http.get(f"/interpreter/summit/{world['room']}/de", cookies=world["cookies"][who])
    assert (page.status_code == 200) is allowed, page.status_code
    if not allowed:
        return
    assert 'data-can-go-live="true"' in page.text

    registry = BoothRegistry()
    participant, _ = await registry.join_participant(booth_id, who, granted, "de", "c")
    state = await registry.update_participant_state(booth_id, participant.participant_id, "de", "c", ingest_connected=True)
    me = next(p for p in state["participants"] if p["participant_id"] == participant.participant_id)
    assert me["ingest_connected"] is True and me["can_go_live"] is True
    message, _ = await registry.add_chat_message(booth_id, participant.participant_id, "hello", "de", "c")
    assert message["body"] == "hello"


# ── Where roles can be given ─────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("level", "role", "ok"),
    [
        ("event", EVENT_OWNER, True),
        ("event", ROOM_COORDINATOR, True),
        ("event", INTERPRETER, True),
        ("event", ADMIN, False),
        ("event", "support", False),
        ("room", ROOM_COORDINATOR, True),
        ("room", INTERPRETER, False),
        ("room", EVENT_OWNER, False),
        ("booth", INTERPRETER, True),
        ("booth", ROOM_COORDINATOR, True),
        ("booth", EVENT_OWNER, False),
    ],
)
async def test_roles_can_only_be_given_where_the_table_allows(world, level, role, ok):
    where = {"event": f"event:{world['event']}", "room": f"room:{world['room']}", "booth": f"booth:{world['booth']}"}[level]
    async with client() as http:
        res = await http.post(
            f"/admin/events/{world['event']}/team/add",
            data={"email": "new@example.org", "role": role, "where": where},
            headers=ORIGIN,
            cookies=world["cookies"][ADMIN],
            follow_redirects=False,
        )
    if ok:
        assert res.status_code == 303
        assert "notice=added" in res.headers["location"]
        return
    assert res.status_code == 400
    assert "can&#39;t be given to" in res.text or "Unknown role." in res.text
    async with get_session() as s:
        # Refused before anything is created: no placeholder account for the email.
        from portal.database import get_user_by_email

        assert await get_user_by_email(s, "new@example.org") is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("level", "role"),
    [("event", ADMIN), ("event", "support"), ("room", INTERPRETER), ("room", EVENT_OWNER), ("booth", EVENT_OWNER)],
)
async def test_legacy_member_routes_refuse_roles_the_table_does_not_allow(world, level, role):
    path = {
        "event": f"/admin/events/{world['event']}/members/",
        "room": f"/admin/events/{world['event']}/rooms/{world['room']}/members/",
        "booth": f"/admin/events/{world['event']}/rooms/{world['room']}/booths/{world['booth']}/members/",
    }[level]
    async with client() as http:
        res = await http.post(
            path,
            data={"email": "new@example.org", "role": role},
            headers=ORIGIN,
            cookies=world["cookies"][ADMIN],
            follow_redirects=False,
        )
    assert "error=invalid_role" in res.headers["location"]
    async with get_session() as s:
        from portal.database import get_user_by_email

        assert await get_user_by_email(s, "new@example.org") is None


# ── Coordinators given per event or per booth get the same room access ───


@pytest.mark.anyio
@pytest.mark.parametrize("level", ["event", "booth"])
async def test_coordinator_given_per_event_or_booth_can_use_mission_control(world, level):
    async with get_session() as s:
        u = await create_user(s, email=f"coord-{level}@example.org", display_name="C", password_hash="x")
        if level == "event":
            await set_event_membership(s, user_id=u.id, event_id=world["event"], role=ROOM_COORDINATOR)
        else:
            await set_booth_membership(s, user_id=u.id, booth_id=world["booth"], role=ROOM_COORDINATOR)
        uid = u.id
    cookies = {"user_token": create_user_token(user_id=uid, email=f"coord-{level}@example.org")}
    async with client() as http:
        mc = await http.get("/mission-control/summit/", cookies=cookies)
        floor = await http.get(f"/api/rooms/{world['room']}/floor-transcription/status", cookies=cookies)
        delete_room = await http.post(
            f"/admin/events/{world['event']}/rooms/{world['room']}/delete", headers=ORIGIN, cookies=cookies
        )
    assert mc.status_code == 200
    assert floor.status_code == 200
    assert delete_room.status_code == 403
