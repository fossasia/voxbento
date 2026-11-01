"""Sign in with YouTube (#1087): Google OAuth, broadcast linking and room status.

Google is replaced by an ``httpx.MockTransport``; nothing here reaches the network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

import fastapi_app
from fastapi_app import app
from portal import program_ingest as ingest
from portal import program_relay as relay
from portal import youtube_account as yt
from portal.auth import create_user_token
from portal.config import Settings, settings
from portal.crypto import decrypt_val
from portal.database import configure, create_user, dispose, get_session, init_db, set_event_membership
from portal.models import Event, Room, YouTubeConnection
from portal.roles import EVENT_OWNER

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"
REFRESH_TOKEN = "1//refresh-token-SECRET-value"
ACCESS_TOKEN = "ya29.access-token-SECRET-value"
AUTH_CODE = "4/0auth-code-SECRET-value"
STREAM_KEY = "abcd-efgh-ijkl-mnop-qrst"
CAPTIONS_URL = "http://upload.youtube.com/closedcaption?cid=caption-SECRET-cid"
LIVE_ID = "LiveVideo01"
UPCOMING_ID = "Upcoming001"
LATER_ID = "Upcoming002"
UNBOUND_ID = "Unbound0001"
STREAM_ID = "stream-abc-123"


def _broadcast(video_id, status, start, stream_id=STREAM_ID):
    details = {"latencyPreference": "low", "closedCaptionsType": "closedCaptionsDisabled"}
    if stream_id:
        details["boundStreamId"] = stream_id
    return {
        "id": video_id,
        "snippet": {"title": f"Talk {video_id}", "scheduledStartTime": start},
        "contentDetails": details,
        "status": {"lifeCycleStatus": status},
    }


@pytest.fixture
async def yt_env(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(settings, "youtube_oauth_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "youtube_oauth_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(settings, "public_base_url", "http://test")
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "program_relay_enabled", True)
    monkeypatch.setattr(settings, "debug", True)
    yt._tokens.clear()
    yt._status.clear()
    yt._event_locks.clear()
    # Each test runs its own event loop; a lock that once had waiters stays bound to its loop.
    monkeypatch.setattr(yt, "connections_lock", asyncio.Lock())
    ingest.room_locks.clear()
    relay.relay_health.clear()
    google = {
        "calls": [],
        "revoked": [],
        "omit_refresh": False,
        "refresh_error": None,
        "channel_id": "UCchannel0001",
        "challenges": set(),
        # broadcastStatus -> further result pages, served after the first via nextPageToken.
        "extra_pages": {},
        "broadcasts": {
            # The "active" call also returns the upcoming one, to prove de-duplication.
            "active": [
                _broadcast(UPCOMING_ID, "ready", "2026-11-02T09:00:00Z"),
                _broadcast(LIVE_ID, "live", "2026-11-03T09:00:00Z"),
            ],
            "upcoming": [
                _broadcast(LATER_ID, "created", "2026-11-05T09:00:00Z"),
                _broadcast(UPCOMING_ID, "ready", "2026-11-02T09:00:00Z"),
                _broadcast(UNBOUND_ID, "created", "2026-11-06T09:00:00Z", stream_id=None),
            ],
        },
    }

    def by_id():
        found = {}
        for items in google["broadcasts"].values():
            found.update({item["id"]: item for item in items})
        return found

    def handler(request: httpx.Request) -> httpx.Response:
        google["calls"].append((request.method, request.url.host, request.url.path, dict(request.url.params)))
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            if request.url.path == "/revoke":
                google["revoked"].append(form["token"][0])
                return httpx.Response(200)
            assert form["client_id"] == [CLIENT_ID] and form["client_secret"] == [CLIENT_SECRET]
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == [AUTH_CODE]
                assert form["redirect_uri"] == ["http://test/youtube/oauth/callback"]
                challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest())
                assert challenge.rstrip(b"=").decode() in google["challenges"]
                body = {"access_token": ACCESS_TOKEN, "expires_in": 3599, "scope": yt.SCOPE, "token_type": "Bearer"}
                if not google["omit_refresh"]:
                    body["refresh_token"] = REFRESH_TOKEN
                return httpx.Response(200, json=body)
            assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == [REFRESH_TOKEN]
            if google["refresh_error"]:
                return httpx.Response(400, json={"error": google["refresh_error"], "error_description": "Bad Request"})
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599, "scope": yt.SCOPE})
        assert request.url.host == "www.googleapis.com"
        assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
        params = request.url.params
        resource = request.url.path.rsplit("/", 1)[1]
        if resource == "channels":
            assert params["mine"] == "true"
            return httpx.Response(
                200, json={"items": [{"id": google["channel_id"], "snippet": {"title": "Test Channel"}}]}
            )
        if resource == "liveBroadcasts":
            if google.get("broadcast_error"):
                error = {"errors": [{"reason": google["broadcast_error"]}], "message": f"secret {STREAM_KEY}"}
                return httpx.Response(403, json={"error": error})
            if "id" in params:
                item = by_id().get(params["id"])
                return httpx.Response(200, json={"items": [item] if item else []})
            # Google rejects mine=true together with broadcastStatus (incompatibleParameters).
            assert "mine" not in params
            status = params["broadcastStatus"]
            pages = [google["broadcasts"][status], *google["extra_pages"].get(status, [])]
            page = int(params.get("pageToken", "page0")[4:])
            body = {"items": pages[page]}
            if page + 1 < len(pages):
                body["nextPageToken"] = f"page{page + 1}"
            return httpx.Response(200, json=body)
        if resource == "liveStreams":
            assert params["id"] == STREAM_ID
            stream = {
                "id": STREAM_ID,
                "cdn": {
                    "ingestionInfo": {"ingestionAddress": "rtmp://a.rtmp.youtube.com/live2", "streamName": STREAM_KEY}
                },
                "contentDetails": {"closedCaptionsIngestionUrl": CAPTIONS_URL},
                "status": {"streamStatus": "active", "healthStatus": {"status": "good"}},
            }
            return httpx.Response(200, json={"items": [stream]})
        return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as google_client:
        monkeypatch.setattr(yt, "get_http_client", lambda: google_client)
        async with get_session() as session:
            owner = await create_user(session, email="yt-owner@example.com", display_name="Owner", email_verified=True)
            co_owner = await create_user(session, email="yt-co@example.com", display_name="Co", email_verified=True)
            stranger = await create_user(session, email="yt-x@example.com", display_name="X", email_verified=True)
            event = Event(slug="yt-test", display_name="YouTube Test")
            session.add(event)
            await session.flush()
            await set_event_membership(session, user_id=owner.id, event_id=event.id, role=EVENT_OWNER)
            await set_event_membership(session, user_id=co_owner.id, event_id=event.id, role=EVENT_OWNER)
            rooms = [
                Room(event_id=event.id, display_name="Relay room", program_ingest_mode=relay.RELAY_MODE),
                Room(event_id=event.id, display_name="WHIP room"),
            ]
            session.add_all(rooms)
        tokens = {
            user.email: create_user_token(user_id=user.id, email=user.email) for user in (owner, co_owner, stranger)
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://test",
            cookies={"user_token": tokens[owner.email]},
        ) as client:
            yield {
                "client": client,
                "google": google,
                "event": event,
                "rooms": rooms,
                "owner": owner,
                "tokens": tokens,
                "co_owner_token": tokens[co_owner.email],
                "stranger_token": tokens[stranger.email],
            }
    yt._tokens.clear()
    yt._status.clear()
    ingest.room_locks.clear()
    await dispose()


def event_url(env, suffix=""):
    return f"/workspace/events/{env['event'].id}/youtube{suffix}"


def broadcast_url(env, room):
    return f"/workspace/events/{env['event'].id}/rooms/{room.id}/youtube/broadcast"


async def start_connect(env, room=None):
    """Run the connect route; returns the Google state parameter. The state cookie stays in the jar."""
    client, room = env["client"], room or env["rooms"][0]
    response = await client.get(event_url(env, "/connect"), params={"room_id": room.id})
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    env["google"]["challenges"].add(query["code_challenge"][0])
    client.cookies.set("vb_yt_oauth", response.cookies["vb_yt_oauth"])
    return query["state"][0]


async def connect(env):
    state = await start_connect(env)
    response = await env["client"].get("/youtube/oauth/callback", params={"state": state, "code": AUTH_CODE})
    assert response.status_code == 303, response.text
    assert "youtube=connected" in response.headers["location"]
    env["client"].cookies.delete("vb_yt_oauth")
    return response


async def stored_connection(env):
    async with get_session() as session:
        return await yt.get_connection(session, env["event"].id)


async def stored_room(room_id):
    async with get_session() as session:
        return (await session.execute(select(Room).where(Room.id == room_id))).scalar_one()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("  https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10  ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/live/dQw4w9WgXcQ?feature=share", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://studio.youtube.com/video/dQw4w9WgXcQ/livestreaming", "dQw4w9WgXcQ"),
        ("http://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("https://evil.example/watch?v=dQw4w9WgXcQ", None),
        ("https://www.youtube.com.evil.example/watch?v=dQw4w9WgXcQ", None),
        ("https://user@www.youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", None),
        ("dQw4w9WgXc", None),
        ("https://youtu.be/dQw4w9WgXc", None),
        ("", None),
    ],
)
def test_parse_broadcast_id(text, expected):
    assert yt.parse_broadcast_id(text) == expected


def test_authorization_url_requests_offline_pkce_consent(monkeypatch):
    monkeypatch.setattr(settings, "youtube_oauth_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "public_base_url", "https://voxbento.example/")
    verifier, challenge = yt.pkce_pair()
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = yt.authorization_url("the-state", challenge)
    assert url.startswith(yt.AUTH_URL + "?")
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [challenge]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["https://www.googleapis.com/auth/youtube.force-ssl"]
    assert query["redirect_uri"] == ["https://voxbento.example/youtube/oauth/callback"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["state"] == ["the-state"]


def test_production_requires_both_client_settings():
    base = {
        "_env_file": None,
        "debug": False,
        "secret_key": "s" * 64,
        "mediamtx_auth_hook_secret": "m" * 32,
        "booth_access_token": "b" * 32,
        "api_key_encryption_key": "k" * 32,
    }
    Settings(**base).validate_production_secrets()
    Settings(
        **base, youtube_oauth_client_id=CLIENT_ID, youtube_oauth_client_secret=CLIENT_SECRET
    ).validate_production_secrets()
    with pytest.raises(RuntimeError, match="YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET"):
        Settings(**base, youtube_oauth_client_id=CLIENT_ID).validate_production_secrets()
    assert "test-client-secret" not in repr(Settings(_env_file=None, youtube_oauth_client_secret=CLIENT_SECRET))


@pytest.mark.anyio
async def test_connect_sets_state_cookie_and_redirects_to_google(yt_env):
    client, room = yt_env["client"], yt_env["rooms"][0]
    response = await client.get(event_url(yt_env, "/connect"), params={"room_id": room.id})
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert response.headers["cache-control"] == "no-store"
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith("vb_yt_oauth="))
    assert "HttpOnly" in cookie
    assert "Path=/youtube/oauth" in cookie
    assert "Max-Age=600" in cookie
    assert "samesite=lax" in cookie.lower()
    # A room from another event is refused.
    assert (await client.get(event_url(yt_env, "/connect"), params={"room_id": 999})).status_code == 404


@pytest.mark.anyio
async def test_callback_rejects_mismatched_state(yt_env):
    await start_connect(yt_env)
    response = await yt_env["client"].get("/youtube/oauth/callback", params={"state": "forged", "code": AUTH_CODE})
    assert response.status_code == 400
    assert await stored_connection(yt_env) is None
    assert not [call for call in yt_env["google"]["calls"] if call[2] == "/token"]
    # Without the state cookie (expired or never set) the callback is refused too.
    yt_env["client"].cookies.delete("vb_yt_oauth")
    assert (
        await yt_env["client"].get("/youtube/oauth/callback", params={"state": "x", "code": "y"})
    ).status_code == 400


@pytest.mark.anyio
async def test_callback_rejects_a_different_signed_in_user(yt_env):
    client = yt_env["client"]
    state = await start_connect(yt_env)
    # Another owner of the same event signs in before Google redirects back.
    client.cookies.set("user_token", yt_env["co_owner_token"])
    response = await client.get("/youtube/oauth/callback", params={"state": state, "code": AUTH_CODE})
    assert response.status_code == 403
    assert await stored_connection(yt_env) is None


@pytest.mark.anyio
async def test_callback_rechecks_ownership(yt_env):
    state = await start_connect(yt_env)
    async with get_session() as session:
        from portal.models import EventMembership

        await session.execute(EventMembership.__table__.delete().where(EventMembership.user_id == yt_env["owner"].id))
    response = await yt_env["client"].get("/youtube/oauth/callback", params={"state": state, "code": AUTH_CODE})
    assert response.status_code == 403
    assert await stored_connection(yt_env) is None


@pytest.mark.anyio
async def test_callback_stores_encrypted_refresh_token(yt_env):
    client, event, room = yt_env["client"], yt_env["event"], yt_env["rooms"][0]
    state = await start_connect(yt_env)
    response = await client.get("/youtube/oauth/callback", params={"state": state, "code": AUTH_CODE})
    assert response.status_code == 303
    location = response.headers["location"]
    assert location == f"/workspace/events/{event.id}/rooms/{room.id}/?youtube=connected#audio"
    assert not urlsplit(location).netloc
    cleared = next(c for c in response.headers.get_list("set-cookie") if c.startswith("vb_yt_oauth="))
    assert "Max-Age=0" in cleared and "Path=/youtube/oauth" in cleared
    connection = await stored_connection(yt_env)
    assert connection.channel_id == "UCchannel0001"
    assert connection.channel_title == "Test Channel"
    assert connection.connected_by_user_id == yt_env["owner"].id
    assert connection.refresh_token_enc != REFRESH_TOKEN
    assert decrypt_val(connection.refresh_token_enc) == REFRESH_TOKEN
    assert REFRESH_TOKEN not in repr(connection)
    status = (await client.get(event_url(yt_env))).json()
    assert status["available"] and status["connected"]
    assert status["channel_title"] == "Test Channel"
    assert REFRESH_TOKEN not in str(status)


@pytest.mark.anyio
async def test_callback_cancelled_and_missing_refresh_token(yt_env):
    client, google = yt_env["client"], yt_env["google"]
    state = await start_connect(yt_env)
    response = await client.get("/youtube/oauth/callback", params={"state": state, "error": "access_denied"})
    assert response.status_code == 303
    assert "youtube=cancelled" in response.headers["location"]
    google["omit_refresh"] = True
    state = await start_connect(yt_env)
    response = await client.get("/youtube/oauth/callback", params={"state": state, "code": AUTH_CODE})
    assert response.status_code == 303
    assert "youtube=error" in response.headers["location"]
    assert await stored_connection(yt_env) is None


@pytest.mark.anyio
async def test_reconnect_revokes_only_a_replaced_channel(yt_env):
    google = yt_env["google"]
    await connect(yt_env)
    await connect(yt_env)
    # Same channel: Google keeps one grant, so revoking the old token would kill the new one.
    assert google["revoked"] == []
    google["channel_id"] = "UCchannel0002"
    await connect(yt_env)
    assert google["revoked"] == [REFRESH_TOKEN]
    assert (await stored_connection(yt_env)).channel_id == "UCchannel0002"


@pytest.mark.anyio
async def test_broadcasts_merge_active_and_upcoming_live_first(yt_env):
    await connect(yt_env)
    response = await yt_env["client"].get(event_url(yt_env, "/broadcasts"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    ids = [b["id"] for b in response.json()["broadcasts"]]
    assert ids == [LIVE_ID, UPCOMING_ID, LATER_ID, UNBOUND_ID]
    first = response.json()["broadcasts"][0]
    assert first == {
        "id": LIVE_ID,
        "title": f"Talk {LIVE_ID}",
        "scheduled_start": "2026-11-03T09:00:00Z",
        "life_cycle_status": "live",
        "latency_preference": "low",
    }
    statuses = [
        call[3].get("broadcastStatus") for call in yt_env["google"]["calls"] if call[2].endswith("liveBroadcasts")
    ]
    assert statuses == ["active", "upcoming"]


async def link_relay_room(env):
    client, room = env["client"], env["rooms"][0]
    response = await client.put(
        broadcast_url(env, room), json={"broadcast": f"https://www.youtube.com/watch?v={LIVE_ID}"}
    )
    assert response.status_code == 200, response.text
    return response


@pytest.mark.anyio
async def test_linking_in_relay_mode_stores_the_destination(yt_env):
    await connect(yt_env)
    response = await link_relay_room(yt_env)
    body = response.json()
    assert body == {
        "broadcast_id": LIVE_ID,
        "title": f"Talk {LIVE_ID}",
        "life_cycle_status": "live",
        "stream_bound": True,
        "destination_configured": True,
    }
    assert STREAM_KEY not in response.text
    assert CAPTIONS_URL not in response.text
    room = await stored_room(yt_env["rooms"][0].id)
    assert room.youtube_broadcast_id == LIVE_ID
    assert room.youtube_stream_id == STREAM_ID
    # YouTube's plain-RTMP ingest address is upgraded to RTMPS.
    assert relay.destination(room) == f"rtmps://a.rtmps.youtube.com/live2/{STREAM_KEY}"
    status = await yt_env["client"].get(f"/workspace/events/{yt_env['event'].id}/rooms/{room.id}/program-ingest")
    assert STREAM_KEY not in status.text


@pytest.mark.anyio
async def test_linking_in_whip_mode_stores_no_destination(yt_env):
    await connect(yt_env)
    room = yt_env["rooms"][1]
    response = await yt_env["client"].put(
        broadcast_url(yt_env, room), json={"broadcast": f"https://youtu.be/{LIVE_ID}"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["destination_configured"] is False
    saved = await stored_room(room.id)
    assert saved.youtube_broadcast_id == LIVE_ID
    assert saved.program_relay_key_enc is None and saved.program_relay_server is None


@pytest.mark.anyio
async def test_link_rejects_foreign_unbound_and_malformed_broadcasts(yt_env):
    client, room = yt_env["client"], yt_env["rooms"][0]
    # Before signing in there is nothing to link with.
    assert (await client.put(broadcast_url(yt_env, room), json={"broadcast": LIVE_ID})).status_code == 409
    await connect(yt_env)
    response = await client.put(broadcast_url(yt_env, room), json={"broadcast": "NotOnChann1"})
    assert response.status_code == 422
    assert response.json()["detail"] == "That broadcast is not on the connected channel."
    response = await client.put(broadcast_url(yt_env, room), json={"broadcast": UNBOUND_ID})
    assert response.status_code == 422
    assert "does not have a stream bound" in response.json()["detail"]
    response = await client.put(broadcast_url(yt_env, room), json={"broadcast": "https://evil.example/watch?v=x"})
    assert response.status_code == 422
    saved = await stored_room(room.id)
    assert saved.youtube_broadcast_id is None and saved.program_relay_key_enc is None
    # A cross-site request is refused before anything else.
    response = await client.put(
        broadcast_url(yt_env, room), json={"broadcast": LIVE_ID}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_invalid_grant_needs_a_new_sign_in(yt_env):
    client, google = yt_env["client"], yt_env["google"]
    await connect(yt_env)
    yt._tokens.clear()
    google["refresh_error"] = "invalid_grant"
    response = await client.get(event_url(yt_env, "/broadcasts"))
    assert response.status_code == 409
    assert response.json()["last_error"] == yt.REAUTH_MESSAGE
    assert (await stored_connection(yt_env)).last_error == yt.REAUTH_MESSAGE
    assert (await client.get(event_url(yt_env))).json()["last_error"] == yt.REAUTH_MESSAGE
    response = await client.put(broadcast_url(yt_env, yt_env["rooms"][0]), json={"broadcast": LIVE_ID})
    assert response.status_code == 409
    # A working refresh clears the error again.
    google["refresh_error"] = None
    assert (await client.get(event_url(yt_env, "/broadcasts"))).status_code == 200
    assert (await stored_connection(yt_env)).last_error is None


@pytest.mark.anyio
async def test_disconnect_revokes_and_clears_rooms(yt_env):
    client, google = yt_env["client"], yt_env["google"]
    await connect(yt_env)
    await link_relay_room(yt_env)
    room = yt_env["rooms"][0]
    response = await client.delete(broadcast_url(yt_env, room))
    assert response.status_code == 200
    saved = await stored_room(room.id)
    assert saved.youtube_broadcast_id is None and saved.youtube_stream_id is None
    # Unlinking keeps the relay destination; the owner removes it explicitly.
    assert relay.destination(saved) is not None
    await link_relay_room(yt_env)
    response = await client.delete(event_url(yt_env))
    assert response.status_code == 200
    assert google["revoked"] == [REFRESH_TOKEN]
    assert await stored_connection(yt_env) is None
    saved = await stored_room(room.id)
    assert saved.youtube_broadcast_id is None and saved.youtube_stream_id is None
    assert yt._tokens == {} and room.id not in yt._status
    assert (await client.get(event_url(yt_env))).json()["connected"] is False


@pytest.mark.anyio
async def test_owner_routes_need_ownership_and_the_feature(yt_env, monkeypatch):
    client, event, room = yt_env["client"], yt_env["event"], yt_env["rooms"][0]
    await connect(yt_env)
    admin_paths = [
        ("GET", f"/admin/events/{event.id}/youtube"),
        ("GET", f"/admin/events/{event.id}/youtube/connect?room_id={room.id}"),
        ("GET", f"/admin/events/{event.id}/youtube/broadcasts"),
        ("PUT", f"/admin/events/{event.id}/rooms/{room.id}/youtube/broadcast"),
        ("DELETE", f"/admin/events/{event.id}/rooms/{room.id}/youtube/broadcast"),
        ("DELETE", f"/admin/events/{event.id}/youtube"),
    ]
    client.cookies.set("user_token", yt_env["stranger_token"])
    for method, path in admin_paths:
        response = await client.request(method, path, json={"broadcast": LIVE_ID} if method == "PUT" else None)
        assert response.status_code == 403, (method, path, response.status_code)
    client.cookies.delete("user_token")
    for method, path in admin_paths:
        response = await client.request(method, path, json={"broadcast": LIVE_ID} if method == "PUT" else None)
        assert response.status_code == 403, (method, path, response.status_code)
    assert await stored_connection(yt_env) is not None

    client.cookies.set("user_token", yt_env["tokens"]["yt-owner@example.com"])
    monkeypatch.setattr(settings, "youtube_oauth_client_secret", "")
    status = await client.get(event_url(yt_env))
    assert status.status_code == 200 and status.json() == {"available": False, "connected": False}
    for method, path in admin_paths[1:]:
        workspace_path = path.replace("/admin/", "/workspace/", 1)
        response = await client.request(
            method, workspace_path, json={"broadcast": LIVE_ID} if method == "PUT" else None
        )
        assert response.status_code == 404, (method, path, response.status_code)
    assert (await client.get("/youtube/oauth/callback", params={"state": "x", "code": "y"})).status_code == 404


@pytest.mark.anyio
async def test_room_status_includes_cached_youtube_block(yt_env, monkeypatch):
    client, google, room = yt_env["client"], yt_env["google"], yt_env["rooms"][0]
    status_url = f"/workspace/events/{yt_env['event'].id}/rooms/{room.id}/program-ingest"
    assert "youtube" not in (await client.get(status_url)).json()
    await connect(yt_env)
    await link_relay_room(yt_env)
    calls = len(google["calls"])
    youtube = (await client.get(status_url)).json()["youtube"]
    assert youtube["broadcast_id"] == LIVE_ID
    assert youtube["title"] == f"Talk {LIVE_ID}"
    assert youtube["life_cycle_status"] == "live"
    assert youtube["stream_status"] == "active"
    assert youtube["health"] == "good"
    assert youtube["latency_preference"] == "low"
    assert youtube["checked_at"]
    made = len(google["calls"]) - calls
    assert made == 2  # liveBroadcasts + liveStreams; the access token is cached
    assert (await client.get(status_url)).json()["youtube"] == youtube
    assert len(google["calls"]) - calls == made  # served from the 30 s cache

    yt._status.clear()
    yt._tokens.clear()
    google["refresh_error"] = "invalid_grant"
    assert (await client.get(status_url)).json()["youtube"] == {"error": yt.REAUTH_MESSAGE}

    async def boom(connection, room):
        raise RuntimeError(f"unexpected {STREAM_KEY}")

    monkeypatch.setattr(yt, "room_status", boom)
    assert (await client.get(status_url)).json()["youtube"] == {"error": "YouTube status unavailable."}


@pytest.mark.anyio
async def test_secrets_never_reach_the_logs(yt_env, caplog):
    caplog.set_level(logging.DEBUG)
    await connect(yt_env)
    await link_relay_room(yt_env)
    await yt_env["client"].get(event_url(yt_env, "/broadcasts"))
    await yt_env["client"].delete(event_url(yt_env))
    assert "action=broadcast_linked" in caplog.text
    for secret in (STREAM_KEY, REFRESH_TOKEN, ACCESS_TOKEN, AUTH_CODE, CAPTIONS_URL, CLIENT_SECRET):
        assert secret not in caplog.text, secret
    stream = yt.StreamInfo("s", "rtmp://a.rtmp.youtube.com/live2", "active", "good", STREAM_KEY, CAPTIONS_URL)
    assert STREAM_KEY not in repr(stream) and CAPTIONS_URL not in repr(stream)


def test_callback_code_is_redacted_from_access_logs():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        "GET /youtube/oauth/callback?code=SECRETCODE&state=x HTTP/1.1",
        None,
        None,
    )
    assert fastapi_app._UvicornTokenRedactor().filter(record)
    assert "SECRETCODE" not in record.getMessage()


@pytest.mark.anyio
async def test_youtube_connection_model_is_one_per_event(yt_env):
    await connect(yt_env)
    async with get_session() as session:
        rows = (await session.execute(select(YouTubeConnection))).scalars().all()
    assert len(rows) == 1


@pytest.mark.anyio
async def test_youtube_refusals_reach_the_page_in_plain_words(yt_env):
    client, google, room = yt_env["client"], yt_env["google"], yt_env["rooms"][1]
    await connect(yt_env)
    response = await client.put(broadcast_url(yt_env, room), json={"broadcast": LIVE_ID})
    assert response.status_code == 200
    google["broadcast_error"] = "liveStreamingNotEnabled"
    response = await client.get(event_url(yt_env, "/broadcasts"))
    assert response.status_code == 502
    assert response.json()["detail"] == yt.API_ERROR_MESSAGES["liveStreamingNotEnabled"]
    yt._status.clear()
    status_url = f"/workspace/events/{yt_env['event'].id}/rooms/{room.id}/program-ingest"
    youtube = (await client.get(status_url)).json()["youtube"]
    assert youtube == {"error": yt.API_ERROR_MESSAGES["liveStreamingNotEnabled"]}
    # An unknown reason shows the status and Google's code, never the raw body.
    google["broadcast_error"] = "somethingNew"
    response = await client.get(event_url(yt_env, "/broadcasts"))
    assert response.json()["detail"] == "YouTube refused the request (403, somethingNew). Try again."
    assert STREAM_KEY not in response.text


async def set_refresh_token_enc(env, value):
    async with get_session() as session:
        connection = await yt.get_connection(session, env["event"].id)
        connection.refresh_token_enc = value


@pytest.mark.anyio
async def test_unreadable_refresh_token_needs_a_new_sign_in(yt_env):
    client, room = yt_env["client"], yt_env["rooms"][1]
    await connect(yt_env)
    assert (await client.put(broadcast_url(yt_env, room), json={"broadcast": LIVE_ID})).status_code == 200
    # As after an API_KEY_ENCRYPTION_KEY change: the stored token no longer decrypts.
    await set_refresh_token_enc(yt_env, "not-a-fernet-token")
    yt._tokens.clear()
    yt._status.clear()
    response = await client.get(event_url(yt_env, "/broadcasts"))
    assert response.status_code == 409
    assert response.json() == {"detail": yt.REAUTH_MESSAGE, "last_error": yt.REAUTH_MESSAGE}
    assert (await stored_connection(yt_env)).last_error == yt.REAUTH_MESSAGE
    response = await client.put(broadcast_url(yt_env, room), json={"broadcast": LIVE_ID})
    assert response.status_code == 409
    status_url = f"/workspace/events/{yt_env['event'].id}/rooms/{room.id}/program-ingest"
    assert (await client.get(status_url)).json()["youtube"] == {"error": yt.REAUTH_MESSAGE}
    # Signing in again replaces the token and keeps the room's broadcast (same channel).
    await connect(yt_env)
    assert (await client.get(event_url(yt_env, "/broadcasts"))).status_code == 200
    assert (await stored_room(room.id)).youtube_broadcast_id == LIVE_ID


@pytest.mark.anyio
async def test_broadcast_list_follows_every_page(yt_env):
    yt_env["google"]["extra_pages"]["upcoming"] = [[_broadcast("Upcoming003", "created", "2026-11-07T09:00:00Z")]]
    await connect(yt_env)
    response = await yt_env["client"].get(event_url(yt_env, "/broadcasts"))
    assert [b["id"] for b in response.json()["broadcasts"]][-1] == "Upcoming003"
    tokens = [call[3].get("pageToken") for call in yt_env["google"]["calls"] if call[2].endswith("liveBroadcasts")]
    assert tokens == [None, None, "page1"]


@pytest.mark.anyio
async def test_reconnecting_another_channel_clears_room_links(yt_env):
    client, google = yt_env["client"], yt_env["google"]
    relay_room, whip_room = yt_env["rooms"]
    await connect(yt_env)
    await link_relay_room(yt_env)
    assert (await client.put(broadcast_url(yt_env, whip_room), json={"broadcast": LIVE_ID})).status_code == 200
    # The same channel again keeps every link and the filled-in stream key.
    await connect(yt_env)
    assert (await stored_room(relay_room.id)).youtube_broadcast_id == LIVE_ID
    assert relay.destination(await stored_room(relay_room.id)) is not None
    google["channel_id"] = "UCchannel0002"
    await connect(yt_env)
    for room_id in (relay_room.id, whip_room.id):
        saved = await stored_room(room_id)
        assert saved.youtube_broadcast_id is None and saved.youtube_stream_id is None
    # The old channel's stream key is gone too, so the relay cannot keep feeding the old channel.
    assert relay.destination(await stored_room(relay_room.id)) is None


@pytest.mark.anyio
async def test_cached_token_is_bound_to_the_stored_grant(yt_env):
    client, google = yt_env["client"], yt_env["google"]
    await connect(yt_env)
    assert (await client.get(event_url(yt_env, "/broadcasts"))).status_code == 200
    refreshes = sum(1 for call in google["calls"] if call[2] == "/token")
    # A refresh for a replaced connection finishing late must not be served to the new one.
    yt._tokens[yt_env["event"].id] = ("stale-access-token", time.monotonic() + 3600, "replaced-grant")
    assert (await client.get(event_url(yt_env, "/broadcasts"))).status_code == 200
    assert sum(1 for call in google["calls"] if call[2] == "/token") == refreshes + 1


def owner_client(env, state_cookie):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
        base_url="http://test",
        cookies={"user_token": env["tokens"]["yt-owner@example.com"], "vb_yt_oauth": state_cookie},
    )


@pytest.mark.anyio
async def test_concurrent_first_sign_ins_share_one_connection(yt_env):
    flows = []
    for _ in range(2):
        response = await yt_env["client"].get(event_url(yt_env, "/connect"), params={"room_id": yt_env["rooms"][0].id})
        query = parse_qs(urlsplit(response.headers["location"]).query)
        yt_env["google"]["challenges"].add(query["code_challenge"][0])
        flows.append((query["state"][0], response.cookies["vb_yt_oauth"]))
    clients = [owner_client(yt_env, cookie) for _, cookie in flows]
    try:
        responses = await asyncio.gather(
            *(
                c.get("/youtube/oauth/callback", params={"state": state, "code": AUTH_CODE})
                for c, (state, _) in zip(clients, flows, strict=True)
            )
        )
    finally:
        for c in clients:
            await c.aclose()
    assert [r.status_code for r in responses] == [303, 303]
    assert all("youtube=connected" in r.headers["location"] for r in responses)
    async with get_session() as session:
        assert len((await session.execute(select(YouTubeConnection))).scalars().all()) == 1


@pytest.mark.anyio
async def test_link_and_disconnect_do_not_interleave(yt_env):
    client, room = yt_env["client"], yt_env["rooms"][0]
    await connect(yt_env)
    link, disconnect = await asyncio.gather(
        client.put(broadcast_url(yt_env, room), json={"broadcast": LIVE_ID}), client.delete(event_url(yt_env))
    )
    assert disconnect.status_code == 200
    assert link.status_code in {200, 409}
    assert await stored_connection(yt_env) is None
    saved = await stored_room(room.id)
    assert saved.youtube_broadcast_id is None and saved.youtube_stream_id is None


@pytest.mark.anyio
async def test_concurrent_disconnects_of_a_shared_channel_revoke_once(yt_env):
    client, google = yt_env["client"], yt_env["google"]
    async with get_session() as session:
        second = Event(slug="yt-second", display_name="Second")
        session.add(second)
        await session.flush()
        await set_event_membership(session, user_id=yt_env["owner"].id, event_id=second.id, role=EVENT_OWNER)
        second_room = Room(event_id=second.id, display_name="Second room")
        session.add(second_room)
    await connect(yt_env)
    response = await client.get(f"/workspace/events/{second.id}/youtube/connect", params={"room_id": second_room.id})
    query = parse_qs(urlsplit(response.headers["location"]).query)
    google["challenges"].add(query["code_challenge"][0])
    client.cookies.set("vb_yt_oauth", response.cookies["vb_yt_oauth"])
    callback = await client.get("/youtube/oauth/callback", params={"state": query["state"][0], "code": AUTH_CODE})
    assert callback.status_code == 303
    client.cookies.delete("vb_yt_oauth")
    first, other = await asyncio.gather(
        client.delete(event_url(yt_env)), client.delete(f"/workspace/events/{second.id}/youtube")
    )
    assert first.status_code == other.status_code == 200
    # Both events shared one Google grant: the last disconnect revokes it, exactly once.
    assert google["revoked"] == [REFRESH_TOKEN]
