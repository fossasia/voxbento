"""Regression tests: booth broadcasts reach only booth-authorized sockets.

Sockets are added to the booth room at WebSocket-connect time, but
ConnectionManager.broadcast delivers only to sockets whose session carries
a role granted for that booth. A socket with no booth role — unauthenticated,
or holding only another booth's invitation — stays connected yet receives no
booth:state or booth:chat broadcasts, even when BOOTH_ACCESS_TOKEN is unset.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from fastapi_app import app
from portal.auth import create_participant_token, create_user_token
from portal.websockets.manager import manager

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_booth_token(monkeypatch):
    """Ensure BOOTH_ACCESS_TOKEN is unset for isolation tests without polluting other suites."""
    from portal.config import settings

    monkeypatch.setattr(settings, "booth_access_token", "")
    monkeypatch.setenv("BOOTH_ACCESS_TOKEN", "")


@pytest.fixture(autouse=True)
def no_membership_recheck(monkeypatch):
    """These tests cover delivery by granted role. The database recheck (installed on the
    shared manager by any test that starts the app lifespan) is covered in test_ws_auth_boundary."""
    monkeypatch.setattr(manager, "membership_checker", None)
    monkeypatch.setattr(manager, "token_checker", None)
    manager._allowed_cache.clear()
    manager._token_allowed_cache.clear()


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    """Give concurrent WebSocket sessions independent database transactions."""
    import anyio

    from portal.database import configure, dispose, init_db

    # In-memory SQLite shares one connection across TestClient threads. A
    # connecting socket's read can end the transaction creating the actor user.
    configure(f"sqlite+aiosqlite:///{tmp_path / 'broadcast-isolation.db'}")
    anyio.run(init_db)
    yield
    anyio.run(dispose)


def _invite_cookie(event_slug: str, room_id: int, language_code: str) -> dict:
    """Create a participant session token cookie for a booth."""
    tok = create_participant_token(
        booth_id=1,
        role="interpreter",
        event_slug=event_slug,
        room_id=room_id,
        language_code=language_code,
    )
    return {"session_token": tok}


def _admin_cookie() -> dict:
    """A user_token for a real, database-backed Admin.

    Admin authorization is read from the live user row at WebSocket connect
    time (#757), so the account must exist and be active — an ``is_admin``
    claim alone is not enough.
    """
    import anyio

    from portal.database import create_user, get_session, get_user_by_email

    async def admin_id() -> int:
        """Resolve or create an admin user and return their database ID."""
        async with get_session() as session:
            user = await get_user_by_email(session, "admin@test.com")
            if user is None:
                user = await create_user(session, email="admin@test.com", display_name="Admin", is_admin=True)
            return user.id

    return {"user_token": create_user_token(user_id=anyio.run(admin_id), email="admin@test.com", is_admin=True)}


def _join(ws, display_name: str = "Pat", role: str = "interpreter", language: str = "French") -> dict:
    """Send a booth join message and consume the join response and initial state."""
    ws.send_text(
        json.dumps(
            {
                "type": "booth:join",
                "display_name": display_name,
                "role": role,
                "language": language,
            }
        )
    )
    joined = json.loads(ws.receive_text())
    assert joined["type"] == "booth:joined"
    # A booth:state broadcast always follows a successful join; drain it so
    # later reads observe only subsequently triggered broadcasts.
    state = json.loads(ws.receive_text())
    assert state["type"] == "booth:state"
    return joined


def _room_size(bid: str) -> int:
    """Return the number of connected sockets in a booth room."""
    return len(manager._rooms.get(bid, set()))


def _try_receive(ws, timeout: float) -> list:
    """Collect messages for up to timeout seconds without hanging the test."""
    got: list = []
    stop = threading.Event()

    def _recv() -> None:
        """Thread worker to collect incoming WebSocket messages until stopped."""
        try:
            while not stop.is_set():
                got.append(ws.receive_text())
        except Exception:
            pass

    t = threading.Thread(target=_recv, daemon=True)
    t.start()
    t.join(timeout=timeout)
    stop.set()
    return got


def test_joined_participant_receives_chat_broadcast():
    """An authorized participant joins and receives its booth's broadcasts."""
    bid = "iso-11-en"
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=_invite_cookie("iso", 11, "en")) as ws:
        _join(ws)
        assert _room_size(bid) == 1
        ws.send_text(json.dumps({"type": "booth:chat", "body": "hello booth"}))
        chat = json.loads(ws.receive_text())
        assert chat["type"] == "booth:chat"
        assert chat["message"]["body"] == "hello booth"


def test_authorized_not_joined_still_receives_state():
    """An authorized socket observes membership changes before joining.

    Multi-party flows (standby/coordinator observers) depend on queued
    join broadcasts; booth authorization — not join state — gates delivery.
    """
    bid = "iso-12-en"
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as observer:
        with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as actor:
            _join(actor, display_name="Actor")
        got = _try_receive(observer, timeout=2.0)
        assert any(json.loads(m).get("type") == "booth:state" for m in got)


def test_unauthenticated_socket_receives_no_state_broadcast():
    """A socket with no session gets no booth:state."""
    bid = "iso-13-en"
    with client.websocket_connect(f"/ws/booth/{bid}") as passive:
        with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as actor:
            _join(actor, display_name="Actor")
        got = _try_receive(passive, timeout=2.0)
        assert got == []


def test_unauthenticated_socket_receives_no_chat_broadcast():
    """A socket with no session gets no booth:chat."""
    bid = "iso-14-en"
    with client.websocket_connect(f"/ws/booth/{bid}") as passive:
        with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as actor:
            _join(actor, display_name="Actor")
            actor.send_text(json.dumps({"type": "booth:chat", "body": "secret chatter"}))
            echo = json.loads(actor.receive_text())
            assert echo["type"] == "booth:chat"
        got = _try_receive(passive, timeout=2.0)
        assert got == []


def test_cross_booth_invite_receives_no_broadcasts():
    """An invite for booth A yields no broadcasts on booth B."""
    bid_b = "iso-15-fr"
    with client.websocket_connect(f"/ws/booth/{bid_b}", cookies=_invite_cookie("iso", 15, "en")) as passive:
        with client.websocket_connect(f"/ws/booth/{bid_b}", cookies=_admin_cookie()) as actor:
            _join(actor, display_name="Actor", language="German")
            actor.send_text(json.dumps({"type": "booth:chat", "body": "booth B private"}))
            assert json.loads(actor.receive_text())["type"] == "booth:chat"
        got = _try_receive(passive, timeout=2.0)
        assert got == []


def test_unauthorized_join_receives_nothing_afterwards():
    """A rejected join is followed by no broadcasts to that socket."""
    bid = "iso-16-en"
    with client.websocket_connect(f"/ws/booth/{bid}") as ws:
        ws.send_text(
            json.dumps({"type": "booth:join", "display_name": "Hacker", "role": "interpreter", "language": "French"})
        )
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "booth:error"
        with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as actor:
            _join(actor, display_name="Actor")
            actor.send_text(json.dumps({"type": "booth:chat", "body": "after rejected join"}))
        got = _try_receive(ws, timeout=2.0)
        assert got == []


def test_disconnect_leaves_no_stale_membership():
    """Join then disconnect removes the socket from the room."""
    import time

    bid = "iso-17-en"
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as ws:
        _join(ws)
        assert _room_size(bid) == 1
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if _room_size(bid) == 0:
            break
        time.sleep(0.05)
    assert _room_size(bid) == 0


def test_silent_socket_receives_nothing_at_connect_time():
    """A socket that connects into an already-active booth and never joins
    receives no booth data (connect-time delivery check).

    The unknown-type ping is a sync barrier, not a join: its booth:error
    reply proves the server task has run and manager.add() completed, so a
    subsequent broadcast cannot slip past before registration.
    """
    bid = "iso-19-en"
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as actor:
        _join(actor, display_name="Actor")
        with client.websocket_connect(f"/ws/booth/{bid}") as passive:
            passive.send_text(json.dumps({"type": "booth:ping-sync"}))
            sync_reply = json.loads(passive.receive_text())
            assert sync_reply["type"] == "booth:error"
            actor.send_text(json.dumps({"type": "booth:chat", "body": "after passive arrived"}))
            assert json.loads(actor.receive_text())["type"] == "booth:chat"
            got = _try_receive(passive, timeout=2.0)
            assert got == []


def test_coordinator_join_behavior_intact():
    """A coordinator join still works and receives broadcasts."""
    bid = "iso-18-en"
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as ws:
        _join(ws, display_name="Coord", role="room_coordinator", language="French")
        ws.send_text(json.dumps({"type": "booth:chat", "body": "coord note"}))
        assert json.loads(ws.receive_text())["type"] == "booth:chat"


def test_captions_listener_receives_sanitized_state_without_chat_or_participants():
    """Event listeners on /ws/captions receive only broadcast_unlocked & ingest_status.

    Private chat logs and participant rosters must never leak to listener feeds.
    """
    bid = "iso-20-en"
    with client.websocket_connect(f"/ws/captions/{bid}") as listener_ws:
        with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as coord:
            _join(coord, display_name="Coordinator", role="room_coordinator", language="English")
            coord.send_text(json.dumps({"type": "booth:chat", "body": "confidential interpreter talk"}))
            chat_msg = json.loads(coord.receive_text())
            assert chat_msg["type"] == "booth:chat"
            chat_state = json.loads(coord.receive_text())
            assert "participants" in chat_state["state"]
            assert "chat_messages" in chat_state["state"]

            # Coordinator toggles broadcast unlock
            coord.send_text(json.dumps({"type": "booth:set-broadcast-unlocked", "unlocked": True}))
            coord_unlocked_state = json.loads(coord.receive_text())
            assert coord_unlocked_state["type"] == "booth:state"
            assert "participants" in coord_unlocked_state["state"]
            assert "chat_messages" in coord_unlocked_state["state"]

        # Listener receives sanitized state
        got = [json.loads(m) for m in _try_receive(listener_ws, timeout=2.0)]
        state_msgs = [m for m in got if m.get("type") == "booth:state"]
        assert len(state_msgs) >= 1
        for msg in state_msgs:
            state = msg["state"]
            # The listener feed is a deliberately narrow projection: adding any
            # other booth field here would leak private coordination data.
            assert set(state) == {
                "booth_id",
                "event_slug",
                "room_id",
                "language_code",
                "broadcast_unlocked",
                "ingest_status",
            }
            assert "participants" not in state
            assert "chat_messages" not in state
            assert "active_interpreter_id" not in state
            assert "handoff_state" not in state
        # The listener also gets the locked status on connect; the latest one is unlocked.
        assert state_msgs[-1]["state"]["broadcast_unlocked"] is True


def test_unsupported_role_token_receives_no_broadcasts():
    """A socket holding an unsupported role token receives no private broadcasts."""
    bid = "iso-21-en"
    guest_tok = create_participant_token(
        booth_id=1,
        role="guest",
        event_slug="iso",
        room_id=21,
        language_code="en",
    )
    with client.websocket_connect(f"/ws/booth/{bid}", cookies={"session_token": guest_tok}) as guest_ws:
        guest_ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        reply = json.loads(guest_ws.receive_text())
        assert reply["type"] == "booth:error"

        with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as actor:
            _join(actor, display_name="Actor", language="English")
            actor.send_text(json.dumps({"type": "booth:chat", "body": "for members only"}))
            assert json.loads(actor.receive_text())["type"] == "booth:chat"

        got = _try_receive(guest_ws, timeout=2.0)
        assert got == []


def test_legitimate_observer_roles_continue_receiving_broadcasts():
    """Every supported role (admin, event_owner, room_coordinator, interpreter) receives broadcasts."""
    for i, role in enumerate(("admin", "event_owner", "room_coordinator", "interpreter"), start=30):
        bid = f"iso-{i}-en"
        if role == "admin":
            cookie = _admin_cookie()
        else:
            tok = create_participant_token(
                booth_id=1,
                role=role,
                event_slug="iso",
                room_id=i,
                language_code="en",
            )
            cookie = {"session_token": tok}

        with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as observer:
            observer.send_text(json.dumps({"type": "booth:ping-sync"}))
            assert json.loads(observer.receive_text())["type"] == "booth:error"

            with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as actor:
                _join(actor, display_name="Actor", language="English")

            got = _try_receive(observer, timeout=2.0)
            assert any(json.loads(m).get("type") == "booth:state" for m in got)


def test_revoked_session_receives_no_broadcasts():
    """A session whose authorization is revoked stops receiving private broadcasts."""
    bid = "iso-23-en"
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as ws:
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"

        # Locate the session in manager and revoke it
        room_sockets = list(manager._rooms.get(bid, set()))
        assert len(room_sockets) == 1
        target_ws = room_sockets[0]
        manager.revoke_session(target_ws)

        # Trigger a broadcast
        with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as actor:
            _join(actor, display_name="Actor", language="English")
            actor.send_text(json.dumps({"type": "booth:chat", "body": "after revocation"}))
            assert json.loads(actor.receive_text())["type"] == "booth:chat"

        # The revoked socket receives no broadcasts
        got = _try_receive(ws, timeout=2.0)
        assert got == []


def test_revoked_session_send_is_rejected_and_closed():
    """A revoked socket attempting to send is closed with 4003."""
    from starlette.websockets import WebSocketDisconnect

    bid = "iso-24-en"
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as ws:
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"

        room_sockets = list(manager._rooms.get(bid, set()))
        assert len(room_sockets) == 1
        target_ws = room_sockets[0]
        manager.revoke_session(target_ws)

        ws.send_text(json.dumps({"type": "booth:chat", "body": "blocked"}))
        err = json.loads(ws.receive_text())
        assert err["type"] == "booth:error"
        assert "revoked" in err["message"]
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 4003


def test_revoked_invite_session_receives_no_broadcasts():
    """An invite-authenticated socket (no account user_id) can still be revoked."""
    bid = "iso-25-en"
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=_invite_cookie("iso", 25, "en")) as ws:
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"

        room_sockets = list(manager._rooms.get(bid, set()))
        assert len(room_sockets) == 1
        target_ws = room_sockets[0]
        session = manager.get_session(target_ws)
        assert session is not None
        # Invite tokens carry a UUID ``sub``; they are not account sessions.
        assert session.user_id is None
        manager.revoke_session(target_ws)

        with client.websocket_connect(f"/ws/booth/{bid}", cookies=_admin_cookie()) as actor:
            _join(actor, display_name="Actor", language="English")
            actor.send_text(json.dumps({"type": "booth:chat", "body": "after invite revocation"}))
            assert json.loads(actor.receive_text())["type"] == "booth:chat"

        got = _try_receive(ws, timeout=2.0)
        assert got == []


def _seed_booth_user(email: str, slug: str = "revoke") -> tuple[int, str]:
    """Create an event/room/booth and an interpreter booth membership for *email*."""
    import anyio

    from portal.database import (
        create_booth,
        create_event,
        create_room,
        create_user,
        get_session,
        set_booth_membership,
    )

    async def seed() -> tuple[int, str]:
        """Seed event, room, booth, and user membership rows in the test database."""
        async with get_session() as session:
            event = await create_event(session, slug=slug, display_name="Revocation")
            room = await create_room(session, event_id=event.id, display_name="Revocation Room")
            booth = await create_booth(
                session,
                event_id=event.id,
                room_id=room.id,
                language_code="en",
                language_name="English",
            )
            user = await create_user(session, email=email, display_name="Target", password_hash="hash")
            await set_booth_membership(session, user_id=user.id, booth_id=booth.id, role="interpreter")
            return user.id, f"{slug}-{room.id}-en"

    return anyio.run(seed)


def test_deactivating_user_closes_live_booth_session():
    """Deactivating an account ends its live booth session immediately (#809).

    The Admin Users flow is the production authorization-change path; it must
    revoke sockets opened before the change, not only affect future logins.
    """
    from starlette.websockets import WebSocketDisconnect

    user_id, bid = _seed_booth_user("revoked@test.com")
    cookie = {"user_token": create_user_token(user_id=user_id, email="revoked@test.com")}
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as ws:
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"

        response = client.post(
            f"/admin/users/{user_id}/access",
            data={"field": "active", "value": "false"},
            cookies=_admin_cookie(),
            headers={"origin": "http://testserver", "accept": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 4003


def test_fixture_restores_booth_access_token(monkeypatch):
    """Verify test fixture isolation: settings.booth_access_token is empty in test scope."""
    from portal.config import settings

    assert settings.booth_access_token == ""


def test_deactivated_user_cannot_reconnect_over_websocket():
    """Assert deactivated user cannot reconnect over WebSocket with a valid token."""
    from starlette.websockets import WebSocketDisconnect

    user_id, bid = _seed_booth_user("reconnect@test.com")
    cookie = {"user_token": create_user_token(user_id=user_id, email="reconnect@test.com")}
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as ws:
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"

        response = client.post(
            f"/admin/users/{user_id}/access",
            data={"field": "active", "value": "false"},
            cookies=_admin_cookie(),
            headers={"origin": "http://testserver", "accept": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 4003

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie):
            pass
    assert exc_info.value.code == 4003


def test_active_user_and_invite_token_connect_successfully():
    """Assert active user with membership and invite token can connect."""
    user_id, bid = _seed_booth_user("active@test.com")
    cookie = {"user_token": create_user_token(user_id=user_id, email="active@test.com")}
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as ws:
        _join(ws, display_name="Active User", language="English")
        assert _room_size(bid) == 1

    from portal.booth_identity import parse_booth_id

    event_slug, room_id, lang_code = parse_booth_id(bid)
    invite_cookies = _invite_cookie(event_slug, room_id, lang_code)
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=invite_cookies) as ws:
        _join(ws, display_name="Invited User", language="English")
        assert _room_size(bid) == 1


def test_demoting_admin_closes_live_booth_session():
    """Demoting an admin ends its live booth session immediately."""
    from starlette.websockets import WebSocketDisconnect

    user_id, bid = _seed_booth_user("demoted_admin@test.com")
    # Grant admin access
    resp = client.post(
        f"/admin/users/{user_id}/access",
        data={"field": "admin", "value": "true"},
        cookies=_admin_cookie(),
        headers={"origin": "http://testserver", "accept": "application/json"},
    )
    assert resp.status_code == 200

    cookie = {"user_token": create_user_token(user_id=user_id, email="demoted_admin@test.com", is_admin=True)}
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as ws:
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"

        response = client.post(
            f"/admin/users/{user_id}/access",
            data={"field": "admin", "value": "false"},
            cookies=_admin_cookie(),
            headers={"origin": "http://testserver", "accept": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 4003


def test_revocation_during_connect_leaves_no_registered_session(monkeypatch):
    """A revocation landing between the access check and registration is not missed.

    Deactivating an account commits, then sweeps registered sockets. A socket
    still finishing its connect handshake is not registered yet, so the sweep
    cannot see it; it must detect the change itself and refuse to register.
    """
    from starlette.websockets import WebSocketDisconnect

    import portal.websockets.handlers as handlers

    user_id, bid = _seed_booth_user("race@test.com", slug="race")
    cookie = {"user_token": create_user_token(user_id=user_id, email="race@test.com")}

    async def _revoke_mid_connect(booth_id: str) -> str:
        """Sweep the user's sessions inside the connect window, then answer normally."""
        await manager.close_user_sessions(user_id)
        return "English"

    monkeypatch.setattr(handlers, "get_booth_language_name", _revoke_mid_connect)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as ws:
            # Without the fix this ping is answered, so the connection is live
            # and still registered for broadcasts.
            ws.send_text(json.dumps({"type": "booth:ping-sync"}))
            ws.receive_text()
    assert exc_info.value.code == 4003

    assert all(session.user_id != user_id for session in manager._sessions.values())
    assert not manager._rooms.get(bid, set())

    import anyio

    anyio.run(manager.broadcast, bid, {"type": "booth:chat", "message": "after the race"})
    assert not manager._rooms.get(bid, set())


def test_connect_without_revocation_still_registers_and_receives():
    """The control case: no revocation during connect registers and receives normally."""
    user_id, bid = _seed_booth_user("norace@test.com", slug="norace")
    cookie = {"user_token": create_user_token(user_id=user_id, email="norace@test.com")}
    epoch_before = manager.revocation_epoch(user_id)

    with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as ws:
        _join(ws, display_name="Control", language="English")

        registered = [manager.get_session(sock) for sock in manager._rooms.get(bid, set())]
        assert [session.user_id for session in registered if session is not None] == [user_id]

        ws.send_text(json.dumps({"type": "booth:chat", "body": "control"}))
        assert json.loads(ws.receive_text())["type"] == "booth:chat"

    assert manager.revocation_epoch(user_id) == epoch_before


def test_promotion_and_reactivation_do_not_close_live_booth_session():
    """Promoting or re-activating an account does not disconnect its live booth sessions."""
    user_id, bid = _seed_booth_user("keepalive@test.com")
    cookie = {"user_token": create_user_token(user_id=user_id, email="keepalive@test.com")}
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as ws:
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"

        # Promoting to admin must NOT close the live socket
        response = client.post(
            f"/admin/users/{user_id}/access",
            data={"field": "admin", "value": "true"},
            cookies=_admin_cookie(),
            headers={"origin": "http://testserver", "accept": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

        # Socket is still responsive
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"

        # Seed another user, deactivate, then reactivate with changed=True
        other_id, _ = _seed_booth_user("other_reactivate@test.com", slug="revoke-other")
        client.post(
            f"/admin/users/{other_id}/access",
            data={"field": "active", "value": "false"},
            cookies=_admin_cookie(),
            headers={"origin": "http://testserver", "accept": "application/json"},
        )
        reactivate_resp = client.post(
            f"/admin/users/{other_id}/access",
            data={"field": "active", "value": "true"},
            cookies=_admin_cookie(),
            headers={"origin": "http://testserver", "accept": "application/json"},
        )
        assert reactivate_resp.status_code == 200
        assert reactivate_resp.json()["ok"] is True

        # User's socket is still alive after other user's reactivation
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"


def test_retry_demotion_when_changed_false_still_closes_live_session():
    """An admin request with changed=False still revokes live sockets idempotently."""
    from starlette.websockets import WebSocketDisconnect

    user_id, bid = _seed_booth_user("retry_demote@test.com", slug="retry-demote")
    # Account is already non-admin (default)
    cookie = {"user_token": create_user_token(user_id=user_id, email="retry_demote@test.com")}
    with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as ws:
        ws.send_text(json.dumps({"type": "booth:ping-sync"}))
        assert json.loads(ws.receive_text())["type"] == "booth:error"

        # Demote admin when already not admin (changed=False)
        resp = client.post(
            f"/admin/users/{user_id}/access",
            data={"field": "admin", "value": "false"},
            cookies=_admin_cookie(),
            headers={"origin": "http://testserver", "accept": "application/json"},
        )
        assert resp.status_code == 200
        # The sweep still executed and closed the live socket!
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 4003


def test_remove_team_assignment_closes_live_booth_session():
    """Removing a user's booth membership immediately revokes their live booth WebSocket."""
    import anyio
    from starlette.websockets import WebSocketDisconnect

    from portal.database import get_session, list_booth_memberships_for_user
    from portal.team import remove_assignment

    user_id, bid = _seed_booth_user("member_removed@test.com", slug="rm-assign")
    cookie = {"user_token": create_user_token(user_id=user_id, email="member_removed@test.com")}

    with client.websocket_connect(f"/ws/booth/{bid}", cookies=cookie) as ws:
        _join(ws, display_name="Member", language="English")

        # Find membership id
        async def get_mid() -> tuple[int, int]:
            async with get_session() as s:
                bms = await list_booth_memberships_for_user(s, user_id)
                from portal.database import get_booth_by_id

                booth = await get_booth_by_id(s, bms[0].booth_id)
                return bms[0].id, booth.event_id

        mid, event_id = anyio.run(get_mid)

        async def remove_mem():
            async with get_session() as s:
                await remove_assignment(s, event_id=event_id, level="booth", membership_id=mid)

        anyio.run(remove_mem)

        # Socket was closed by push revocation
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 4003


def test_check_booth_memberships_real_db():
    """Verify batched membership checker against real database rows."""
    import anyio

    from portal.websockets.reaper import check_booth_memberships

    user_id, bid = _seed_booth_user("checker_test@test.com", slug="check-db")

    async def run_check():
        allowed = await check_booth_memberships(bid, {user_id, 99999})
        assert user_id in allowed
        assert 99999 not in allowed

    anyio.run(run_check)
