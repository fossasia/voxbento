"""Account recovery and access revocation must invalidate previously issued credentials."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fastapi_app import app
from portal.auth import create_listener_token, create_participant_token, create_user_token, decode_token, hash_password
from portal.config import settings
from portal.database import (
    configure,
    create_auth_token,
    create_booth,
    create_event,
    create_invite_token,
    create_room,
    create_user,
    dispose,
    get_session,
    get_user_by_id,
    init_db,
    set_event_membership,
)
from portal.models import Event, Room, User
from portal.roles import EVENT_OWNER, INTERPRETER
from portal.user_admin import set_user_access
from portal.websockets.manager import tts_manager

_PASSWORD_HASH = hash_password("old-recovery-password")


@pytest.fixture
def recovery(monkeypatch):
    @asynccontextmanager
    async def lifespan(app):
        # These tests exercise routes and database commits without external media workers.
        yield

    monkeypatch.setattr(app.router, "lifespan_context", lifespan)
    monkeypatch.setattr(settings, "booth_access_token", "recovery-test-access")

    async def setup():
        configure("sqlite+aiosqlite://")
        await init_db()
        async with get_session() as db:
            actor = await create_user(db, email="actor@example.com", display_name="Actor", is_admin=True)
            target = await create_user(
                db,
                email="target@example.com",
                display_name="Target",
                is_admin=True,
                email_verified=True,
                password_hash=_PASSWORD_HASH,
            )
            event = await create_event(db, slug="recovery-test", display_name="Private event")
            room = await create_room(db, event_id=event.id, display_name="Private room")
            booth = await create_booth(
                db,
                event_id=event.id,
                room_id=room.id,
                language_code="en",
                language_name="English",
            )
        return actor, target, event, room, booth

    rows = asyncio.run(setup())
    with TestClient(app, base_url="https://testserver") as client:
        yield client, rows
    asyncio.run(dispose())


def old_token(user: User) -> str:
    payload = decode_token(create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin))
    payload["iat"] = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp())
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


def reset_password(client: TestClient, user: User):
    async def issue_reset():
        async with get_session() as db:
            return await create_auth_token(db, user_id=user.id, token_type="password_reset")

    reset = asyncio.run(issue_reset())
    return client.post(f"/auth/reset/{reset.jti}", data={"password": "new-recovery-password"}, follow_redirects=False)


def tts_path(room: Room) -> str:
    return f"/ws/tts/{room.id}/en/recovery-test-{room.id}-en"


def broadcast(client: TestClient, room: Room) -> None:
    client.portal.call(
        tts_manager.broadcast_bundle,
        room.id,
        "en",
        f"recovery-test-{room.id}-en",
        b"private-pcm",
        "recovery-segment",
        1,
        "Confidential speech",
        "Private translation",
    )


@pytest.mark.parametrize("endpoint", ["/admin/users/", "/account", "/api/events/recovery-test/booths"])
def test_password_reset_rejects_old_cookie_and_preserves_recovery_session(recovery, endpoint):
    client, (_, target, _, _, _) = recovery
    stolen = old_token(target)
    client.cookies.set("user_token", stolen)
    assert client.get(endpoint, follow_redirects=False).status_code == 200
    client.cookies.clear()
    response = reset_password(client, target)
    assert response.status_code == 303
    assert client.get(endpoint, follow_redirects=False).status_code == 200
    client.cookies.clear()
    client.cookies.set("user_token", stolen)
    assert client.get(endpoint, follow_redirects=False).status_code in (303, 401, 403)


@pytest.mark.parametrize("change", ["admin", "active", "delete"])
@pytest.mark.parametrize("transport", ["query", "cookie"])
def test_revoked_admin_cannot_open_tts(recovery, change, transport):
    client, (actor, target, _, room, _) = recovery
    token = old_token(target)

    async def revoke():
        async with get_session() as db:
            if change == "delete":
                await db.delete(await get_user_by_id(db, target.id))
            else:
                await set_user_access(db, actor_id=actor.id, user_id=target.id, field=change, value=False)

    asyncio.run(revoke())
    path = tts_path(room)
    if transport == "query":
        path += "?token=" + token
    else:
        client.cookies.set("user_token", token)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path):
            pass
    assert exc.value.code == 4003


@pytest.mark.parametrize("credential", ["admin", "listener", "invite", "owner"])
def test_valid_tts_credentials_receive_bundles(recovery, credential):
    client, (_, target, event, room, booth) = recovery
    if credential == "listener":
        token = create_listener_token(event_slug=event.slug)
    elif credential == "invite":
        token = create_participant_token(
            booth_id=booth.id,
            role=INTERPRETER,
            event_slug=event.slug,
            room_id=room.id,
            language_code="en",
        )
    else:
        if credential == "owner":

            async def assign():
                async with get_session() as db:
                    user = await get_user_by_id(db, target.id)
                    user.is_admin = False
                    await set_event_membership(db, user_id=target.id, event_id=event.id, role=EVENT_OWNER)

            asyncio.run(assign())
        token = create_user_token(user_id=target.id, email=target.email, is_admin=credential == "admin")
        client.cookies.set("user_token", token)
    path = tts_path(room)
    if credential in ("listener", "invite"):
        path += "?token=" + token
    with client.websocket_connect(path) as socket:
        broadcast(client, room)
        frame = socket.receive_bytes()
        assert b"Confidential speech" in frame
        assert frame.endswith(b"private-pcm")


@pytest.mark.parametrize("change", ["reset", "admin", "active", "delete"])
def test_existing_tts_subscriber_receives_no_bundle_after_revocation(recovery, change):
    client, (actor, target, _, room, _) = recovery
    token = old_token(target)
    with client.websocket_connect(tts_path(room) + "?token=" + token) as socket:
        broadcast(client, room)
        assert socket.receive_bytes().endswith(b"private-pcm")
        if change == "reset":
            assert reset_password(client, target).status_code == 303
        else:

            async def revoke():
                async with get_session() as db:
                    if change == "delete":
                        await db.delete(await get_user_by_id(db, target.id))
                    else:
                        await set_user_access(db, actor_id=actor.id, user_id=target.id, field=change, value=False)

            asyncio.run(revoke())
        broadcast(client, room)
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_bytes()
        assert exc.value.code == 4003


def test_old_cookie_cannot_reconnect_to_booth_after_password_reset(recovery):
    client, (_, target, _, room, _) = recovery
    token = old_token(target)
    assert reset_password(client, target).status_code == 303
    client.cookies.clear()
    client.cookies.set("user_token", token)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/booth/recovery-test-{room.id}-en") as socket:
            socket.send_json({"type": "booth:join", "display_name": "Old session"})
            socket.receive_json()
    assert exc.value.code == 4003


@pytest.mark.parametrize("naive_cutoff", [False, True])
def test_new_session_in_same_second_as_revocation_is_usable(recovery, monkeypatch, naive_cutoff):
    import portal.auth as auth

    client, (_, target, _, _, _) = recovery
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=600000)

    async def revoke():
        async with get_session() as db:
            user = await get_user_by_id(db, target.id)
            user.sessions_valid_after = cutoff.replace(tzinfo=None) if naive_cutoff else cutoff

    asyncio.run(revoke())

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff + timedelta(microseconds=1)

    monkeypatch.setattr(auth, "datetime", Clock)
    token = create_user_token(user_id=target.id, email=target.email, is_admin=True)
    client.cookies.set("user_token", token)
    assert client.get("/admin/users/").status_code == 200
    payload = decode_token(token)
    payload["iat"] = int(cutoff.timestamp())
    client.cookies.clear()
    client.cookies.set("user_token", jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256"))
    assert client.get("/admin/users/", follow_redirects=False).status_code == 403


def test_password_reset_closes_existing_booth_socket(recovery):
    client, (_, target, _, room, _) = recovery
    client.cookies.set("user_token", old_token(target))
    with client.websocket_connect(f"/ws/booth/recovery-test-{room.id}-en") as socket:
        socket.send_json({"type": "booth:join", "display_name": "Old session"})
        assert socket.receive_json()["type"] == "booth:joined"
        client.cookies.clear()
        assert reset_password(client, target).status_code == 303
        with pytest.raises(WebSocketDisconnect) as exc:
            # Drain the initial state frame if it was queued before the reset.
            for _ in range(3):
                socket.receive_json()
        assert exc.value.code == 4003


def test_revoked_user_cookie_does_not_override_valid_listener_code(recovery):
    client, (actor, target, event, room, _) = recovery
    token = old_token(target)

    async def revoke():
        async with get_session() as db:
            await set_user_access(db, actor_id=actor.id, user_id=target.id, field="active", value=False)
            row = await db.get(Event, event.id)
            row.listener_join_code = "independent-listener-code"

    asyncio.run(revoke())
    client.cookies.set("user_token", token)
    client.cookies.set("listener_code_recovery-test", "independent-listener-code")
    with client.websocket_connect(tts_path(room)) as socket:
        broadcast(client, room)
        assert socket.receive_bytes().endswith(b"private-pcm")


def test_revoking_one_tts_subscriber_preserves_other_listeners(recovery):
    client, (actor, target, event, room, _) = recovery
    token = old_token(target)
    listener_token = create_listener_token(event_slug=event.slug)
    with (
        client.websocket_connect(tts_path(room) + "?token=" + token) as revoked_socket,
        client.websocket_connect(tts_path(room) + "?token=" + listener_token) as listener_socket,
    ):

        async def revoke():
            async with get_session() as db:
                await set_user_access(db, actor_id=actor.id, user_id=target.id, field="admin", value=False)

        asyncio.run(revoke())
        broadcast(client, room)
        assert listener_socket.receive_bytes().endswith(b"private-pcm")
        with pytest.raises(WebSocketDisconnect) as exc:
            revoked_socket.receive_bytes()
        assert exc.value.code == 4003


def test_existing_tts_subscriber_stops_when_token_expires(recovery, monkeypatch):
    client, (_, target, _, room, _) = recovery
    now = datetime.now(timezone.utc)
    payload = decode_token(old_token(target))
    payload["exp"] = (now + timedelta(seconds=60)).timestamp()
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now + timedelta(seconds=120)

    with client.websocket_connect(tts_path(room) + "?token=" + token) as socket:
        monkeypatch.setattr("jwt.api_jwt.datetime", Clock)
        broadcast(client, room)
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_bytes()
        assert exc.value.code == 4003


def test_reset_cookie_cannot_skip_password_on_hosting_invite(recovery):
    import hashlib

    from portal.models import TOKEN_TYPE_HOSTING_INVITE

    client, (_, target, _, _, _) = recovery
    stolen = old_token(target)
    raw_invite = "recovery-hosting-invite"

    async def invite():
        async with get_session() as db:
            return await create_auth_token(
                db,
                user_id=target.id,
                token_type=TOKEN_TYPE_HOSTING_INVITE,
                jti=hashlib.sha256(raw_invite.encode()).hexdigest(),
            )

    asyncio.run(invite())
    assert reset_password(client, target).status_code == 303
    client.cookies.clear()
    client.cookies.set("user_token", stolen)
    response = client.post(
        f"/invite/accept/{raw_invite}",
        data={},
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "user_token" not in response.cookies
    response = client.post(
        f"/invite/accept/{raw_invite}",
        data={"password": "new-recovery-password"},
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/admin/users/").status_code == 200


@pytest.mark.parametrize("channel", ["tts", "booth"])
@pytest.mark.parametrize("revoked_invite", [False, True])
def test_revoked_account_cookie_does_not_override_valid_invite(recovery, channel, revoked_invite):
    client, (_, target, event, room, booth) = recovery
    stolen = old_token(target)

    async def invitation():
        async with get_session() as db:
            token = await create_invite_token(db, booth_id=booth.id, role=INTERPRETER)
            if revoked_invite:
                token.revoked_at = datetime.now(timezone.utc)
            return token.public_id

    participant = create_participant_token(
        booth_id=booth.id,
        role=INTERPRETER,
        event_slug=event.slug,
        room_id=room.id,
        language_code="en",
        token_id=asyncio.run(invitation()),
    )
    assert reset_password(client, target).status_code == 303
    client.cookies.clear()
    client.cookies.set("user_token", stolen)
    client.cookies.set("session_token", participant)
    path = tts_path(room) if channel == "tts" else f"/ws/booth/recovery-test-{room.id}-en"
    if revoked_invite:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(path):
                pass
        assert exc.value.code == 4003
        return
    with client.websocket_connect(path) as socket:
        if channel == "tts":
            broadcast(client, room)
            assert socket.receive_bytes().endswith(b"private-pcm")
        else:
            socket.send_json({"type": "booth:join", "display_name": "Invited interpreter"})
            assert socket.receive_json()["type"] == "booth:joined"


@pytest.mark.anyio
async def test_tts_authorization_database_failure_disconnects_without_delivery():
    from sqlalchemy.exc import OperationalError

    from portal.websockets.manager import TTSConnectionManager

    class Socket:
        def __init__(self):
            self.frames = []
            self.closed = None

        async def send_bytes(self, data):
            self.frames.append(data)

        async def close(self, code=1000, reason=""):
            self.closed = code

    async def unavailable():
        raise OperationalError("SELECT users", {}, RuntimeError("database unavailable"))

    socket = Socket()
    manager = TTSConnectionManager()
    manager.add(socket, 1, "en", "recovery-test-1-en", authorize=unavailable)
    await manager.broadcast_bundle(1, "en", "recovery-test-1-en", b"private", "segment", 1)
    assert socket.frames == []
    assert socket.closed == 1011
    assert not manager.has_listeners(1, "en", "recovery-test-1-en")


def test_listener_routes_reject_reset_account_cookie_without_join_code(recovery):
    client, (_, target, _, room, _) = recovery
    stolen = old_token(target)
    assert reset_password(client, target).status_code == 303
    client.cookies.clear()
    client.cookies.set("user_token", stolen)
    response = client.get(f"/listener/recovery-test/rooms/{room.id}/audio-delay")
    assert response.status_code == 403
    page = client.get("/listener/recovery-test")
    assert "listener-event.js" not in page.text


def test_password_reset_invalidates_outstanding_magic_links(recovery):
    client, (_, target, _, _, _) = recovery

    async def issue_link():
        async with get_session() as db:
            return await create_auth_token(db, user_id=target.id, token_type="magic_link")

    link = asyncio.run(issue_link())
    assert reset_password(client, target).status_code == 303
    client.cookies.clear()
    response = client.get(f"/auth/magic/{link.jti}", follow_redirects=False)
    assert response.status_code == 400
    assert "user_token" not in response.cookies
    fresh_link = asyncio.run(issue_link())
    assert client.get(f"/auth/magic/{fresh_link.jti}", follow_redirects=False).status_code == 303
    assert client.get("/admin/users/").status_code == 200


def test_recovered_membership_is_hidden_from_interpreter_lobby(recovery):
    from portal.auth import resolve_booth_role

    client, (_, target, event, room, _) = recovery

    async def assign():
        async with get_session() as db:
            await set_event_membership(db, user_id=target.id, event_id=event.id, role=EVENT_OWNER)

    asyncio.run(assign())
    token = old_token(target)
    client.cookies.set("user_token", token)
    assert client.get("/interpreter").context["my_booths"]
    assert reset_password(client, target).status_code == 303
    client.cookies.clear()
    client.cookies.set("user_token", token)
    assert asyncio.run(resolve_booth_role(decode_token(token), f"{event.slug}-{room.id}-en")) is None
    response = client.get("/interpreter", follow_redirects=False)
    assert response.status_code == 303 or not response.context["my_booths"]


def test_recovered_admin_query_token_cannot_open_captions(recovery):
    from portal.websockets.manager import listener_manager

    client, (_, target, _, room, _) = recovery
    token = old_token(target)
    assert reset_password(client, target).status_code == 303
    booth_id = f"recovery-test-{room.id}-en"
    with client.websocket_connect(f"/ws/captions/{booth_id}?token={token}") as socket:
        client.portal.call(listener_manager.broadcast, booth_id, {"type": "caption", "text": "private"})
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
        assert exc.value.code == 4003


@pytest.mark.parametrize("revoked", [False, True])
def test_unrelated_active_account_does_not_shadow_tts_invite(recovery, revoked):
    client, (_, target, event, room, booth) = recovery

    async def prepare():
        async with get_session() as db:
            user = await get_user_by_id(db, target.id)
            user.is_admin = False
            invitation = await create_invite_token(db, booth_id=booth.id, role=INTERPRETER)
            if revoked:
                invitation.revoked_at = datetime.now(timezone.utc)
            return invitation.public_id

    participant = create_participant_token(
        booth_id=booth.id,
        role=INTERPRETER,
        event_slug=event.slug,
        room_id=room.id,
        language_code="en",
        token_id=asyncio.run(prepare()),
    )
    client.cookies.set("user_token", old_token(target))
    client.cookies.set("session_token", participant)
    if revoked:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(tts_path(room)):
                pass
    else:
        with client.websocket_connect(tts_path(room)) as socket:
            broadcast(client, room)
            assert socket.receive_bytes().endswith(b"private-pcm")


def test_tts_delivery_observes_invite_revocation_despite_cached_grant(recovery):
    from portal.auth import is_invite_token_revoked
    from portal.models import InviteToken

    client, (_, _, event, room, booth) = recovery

    async def invite():
        async with get_session() as db:
            return await create_invite_token(db, booth_id=booth.id, role=INTERPRETER)

    invitation = asyncio.run(invite())
    token = create_participant_token(
        booth_id=booth.id,
        role=INTERPRETER,
        event_slug=event.slug,
        room_id=room.id,
        language_code="en",
        token_id=invitation.public_id,
    )
    with client.websocket_connect(tts_path(room) + "?token=" + token) as socket:
        broadcast(client, room)
        assert socket.receive_bytes().endswith(b"private-pcm")
        assert not asyncio.run(is_invite_token_revoked(invitation.public_id))

        async def revoke():
            async with get_session() as db:
                row = await db.get(InviteToken, invitation.token)
                row.revoked_at = datetime.now(timezone.utc)

        asyncio.run(revoke())
        broadcast(client, room)
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_bytes()
        assert exc.value.code == 4003


@pytest.mark.parametrize("cutoff_before_login", [False, True])
def test_login_snapshot_cannot_mint_valid_session_after_recovery(recovery, monkeypatch, cutoff_before_login):
    import portal.routers.auth as routes

    client, (_, target, _, _, _) = recovery
    original = routes.get_user_by_email

    async def read_before_reset(db, email):
        snapshot = await original(db, email)
        async with get_session() as reset_db:
            row = await get_user_by_id(reset_db, target.id)
            row.password_hash = "replaced-password-hash"
            row.sessions_valid_after = datetime.now(timezone.utc) - timedelta(seconds=5 if cutoff_before_login else 0)
        return snapshot

    monkeypatch.setattr(routes, "get_user_by_email", read_before_reset)
    response = client.post(
        "/login", data={"email": target.email, "password": "old-recovery-password"}, follow_redirects=False
    )
    assert response.status_code in (303, 403)
    assert client.get("/admin/users/", follow_redirects=False).status_code == 403


def test_auth_token_redemption_rejects_stale_unused_snapshot(recovery, monkeypatch):
    import portal.database as database

    _, (_, target, _, _, _) = recovery

    async def redeem_twice():
        async with get_session() as db:
            token = await create_auth_token(db, user_id=target.id, token_type="password_reset")
        async with get_session() as db:
            stale = await database.get_auth_token(db, token.jti)
        async with get_session() as db:
            await database.redeem_auth_token(db, token.jti, "password_reset")

        async def stale_read(db, jti):
            return stale

        monkeypatch.setattr(database, "get_auth_token", stale_read)
        async with get_session() as db:
            with pytest.raises(ValueError, match="already been used"):
                await database.redeem_auth_token(db, token.jti, "password_reset")

    asyncio.run(redeem_twice())


@pytest.mark.anyio
async def test_tts_subscribers_do_not_wait_for_another_authorization():
    from portal.websockets.manager import TTSConnectionManager

    sent = asyncio.Event()
    release = asyncio.Event()

    class Socket:
        async def send_bytes(self, data):
            sent.set()

    async def slow_authorizer():
        await release.wait()

    slow, fast = Socket(), Socket()
    manager = TTSConnectionManager()
    manager.add(slow, 1, "en", "recovery-test-1-en", authorize=slow_authorizer)
    manager.add(fast, 1, "en", "recovery-test-1-en")
    # Use a deterministic iteration order so the slow subscriber is checked first.
    manager._rooms[manager._get_key(1, "en", "recovery-test-1-en")] = [slow, fast]
    task = asyncio.create_task(manager.broadcast_bundle(1, "en", "recovery-test-1-en", b"pcm", "segment", 1))
    try:
        await asyncio.wait_for(sent.wait(), timeout=0.2)
    finally:
        release.set()
        await task
