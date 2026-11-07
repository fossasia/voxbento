"""The event Team page: everyone with a role, and invite links, in one place (#757)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.auth import create_user_token, hash_password
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    get_user_by_email,
    init_db,
    set_booth_membership,
    set_event_membership,
    set_room_membership,
)
from portal.models import BoothMembership, EventMembership, InviteToken, RoomMembership
from portal.roles import EVENT_OWNER, INTERPRETER, ROOM_COORDINATOR

os.environ["BOOTH_ACCESS_TOKEN"] = ""
ORIGIN = {"origin": "http://test"}


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture(autouse=True)
def no_email():
    with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as sent:
        yield sent


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def make_user(session, email: str, **kwargs):
    return await create_user(
        session, email=email, display_name=email.split("@")[0].title(), password_hash=hash_password("pw123456"),
        email_verified=True, **kwargs,
    )


@pytest.fixture
async def event():
    async with get_session() as s:
        owner = await make_user(s, "owner@example.org")
        coord = await make_user(s, "coord@example.org")
        interp = await make_user(s, "interp@example.org")
        ev = await create_event(s, slug="summit", display_name="Summit")
        room = await create_room(s, event_id=ev.id, display_name="Room 1")
        booth = await create_booth(s, event_id=ev.id, room_id=room.id, language_code="de", language_name="Deutsch")
        other = await create_event(s, slug="other", display_name="Other")
        other_room = await create_room(s, event_id=other.id, display_name="Elsewhere")
        await set_event_membership(s, user_id=owner.id, event_id=ev.id, role=EVENT_OWNER)
        await set_room_membership(s, user_id=coord.id, room_id=room.id, role=ROOM_COORDINATOR)
        await set_booth_membership(s, user_id=interp.id, booth_id=booth.id, role=INTERPRETER)
        ids = {"event": ev.id, "room": room.id, "booth": booth.id, "other": other.id, "other_room": other_room.id,
                   "owner": owner.id, "coord": coord.id, "interp": interp.id}
    cookie = lambda uid, email: {"user_token": create_user_token(user_id=uid, email=email)}  # noqa: E731
    return {
        **ids,
        "owner_cookies": cookie(ids["owner"], "owner@example.org"),
        "coord_cookies": cookie(ids["coord"], "coord@example.org"),
        "interp_cookies": cookie(ids["interp"], "interp@example.org"),
        "base": f"/workspace/events/{ids['event']}/team",
    }


async def post(http, event, path, data, cookies=None):
    return await http.post(
        f"{event['base']}/{path}", data=data, headers=ORIGIN, cookies=cookies or event["owner_cookies"], follow_redirects=False
    )


# ── Who can open it ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_only_owners_and_admins_manage_the_team(event):
    async with get_session() as s:
        admin = await make_user(s, "admin@example.org", is_admin=True)
    admin_cookies = {"user_token": create_user_token(user_id=admin.id, email=admin.email, is_admin=True)}
    async with client() as http:
        owner = await http.get(f"{event['base']}/", cookies=event["owner_cookies"])
        admin_page = await http.get(f"/admin/events/{event['event']}/team/", cookies=admin_cookies)
        coord = await http.get(f"/admin/events/{event['event']}/team/", cookies=event["coord_cookies"])
        interp = await http.get(f"/admin/events/{event['event']}/team/", cookies=event["interp_cookies"])
    assert owner.status_code == 200 and admin_page.status_code == 200
    assert coord.status_code == 403 and interp.status_code == 403


@pytest.mark.anyio
async def test_page_lists_everyone_once_in_plain_language(event):
    async with client() as http:
        html = (await http.get(f"{event['base']}/", cookies=event["owner_cookies"])).text
    assert "<h1>Team</h1>" in html
    assert "<span>Event owner</span>" in html
    assert "<span>Room coordinator · Room 1</span>" in html
    assert "<span>Interpreter · Room 1 · Deutsch</span>" in html
    assert html.count('data-team-person') == 3
    for raw in ("event_owner</", "room_coordinator</", ">interpreter<"):
        assert raw not in html


@pytest.mark.anyio
async def test_old_members_page_and_old_sections_point_to_team(event):
    async with client() as http:
        old = await http.get(f"/workspace/events/{event['event']}/members/", cookies=event["owner_cookies"], follow_redirects=False)
        room = await http.get(f"/workspace/events/{event['event']}/rooms/{event['room']}/", cookies=event["owner_cookies"])
        booth = await http.get(
            f"/workspace/events/{event['event']}/rooms/{event['room']}/booths/{event['booth']}/", cookies=event["owner_cookies"]
        )
        detail = await http.get(f"/workspace/events/{event['event']}/", cookies=event["owner_cookies"])
    assert old.status_code == 303 and old.headers["location"].endswith(f"/events/{event['event']}/team/")
    for page in (room, booth):
        assert "Open Team" in page.text
        assert "/members/" not in page.text and "Generate New Token" not in page.text
    assert f'/events/{event["event"]}/team/' in detail.text


# ── Adding people ────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("role", "where_key", "model"),
    [
        (EVENT_OWNER, "event", EventMembership),
        (ROOM_COORDINATOR, "event", EventMembership),
        (ROOM_COORDINATOR, "room", RoomMembership),
        (ROOM_COORDINATOR, "booth", BoothMembership),
        (INTERPRETER, "event", EventMembership),
        (INTERPRETER, "booth", BoothMembership),
    ],
)
async def test_add_where_the_table_allows(event, no_email, role, where_key, model):
    where = f"{where_key}:{event[where_key]}"
    async with client() as http:
        res = await post(http, event, "add", {"email": "New@Example.org", "role": role, "where": where})
    assert res.status_code == 303 and res.headers["location"].endswith("?notice=added")
    async with get_session() as s:
        user = await get_user_by_email(s, "new@example.org")
        rows = (await s.execute(select(model).where(model.user_id == user.id))).scalars().all()
    assert [r.role for r in rows] == [role]
    no_email.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("role", "where_key", "message"),
    [
        (EVENT_OWNER, "room", "Event owner can&#39;t be given to a room."),
        (EVENT_OWNER, "booth", "Event owner can&#39;t be given to a booth."),
        (INTERPRETER, "room", "Interpreter can&#39;t be given to a room."),
        ("admin", "event", "Admin can&#39;t be given to an event."),
        ("support", "event", "Unknown role."),
    ],
)
async def test_add_refused_where_the_table_does_not_allow(event, no_email, role, where_key, message):
    async with client() as http:
        res = await post(http, event, "add", {"email": "new@example.org", "role": role, "where": f"{where_key}:{event[where_key]}"})
    assert res.status_code == 400
    assert message in res.text
    assert 'value="new@example.org"' in res.text  # what they typed is kept
    async with get_session() as s:
        assert await get_user_by_email(s, "new@example.org") is None  # nothing created
    no_email.assert_not_awaited()


@pytest.mark.anyio
async def test_add_refuses_places_outside_this_event_and_bad_email(event):
    async with client() as http:
        elsewhere = await post(http, event, "add", {"email": "a@example.org", "role": ROOM_COORDINATOR, "where": f"room:{event['other_room']}"})
        bad_email = await post(http, event, "add", {"email": "not-an-email", "role": INTERPRETER, "where": f"event:{event['event']}"})
    assert elsewhere.status_code == 400 and "Choose where this role applies." in elsewhere.text
    assert bad_email.status_code == 400 and "Enter a valid email address." in bad_email.text


@pytest.mark.anyio
async def test_adding_the_same_role_again_changes_nothing(event, no_email):
    async with client() as http:
        res = await post(http, event, "add", {"email": "interp@example.org", "role": INTERPRETER, "where": f"booth:{event['booth']}"})
    assert res.headers["location"].endswith("?notice=already")
    no_email.assert_not_awaited()


@pytest.mark.anyio
async def test_the_last_event_owner_cannot_be_downgraded_or_removed(event):
    async with get_session() as s:
        owner_row = (await s.execute(select(EventMembership).where(EventMembership.user_id == event["owner"]))).scalar_one()
    async with client() as http:
        downgrade = await post(http, event, "add", {"email": "owner@example.org", "role": INTERPRETER, "where": f"event:{event['event']}"})
        remove = await post(http, event, "remove", {"level": "event", "membership_id": owner_row.id})
        # With a second owner, the first can be removed.
        await post(http, event, "add", {"email": "second@example.org", "role": EVENT_OWNER, "where": f"event:{event['event']}"})
        allowed = await post(http, event, "remove", {"level": "event", "membership_id": owner_row.id})
    for res in (downgrade, remove):
        assert res.status_code == 400 and "An event needs at least one Event owner." in res.text
    assert allowed.status_code == 303


# ── Removing people ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_remove_a_role(event):
    async with get_session() as s:
        row = (await s.execute(select(BoothMembership).where(BoothMembership.user_id == event["interp"]))).scalar_one()
    async with client() as http:
        res = await post(http, event, "remove", {"level": "booth", "membership_id": row.id})
    assert res.headers["location"].endswith("?notice=removed")
    async with get_session() as s:
        assert await s.get(BoothMembership, row.id) is None


@pytest.mark.anyio
async def test_cannot_remove_members_of_another_event(event):
    async with get_session() as s:
        stranger = await make_user(s, "stranger@example.org")
        other_row = await set_event_membership(s, user_id=stranger.id, event_id=event["other"], role=INTERPRETER)
        other_id = other_row.id
    async with client() as http:
        res = await post(http, event, "remove", {"level": "event", "membership_id": other_id})
    assert res.status_code == 400
    async with get_session() as s:
        assert await s.get(EventMembership, other_id) is not None


@pytest.mark.anyio
async def test_coordinators_cannot_change_the_team(event):
    async with client() as http:
        res = await http.post(
            f"/admin/events/{event['event']}/team/add",
            data={"email": "x@example.org", "role": EVENT_OWNER, "where": f"event:{event['event']}"},
            headers=ORIGIN,
            cookies=event["coord_cookies"],
        )
    assert res.status_code == 403


# ── Invite links ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_copy_and_revoke_an_invite_link(event):
    async with client() as http:
        created = await post(
            http, event, "links", {"booth": f"booth:{event['booth']}", "link_role": INTERPRETER, "label": "Maria", "expires_hours": "48"}
        )
        page = await http.get(f"{event['base']}/", cookies=event["owner_cookies"])
        async with get_session() as s:
            token = (await s.execute(select(InviteToken))).scalar_one()
        revoked = await post(http, event, "links/revoke", {"token": token.token})
        after = await http.get(f"{event['base']}/", cookies=event["owner_cookies"])
    assert created.headers["location"].endswith("?notice=link_created")
    assert f"/join/{token.token}" in page.text and "Maria" in page.text
    assert revoked.headers["location"].endswith("?notice=link_revoked")
    assert f"/join/{token.token}" not in after.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"link_role": EVENT_OWNER, "expires_hours": ""}, "Event owner can&#39;t be given to a booth."),
        ({"link_role": INTERPRETER, "expires_hours": "0"}, "Links can last between 1 and 2160 hours."),
        ({"link_role": INTERPRETER, "expires_hours": "soon"}, "Enter the expiry in whole hours."),
    ],
)
async def test_invite_link_validation(event, data, message):
    async with client() as http:
        res = await post(http, event, "links", {"booth": f"booth:{event['booth']}", "label": "", **data})
    assert res.status_code == 400 and message in res.text


@pytest.mark.anyio
async def test_cannot_revoke_links_of_another_event(event):
    async with get_session() as s:
        other_booth = await create_booth(s, event_id=event["other"], room_id=event["other_room"], language_code="fr", language_name="French")
        tok = InviteToken(booth_id=other_booth.id, role=INTERPRETER)
        s.add(tok)
        await s.flush()
        value = tok.token
    async with client() as http:
        res = await post(http, event, "links/revoke", {"token": value})
    assert res.status_code == 400
    async with get_session() as s:
        assert (await s.get(InviteToken, value)).used_at is None


# ── Legacy handlers stay inside the event they name ──────────────────────


@pytest.mark.anyio
async def test_owner_cannot_delete_a_booth_of_another_event(event):
    from portal.database import get_booth_by_id

    async with get_session() as s:
        foreign = await create_booth(
            s, event_id=event["other"], room_id=event["other_room"], language_code="fr", language_name="French"
        )
        foreign_id = foreign.id
    async with client() as http:
        res = await http.post(
            f"/workspace/events/{event['event']}/rooms/{event['other_room']}/booths/{foreign_id}/delete",
            headers=ORIGIN, cookies=event["owner_cookies"], follow_redirects=False,
        )
    assert res.status_code == 404
    async with get_session() as s:
        assert await get_booth_by_id(s, foreign_id) is not None


@pytest.mark.anyio
async def test_legacy_member_removal_checks_the_event_and_keeps_the_last_owner(event):
    async with get_session() as s:
        outsider = await make_user(s, "outsider@example.org")
        foreign = await set_event_membership(s, user_id=outsider.id, event_id=event["other"], role=EVENT_OWNER)
        foreign_id = foreign.id
        owner_row = await s.scalar(select(EventMembership).where(EventMembership.user_id == event["owner"]))
        owner_row_id = owner_row.id
    async with client() as http:
        cross = await http.post(
            f"/workspace/events/{event['event']}/members/{foreign_id}/delete",
            headers=ORIGIN, cookies=event["owner_cookies"], follow_redirects=False,
        )
        last = await http.post(
            f"/workspace/events/{event['event']}/members/{owner_row_id}/delete",
            headers=ORIGIN, cookies=event["owner_cookies"], follow_redirects=False,
        )
    assert cross.status_code == 404
    assert last.status_code == 400
    assert "at least one Event owner" in last.text
    async with get_session() as s:
        assert await s.get(EventMembership, foreign_id) is not None
        assert await s.get(EventMembership, owner_row_id) is not None


# ── Interpreter lobby ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_lobby_offers_mission_control_to_a_coordinator_invited_by_link(event):
    from portal.auth import create_participant_token

    token = create_participant_token(
        booth_id=event["booth"], role=ROOM_COORDINATOR, event_slug="summit", room_id=event["room"], language_code="de"
    )
    async with client() as http:
        res = await http.get("/interpreter", cookies={"session_token": token})
    assert res.status_code == 200
    assert 'href="/mission-control/summit/"' in res.text


@pytest.mark.anyio
async def test_lobby_shows_booths_for_event_level_interpreter(event):
    async with get_session() as s:
        user = await make_user(s, "eventinterp@example.org")
        french_booth = await create_booth(
            s, event_id=event["event"], room_id=event["room"], language_code="fr", language_name="French"
        )
        await set_event_membership(s, user_id=user.id, event_id=event["event"], role=INTERPRETER)
    cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}

    async with client() as http:
        res = await http.get("/interpreter", cookies=cookies)
    assert res.status_code == 200
    assert "Deutsch" in res.text
    assert "French" in res.text
    assert f'href="/interpreter/summit/{event["room"]}/de"' in res.text
    assert f'href="/interpreter/summit/{event["room"]}/{french_booth.language_code}"' in res.text
    assert "You have no assigned booths" not in res.text


@pytest.mark.anyio
async def test_lobby_shows_booths_for_room_level_assignments(event):
    async with client() as http:
        # Room coordinator sees booth in their assigned room with Mission Control link
        coord_res = await http.get("/interpreter", cookies=event["coord_cookies"])
        assert coord_res.status_code == 200
        assert "Deutsch" in coord_res.text
        assert 'href="/mission-control/summit/"' in coord_res.text


@pytest.mark.anyio
async def test_lobby_preserves_booth_level_assignment_and_scopes_strictly(event):
    async with get_session() as s:
        foreign_booth = await create_booth(
            s, event_id=event["other"], room_id=event["other_room"], language_code="es", language_name="Spanish"
        )

    async with client() as http:
        # Booth-level interpreter only sees Deutsch, not foreign booth
        res = await http.get("/interpreter", cookies=event["interp_cookies"])
    assert res.status_code == 200
    assert "Deutsch" in res.text
    assert "Spanish" not in res.text
    assert f'href="/interpreter/other/{foreign_booth.room_id}/es"' not in res.text


@pytest.mark.anyio
async def test_lobby_deduplicates_overlapping_memberships_and_applies_highest_role(event):
    async with get_session() as s:
        user = await make_user(s, "multi@example.org")
        # Event-level: interpreter
        await set_event_membership(s, user_id=user.id, event_id=event["event"], role=INTERPRETER)
        # Booth-level: room_coordinator on Deutsch booth
        await set_booth_membership(s, user_id=user.id, booth_id=event["booth"], role=ROOM_COORDINATOR)
    cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}

    async with client() as http:
        res = await http.get("/interpreter", cookies=cookies)
    assert res.status_code == 200
    assert "Deutsch" in res.text
    # Exactly one row rendered for the booth
    assert res.text.count('class="lobby-booth"') == 1
    # Highest role (room_coordinator) wins over event-level interpreter
    assert 'href="/mission-control/summit/"' in res.text


@pytest.mark.anyio
async def test_owner_cannot_assign_or_invite_in_another_events_rooms_and_booths(event):
    from portal.database import create_invite_token, get_booth_by_id

    async with get_session() as s:
        foreign_booth = await create_booth(
            s, event_id=event["other"], room_id=event["other_room"], language_code="fr", language_name="French"
        )
        outsider = await make_user(s, "outsider@example.org")
        room_row = await set_room_membership(s, user_id=outsider.id, room_id=event["other_room"], role=ROOM_COORDINATOR)
        booth_row = await set_booth_membership(s, user_id=outsider.id, booth_id=foreign_booth.id, role=INTERPRETER)
        token = await create_invite_token(s, booth_id=foreign_booth.id, role=INTERPRETER)
        ids = {"booth": foreign_booth.id, "room_m": room_row.id, "booth_m": booth_row.id, "token": token.token}

    own = f"/workspace/events/{event['event']}"
    room = f"{own}/rooms/{event['other_room']}"
    booth = f"{room}/booths/{ids['booth']}"
    attempts = [
        (f"{room}/members/", {"email": "new@example.org", "role": ROOM_COORDINATOR}),
        (f"{room}/members/{ids['room_m']}/invite", {}),
        (f"{room}/booths/", {"language_code": "es", "language_name": "Spanish"}),
        (f"{booth}/members/", {"email": "new@example.org", "role": INTERPRETER}),
        (f"{booth}/members/{ids['booth_m']}/invite", {}),
        (f"{booth}/tokens/", {"role": INTERPRETER}),
        (f"{booth}/tokens/{ids['token']}/revoke", {}),
    ]
    async with client() as http:
        for path, data in attempts:
            res = await http.post(path, data=data, headers=ORIGIN, cookies=event["owner_cookies"], follow_redirects=False)
            assert res.status_code == 404, path

    async with get_session() as s:
        assert await get_user_by_email(s, "new@example.org") is None
        tokens = (await s.execute(select(InviteToken).where(InviteToken.booth_id == ids["booth"]))).scalars().all()
        assert [t.token for t in tokens] == [ids["token"]] and tokens[0].used_at is None
        assert (await get_booth_by_id(s, ids["booth"])).room_id == event["other_room"]
        from portal.database import list_booths_for_room

        assert [b.language_code for b in await list_booths_for_room(s, event["other_room"])] == ["fr"]


@pytest.mark.anyio
async def test_room_setup_does_not_add_booths_to_another_events_room(event):
    from portal.database import list_booths_for_room

    async with client() as http:
        res = await http.post(
            f"/workspace/events/{event['event']}/rooms/{event['other_room']}/setup",
            data={"display_name": "Taken", "spoken_language": "en", "mode": "interpreters", "languages": ["es"]},
            headers=ORIGIN, cookies=event["owner_cookies"], follow_redirects=False,
        )
    assert res.status_code == 404
    async with get_session() as s:
        assert await list_booths_for_room(s, event["other_room"]) == []
