"""Regression tests for scoped authorization in the legacy booth API."""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timezone

import anyio
import pytest
from fastapi.testclient import TestClient

from fastapi_app import app
from portal.auth import create_participant_token, create_token, create_user_token
from portal.booth_identity import make_booth_id
from portal.config import settings
from portal.database import (
    create_api_key,
    create_event,
    create_room,
    create_user,
    get_session,
    set_booth_membership,
    set_event_membership,
    set_room_membership,
)
from portal.globals import booths
from portal.models import DBBooth
from portal.utils import _require_access

client = TestClient(app)


def _request():
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


def test_missing_booth_token_allows_development_access(monkeypatch):
    monkeypatch.setattr(settings, "booth_access_token", "")
    monkeypatch.setattr(settings, "debug", True)

    _require_access(_request(), None)


def test_missing_booth_token_rejects_production_access(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "booth_access_token", "")
    monkeypatch.setattr(settings, "debug", False)

    with pytest.raises(HTTPException, match="BOOTH_ACCESS_TOKEN"):
        _require_access(_request(), None)


def test_configured_booth_token_allows_legacy_access(monkeypatch):
    from fastapi.security import HTTPAuthorizationCredentials

    monkeypatch.setattr(settings, "booth_access_token", "test-booth-token")
    monkeypatch.setattr(settings, "debug", False)

    _require_access(
        _request(),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-booth-token"),
    )


def _setup_database():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    anyio.run(init_db)
    return dispose


def test_user_cannot_create_or_list_booths_for_another_event(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event_a = await create_event(session, slug="owned-event", display_name="Owned")
                event_b = await create_event(session, slug="other-event", display_name="Other")
                room_b = await create_room(session, event_id=event_b.id, display_name="Other Room")
                user = await create_user(
                    session,
                    email="owner@example.com",
                    display_name="Owner",
                    password_hash="hash",
                )
                await set_event_membership(session, user_id=user.id, event_id=event_a.id, role="event_owner")
                await session.commit()
                return user.id, room_b.id

        user_id, room_b_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "")
        monkeypatch.setattr(settings, "debug", True)
        token = create_user_token(user_id=user_id, email="owner@example.com")
        headers = {"Authorization": f"Bearer {token}"}

        create_response = client.post(
            "/api/events/other-event/booths",
            json={"language_code": "en", "room_id": room_b_id},
            headers=headers,
        )
        assert create_response.status_code == 403

        list_response = client.get("/api/events/other-event/booths", headers=headers)
        assert list_response.status_code == 403
    finally:
        anyio.run(dispose)


def test_room_coordinator_cannot_manage_booths(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="coordinator-event", display_name="Coordinator")
                room = await create_room(session, event_id=event.id, display_name="Main")
                user = await create_user(
                    session,
                    email="coordinator@example.com",
                    display_name="Coordinator",
                    password_hash="hash",
                )
                from portal.database import set_room_membership

                await set_room_membership(session, user_id=user.id, room_id=room.id, role="room_coordinator")
                await session.commit()
                return user.id, room.id

        user_id, room_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "configured-master-token")
        monkeypatch.setattr(settings, "debug", False)
        token = create_user_token(user_id=user_id, email="coordinator@example.com")
        response = client.post(
            "/api/events/coordinator-event/booths",
            json={"language_code": "en", "room_id": room_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403, response.text
    finally:
        anyio.run(dispose)


def test_numeric_external_room_id_uses_explicit_external_route(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="numeric-event", display_name="Numeric")
                room = await create_room(session, event_id=event.id, display_name="External", eventyay_room_id="42")
                booth = DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English")
                session.add(booth)
                await session.commit()

        anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "master-token")
        monkeypatch.setattr(settings, "debug", False)
        response = client.delete(
            "/api/events/numeric-event/eventyay-rooms/42/booths/en",
            headers={"Authorization": "Bearer master-token"},
        )
        assert response.status_code == 204
    finally:
        anyio.run(dispose)


def test_event_api_key_can_delete_only_its_event_booth(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="key-delete", display_name="Key Delete")
                other = await create_event(session, slug="other-key-delete", display_name="Other")
                room = await create_room(session, event_id=event.id, display_name="Main", eventyay_room_id="42")
                other_room = await create_room(session, event_id=other.id, display_name="Other", eventyay_room_id="42")
                session.add_all(
                    [
                        DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English"),
                        DBBooth(event_id=other.id, room_id=other_room.id, language_code="en", language_name="English"),
                    ]
                )
                _, raw_key = await create_api_key(session, event.id, name="Delete key")
                await session.commit()
                return raw_key

        raw_key = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "configured-master-token")
        monkeypatch.setattr(settings, "debug", False)
        headers = {"Authorization": f"Bearer {raw_key}"}

        allowed = client.delete("/api/events/key-delete/eventyay-rooms/42/booths/en", headers=headers)
        assert allowed.status_code == 204, allowed.text

        missing = client.delete("/api/events/other-key-delete/eventyay-rooms/42/booths/en")
        assert missing.status_code == 403, missing.text

        invalid = client.delete(
            "/api/events/other-key-delete/eventyay-rooms/42/booths/en",
            headers={"Authorization": "Bearer invalid-api-key"},
        )
        assert invalid.status_code == 403, invalid.text

        denied = client.delete("/api/events/other-key-delete/eventyay-rooms/42/booths/en", headers=headers)
        assert denied.status_code == 403, denied.text
    finally:
        anyio.run(dispose)


def test_invalid_bearer_cannot_fall_back_to_participant_cookie(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="cookie-scope", display_name="Cookie Scope")
                room = await create_room(session, event_id=event.id, display_name="Main")
                session.add(DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English"))
                await session.commit()
                return room.id

        room_id = anyio.run(seed)
        participant = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="cookie-scope",
            room_id=room_id,
            language_code="en",
        )
        monkeypatch.setattr(settings, "booth_access_token", "configured-master-token")
        monkeypatch.setattr(settings, "debug", False)
        path = f"/api/events/cookie-scope/booths/en/state?room_id={room_id}"
        cookie = {"session_token": participant}

        allowed = client.get(path, cookies=cookie)
        assert allowed.status_code == 200, allowed.text

        unauthorized_user = create_user_token(user_id=999, email="not-a-member@example.com")
        mixed_cookies = {**cookie, "user_token": unauthorized_user}
        participant_fallback = client.get(path, cookies=mixed_cookies)
        assert participant_fallback.status_code == 200, participant_fallback.text

        other_room_participant = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="cookie-scope",
            room_id=room_id + 1,
            language_code="en",
        )
        wrong_room_cookies = {"session_token": other_room_participant, "user_token": unauthorized_user}
        wrong_room = client.get(path, cookies=wrong_room_cookies)
        assert wrong_room.status_code == 403, wrong_room.text

        rejected = client.get(path, cookies=cookie, headers={"Authorization": "Bearer malformed"})
        assert rejected.status_code == 403, rejected.text

        malformed = client.get(path, cookies=cookie, headers={"Authorization": "Basic not-a-bearer"})
        assert malformed.status_code == 403, malformed.text
    finally:
        anyio.run(dispose)


def test_auth_token_flow_authorizes_legacy_event_api(monkeypatch):
    dispose = _setup_database()
    try:
        monkeypatch.setattr(settings, "booth_access_token", "a" * 32)
        monkeypatch.setattr(settings, "debug", False)
        response = client.post("/api/auth/token", json={"token": "a" * 32})
        assert response.status_code == 200, response.text
        jwt_token = response.json()["access_token"]

        list_response = client.get(
            "/api/events/token-flow-event/booths", headers={"Authorization": f"Bearer {jwt_token}"}
        )
        assert list_response.status_code == 200, list_response.text
        assert list_response.json()["booths"] == []
    finally:
        anyio.run(dispose)


def test_soft_deleted_event_cannot_create_or_list_booths(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="deleted-event", display_name="Deleted")
                event.deleted_at = datetime.now(timezone.utc)
                await session.commit()

        anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "a" * 32)
        monkeypatch.setattr(settings, "debug", False)
        headers = {"Authorization": "Bearer " + "a" * 32}
        create_response = client.post(
            "/api/events/deleted-event/booths", json={"language_code": "en"}, headers=headers
        )
        assert create_response.status_code == 404
        list_response = client.get("/api/events/deleted-event/booths", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()["booths"] == []
    finally:
        anyio.run(dispose)


def test_global_booth_token_does_not_inherit_participant_cookie_scope(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="global-cookie", display_name="Global Cookie")
                room = await create_room(session, event_id=event.id, display_name="Main")
                session.add(DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English"))
                await session.commit()
                return room.id

        room_id = anyio.run(seed)
        participant = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="another-event",
            room_id=999,
            language_code="de",
        )
        monkeypatch.setattr(settings, "booth_access_token", "configured-master-token")
        monkeypatch.setattr(settings, "debug", False)
        response = client.get(
            f"/api/events/global-cookie/booths/en/state?room_id={room_id}",
            headers={"Authorization": "Bearer configured-master-token"},
            cookies={"session_token": participant},
        )
        assert response.status_code == 200, response.text
    finally:
        anyio.run(dispose)


def _seed_booth_team():
    """One booth plus a user for each way of reaching it, and one outside it."""

    async def seed():
        async with get_session() as session:
            event = await create_event(session, slug="team-event", display_name="Team")
            room = await create_room(session, event_id=event.id, display_name="Main")
            booth = DBBooth(
                event_id=event.id,
                room_id=room.id,
                language_code="es",
                language_name="Spanish",
                transcription_enabled=True,
                transcription_provider="local",
                transcription_model="tiny",
            )
            other_booth = DBBooth(event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
            session.add_all([booth, other_booth])
            await session.flush()
            users = {}
            for name, active in (
                ("interpreter", True),
                ("standby", True),
                ("coordinator", True),
                ("owner", True),
                ("other-booth", True),
                ("deactivated", False),
                ("stale-admin", True),
            ):
                users[name] = await create_user(
                    session, email=f"{name}@example.com", display_name=name, password_hash="hash", is_active=active
                )
            await set_booth_membership(session, user_id=users["interpreter"].id, booth_id=booth.id, role="interpreter")
            await set_booth_membership(session, user_id=users["standby"].id, booth_id=booth.id, role="interpreter")
            await set_booth_membership(session, user_id=users["deactivated"].id, booth_id=booth.id, role="interpreter")
            await set_booth_membership(
                session, user_id=users["other-booth"].id, booth_id=other_booth.id, role="interpreter"
            )
            await set_room_membership(
                session, user_id=users["coordinator"].id, room_id=room.id, role="room_coordinator"
            )
            await set_event_membership(session, user_id=users["owner"].id, event_id=event.id, role="event_owner")
            await session.commit()
            return {name: user.id for name, user in users.items()}, room.id, booth.id

    return anyio.run(seed)


@pytest.fixture
def booth_team(monkeypatch):
    dispose = _setup_database()
    calls = []

    async def record_start(*args, **kwargs):
        calls.append("start")

    async def record_stop(*args, **kwargs):
        calls.append("stop")

    monkeypatch.setattr("portal.routers.api.start_transcription_worker", record_start)
    monkeypatch.setattr("portal.routers.api.stop_transcription_worker", record_stop)
    monkeypatch.setattr(settings, "booth_access_token", "configured-master-token")
    monkeypatch.setattr(settings, "debug", False)
    room_id = None
    try:
        user_ids, room_id, booth_id = _seed_booth_team()
        yield user_ids, room_id, booth_id, calls
    finally:
        if room_id is not None:
            # Booth state lives in memory, outside the per-test database.
            anyio.run(booths.remove_booth, "team-event", room_id, "es")
        anyio.run(dispose)


# Booth page requests are same-origin; cookie-only POSTs without it are refused (CSRF).
SAME_ORIGIN = {"Origin": "http://testserver"}


def _user_cookies(user_ids, name, *, is_admin=False):
    return {"user_token": create_user_token(user_id=user_ids[name], email=f"{name}@example.com", is_admin=is_admin)}


@pytest.mark.parametrize(
    ("name", "can_view", "can_go_live"),
    [
        ("interpreter", True, True),
        ("coordinator", True, True),
        ("owner", True, True),
        ("other-booth", False, False),
        ("deactivated", False, False),
    ],
)
def test_signed_in_booth_team_reaches_the_booth_api(booth_team, name, can_view, can_go_live):
    user_ids, room_id, _, calls = booth_team
    cookies = _user_cookies(user_ids, name)
    base = f"/api/events/team-event/rooms/{room_id}/booths/es/transcription"

    state = client.get(f"/api/events/team-event/booths/es/state?room_id={room_id}", cookies=cookies)
    assert (state.status_code == 200) is can_view, state.text

    start = client.post(f"{base}/start", cookies=cookies, headers=SAME_ORIGIN)
    stop = client.post(f"{base}/stop", cookies=cookies, headers=SAME_ORIGIN)
    assert (start.status_code == 200) is can_go_live, start.text
    assert (stop.status_code == 200) is can_go_live, stop.text
    assert calls == (["start", "stop"] if can_go_live else [])


def test_stale_admin_claim_does_not_grant_booth_access(booth_team):
    user_ids, room_id, _, calls = booth_team
    cookies = _user_cookies(user_ids, "stale-admin", is_admin=True)

    response = client.post(
        f"/api/events/team-event/rooms/{room_id}/booths/es/transcription/start", cookies=cookies, headers=SAME_ORIGIN
    )

    assert response.status_code == 403, response.text
    assert calls == []


@pytest.mark.parametrize(
    ("role", "can_go_live"),
    [("interpreter", True), pytest.param("support", False, id="removed-support-role")],
)
def test_invite_link_role_must_be_able_to_go_live(booth_team, role, can_go_live):
    _, room_id, booth_id, calls = booth_team
    participant = create_participant_token(
        booth_id=booth_id, role=role, event_slug="team-event", room_id=room_id, language_code="es"
    )

    response = client.post(
        f"/api/events/team-event/rooms/{room_id}/booths/es/transcription/start",
        cookies={"session_token": participant},
        headers=SAME_ORIGIN,
    )

    assert (response.status_code == 200) is can_go_live, response.text
    assert calls == (["start"] if can_go_live else [])


def test_booth_role_does_not_reach_another_language(booth_team):
    user_ids, room_id, _, calls = booth_team

    response = client.post(
        f"/api/events/team-event/rooms/{room_id}/booths/fr/transcription/start",
        cookies=_user_cookies(user_ids, "interpreter"),
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 403, response.text
    assert calls == []


def test_old_invite_cookie_does_not_block_a_booth_member(booth_team):
    user_ids, room_id, booth_id, calls = booth_team
    stale_invite = create_participant_token(
        booth_id=booth_id, role="interpreter", event_slug="team-event", room_id=room_id + 1, language_code="es"
    )
    cookies = {**_user_cookies(user_ids, "interpreter"), "session_token": stale_invite}

    response = client.post(
        f"/api/events/team-event/rooms/{room_id}/booths/es/transcription/start", cookies=cookies, headers=SAME_ORIGIN
    )

    assert response.status_code == 200, response.text
    assert calls == ["start"]


@pytest.mark.parametrize("headers", [{"Origin": "https://attacker.example"}, {}], ids=["cross-site", "no-origin"])
@pytest.mark.parametrize("action", ["start", "stop"])
def test_cookie_only_transcription_post_must_be_same_origin(booth_team, headers, action):
    user_ids, room_id, _, calls = booth_team

    response = client.post(
        f"/api/events/team-event/rooms/{room_id}/booths/es/transcription/{action}",
        cookies=_user_cookies(user_ids, "interpreter"),
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Invalid request origin."
    assert calls == []


def test_wrong_query_token_does_not_skip_the_origin_check(booth_team):
    user_ids, room_id, _, calls = booth_team

    response = client.post(
        f"/api/events/team-event/rooms/{room_id}/booths/es/transcription/start?token=not-the-booth-token",
        cookies=_user_cookies(user_ids, "interpreter"),
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 400, response.text
    assert calls == []


def test_configured_query_token_needs_no_origin(booth_team):
    _, room_id, _, calls = booth_team

    response = client.post(
        f"/api/events/team-event/rooms/{room_id}/booths/es/transcription/start?token=configured-master-token"
    )

    assert response.status_code == 200, response.text
    assert calls == ["start"]


def _join_booth(stack, room_id, cookies, name):
    """Join the Spanish booth over /ws/booth and return the new participant id.

    The socket stays open until *stack* closes, so the participant stays in the booth.
    """
    ws = stack.enter_context(
        client.websocket_connect(f"/ws/booth/{make_booth_id('team-event', room_id, 'es')}", cookies=cookies)
    )
    ws.send_text(json.dumps({"type": "booth:join", "display_name": name, "role": "interpreter"}))
    joined = ws.receive_json()
    assert joined["type"] == "booth:joined", joined
    return joined["participant_id"]


def _stop(room_id, headers=None, **kwargs):
    return client.post(
        f"/api/events/team-event/rooms/{room_id}/booths/es/transcription/stop",
        headers={**SAME_ORIGIN, **(headers or {})},
        **kwargs,
    )


def test_standby_interpreter_cannot_stop_live_transcription(booth_team):
    user_ids, room_id, _, calls = booth_team
    with ExitStack() as stack:
        live_id = _join_booth(stack, room_id, _user_cookies(user_ids, "interpreter"), "Live")
        _join_booth(stack, room_id, _user_cookies(user_ids, "standby"), "Standby")

        # Naming the live participant must not help; only the session that joined it counts.
        refused = _stop(room_id, cookies=_user_cookies(user_ids, "standby"), params={"participant_id": live_id})
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == "Only the live interpreter can stop transcription."
        assert calls == []

        allowed = _stop(room_id, cookies=_user_cookies(user_ids, "interpreter"))
        assert allowed.status_code == 200, allowed.text
        assert calls == ["stop"]


def test_standby_can_stop_after_taking_over_from_a_live_interpreter_who_left(booth_team):
    user_ids, room_id, _, calls = booth_team
    with ExitStack() as standby_stack:
        with ExitStack() as live_stack:
            _join_booth(live_stack, room_id, _user_cookies(user_ids, "interpreter"), "Live")
            standby_id = _join_booth(standby_stack, room_id, _user_cookies(user_ids, "standby"), "Standby")
        assert booths.get_booth_sync(make_booth_id("team-event", room_id, "es")).active_interpreter_id == standby_id

        response = _stop(room_id, cookies=_user_cookies(user_ids, "standby"))

    assert response.status_code == 200, response.text
    assert calls == ["stop"]


@pytest.mark.parametrize("name", ["coordinator", "owner"])
def test_booth_managers_can_stop_while_an_interpreter_is_live(booth_team, name):
    user_ids, room_id, _, calls = booth_team
    with ExitStack() as stack:
        _join_booth(stack, room_id, _user_cookies(user_ids, "interpreter"), "Live")

        response = _stop(room_id, cookies=_user_cookies(user_ids, name))

    assert response.status_code == 200, response.text
    assert calls == ["stop"]


def test_configured_token_can_stop_while_an_interpreter_is_live(booth_team):
    user_ids, room_id, _, calls = booth_team
    with ExitStack() as stack:
        _join_booth(stack, room_id, _user_cookies(user_ids, "interpreter"), "Live")

        response = client.post(
            f"/api/events/team-event/rooms/{room_id}/booths/es/transcription/stop?token=configured-master-token"
        )

    assert response.status_code == 200, response.text
    assert calls == ["stop"]


def test_invite_link_sessions_are_told_apart(booth_team):
    _, room_id, booth_id, calls = booth_team

    def invite():
        token = create_participant_token(
            booth_id=booth_id, role="interpreter", event_slug="team-event", room_id=room_id, language_code="es"
        )
        return {"session_token": token}

    live, standby = invite(), invite()
    with ExitStack() as stack:
        _join_booth(stack, room_id, live, "Live")
        _join_booth(stack, room_id, standby, "Standby")

        refused = _stop(room_id, cookies=standby)
        assert refused.status_code == 403, refused.text
        assert calls == []

        allowed = _stop(room_id, cookies=live)
        assert allowed.status_code == 200, allowed.text
        assert calls == ["stop"]


@pytest.mark.parametrize(
    ("configured_token", "expected"),
    [pytest.param("", 403, id="anyone-can-mint-it"), pytest.param("configured-master-token", 200, id="operator")],
)
def test_legacy_api_bearer_only_skips_the_live_check_with_a_configured_token(
    booth_team, monkeypatch, configured_token, expected
):
    user_ids, room_id, _, calls = booth_team
    # Without BOOTH_ACCESS_TOKEN (debug), the booth page gets a legacy_api token from /api/auth/token.
    monkeypatch.setattr(settings, "booth_access_token", configured_token)
    monkeypatch.setattr(settings, "debug", not configured_token)
    with ExitStack() as stack:
        _join_booth(stack, room_id, _user_cookies(user_ids, "interpreter"), "Live")
        _join_booth(stack, room_id, _user_cookies(user_ids, "standby"), "Standby")

        response = _stop(
            room_id,
            cookies=_user_cookies(user_ids, "standby"),
            headers={"Authorization": f"Bearer {create_token()}"},
        )

    assert response.status_code == expected, response.text
    assert calls == (["stop"] if expected == 200 else [])
