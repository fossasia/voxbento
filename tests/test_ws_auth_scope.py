"""Scope checks in resolve_ws_auth for the booth WebSocket (#558, split out of #636).

Every credential that carries an event, language or room must be valid for the
booth it is presented on, however it arrives (query token or cookie) and
whether or not BOOTH_ACCESS_TOKEN is configured. Anonymous access, where it is
allowed, must stay exactly as it was.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

from fastapi_app import app
from portal.auth import WSAuthError, create_listener_token, create_participant_token, resolve_ws_auth
from portal.config import settings

client = TestClient(app)

BOOTH = "conf-1-en"


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    import anyio

    from portal.database import configure, dispose, init_db

    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "booth_access_token", "")
    configure("sqlite+aiosqlite://")
    anyio.run(init_db)
    yield
    anyio.run(dispose)


def _participant(event_slug: str = "conf", language_code: str = "en", room_id: int = 1, role: str = "interpreter") -> str:
    return create_participant_token(
        booth_id=1,
        role=role,
        event_slug=event_slug,
        room_id=room_id,
        language_code=language_code,
    )


def _forged(**claims) -> str:
    """A validly signed token whose claims no token factory would produce."""
    now = datetime.now(timezone.utc)
    payload = {"sub": "forged", "iat": now, "exp": now + timedelta(minutes=5), **claims}
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


def _join(ws, role: str = "interpreter") -> dict:
    ws.send_text(
        json.dumps({"type": "booth:join", "display_name": "Ana", "role": role, "language": "English", "channel_id": "x"})
    )
    return json.loads(ws.receive_text())


def _refused_with(path: str, **kwargs) -> int:
    """Close code the server refused the handshake with; fails if the socket was accepted."""
    try:
        with client.websocket_connect(path, **kwargs) as ws:
            # An accepted socket would otherwise leave receive_text() waiting forever.
            accepted_reply = _join(ws)
    except WebSocketDisconnect as exc:
        return exc.code
    pytest.fail(f"the socket was accepted instead of refused: {accepted_reply}")


def _resolver_socket(booth_token: str) -> SimpleNamespace:
    """The minimum WebSocket surface resolve_ws_auth reads on the query-token path."""
    return SimpleNamespace(
        query_params={"token": booth_token},
        headers={},
        cookies={},
        url=SimpleNamespace(netloc="testserver"),
        close=AsyncMock(),
    )


async def _resolve(booth_id: str, token: str):
    websocket = _resolver_socket(token)
    try:
        return await resolve_ws_auth(websocket, booth_id), websocket.close
    except WSAuthError:
        return None, websocket.close


# -- a participant credential is valid only for its own booth ------------------------------------


@pytest.mark.parametrize(
    ("event_slug", "language_code", "room_id"),
    [("other", "en", 1), ("conf", "fr", 1), ("conf", "en", 2)],
)
def test_a_participant_token_is_refused_on_a_booth_it_was_not_issued_for(event_slug, language_code, room_id):
    token = _participant(event_slug=event_slug, language_code=language_code, room_id=room_id)

    assert _refused_with(f"/ws/booth/{BOOTH}?token={token}") == 4003


def test_a_participant_token_is_accepted_on_its_own_booth():
    with client.websocket_connect(f"/ws/booth/{BOOTH}?token={_participant()}") as ws:
        assert _join(ws)["type"] == "booth:joined"


# -- a role claim must come with the scope it applies to -----------------------------------------


@pytest.mark.parametrize(
    "claims",
    [
        {"role": "event_owner"},
        {"role": "event_owner", "event_slug": "conf"},
        {"role": "event_owner", "language_code": "en"},
    ],
    ids=["no-scope", "no-language", "no-event"],
)
def test_a_role_claim_without_its_event_and_language_is_refused(claims):
    assert _refused_with(f"/ws/booth/{BOOTH}?token={_forged(**claims)}") == 4003


def test_the_legacy_access_token_is_unaffected_by_the_role_scope_rule(monkeypatch):
    """create_token() carries no role, so it still falls through to the cookie path."""
    monkeypatch.setattr(settings, "booth_access_token", "legacy-secret")
    legacy = client.post("/api/auth/token", json={"token": "legacy-secret"}).json()["access_token"]

    # No cookie, so the cookie path refuses it for want of a session, not for its scope.
    assert _refused_with(f"/ws/booth/{BOOTH}?token={legacy}") == 4001


# -- a cookie is held to the same scope, with or without BOOTH_ACCESS_TOKEN ------------------------


def test_a_role_cookie_without_its_event_and_language_holds_no_role_without_an_access_token():
    """The same scope-less role as a cookie: connected, as #810 has it, but granted nothing."""
    cookies = {"session_token": _forged(role="event_owner")}

    with client.websocket_connect(f"/ws/booth/{BOOTH}", cookies=cookies) as ws:
        reply = _join(ws)

    assert reply["type"] == "booth:error"
    assert "No role" in reply["message"]


def test_a_role_cookie_without_its_event_and_language_is_refused_when_an_access_token_is_set(monkeypatch):
    monkeypatch.setattr(settings, "booth_access_token", "legacy-secret")

    code = _refused_with(f"/ws/booth/{BOOTH}", cookies={"session_token": _forged(role="event_owner")})

    assert code == 4003


def test_a_cookie_for_another_booth_is_refused_when_an_access_token_is_set(monkeypatch):
    monkeypatch.setattr(settings, "booth_access_token", "legacy-secret")

    code = _refused_with(f"/ws/booth/{BOOTH}", cookies={"session_token": _participant(event_slug="other")})

    assert code == 4003


def test_a_cookie_for_another_booth_stays_connected_but_holds_no_role_without_an_access_token():
    """The #810 contract: no handshake refusal, but no role and so no broadcasts."""
    with client.websocket_connect(f"/ws/booth/{BOOTH}", cookies={"session_token": _participant(event_slug="other")}) as ws:
        reply = _join(ws)

    assert reply["type"] == "booth:error"
    assert "No role" in reply["message"]


@pytest.mark.parametrize("access_token", ["", "legacy-secret"], ids=["no-access-token", "access-token-set"])
def test_a_cookie_for_its_own_booth_is_accepted(monkeypatch, access_token):
    monkeypatch.setattr(settings, "booth_access_token", access_token)

    with client.websocket_connect(f"/ws/booth/{BOOTH}", cookies={"session_token": _participant()}) as ws:
        assert _join(ws)["type"] == "booth:joined"


def test_an_anonymous_socket_is_unchanged_when_no_access_token_is_configured():
    """Role-less sockets stay accepted and are told so; #936 builds on exactly this."""
    with client.websocket_connect(f"/ws/booth/{BOOTH}") as ws:
        reply = _join(ws)

    assert reply["type"] == "booth:error"
    assert "No role" in reply["message"]


# -- a listener credential is valid for its own event only ---------------------------------------


@pytest.mark.anyio
async def test_a_listener_token_is_accepted_for_its_own_event():
    payload, close = await _resolve(BOOTH, create_listener_token(event_slug="conf"))

    assert payload is not None
    close.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "booth_id",
    ["other-1-en", "conf-private-3-en", "conference-1-en"],
    ids=["other-event", "event-sharing-a-prefix", "slug-sharing-a-prefix"],
)
async def test_a_listener_token_is_refused_for_a_different_event(booth_id):
    """A hyphenated slug makes a prefix test admit "conf" on "conf-private" (#558)."""
    payload, close = await _resolve(booth_id, create_listener_token(event_slug="conf"))

    assert payload is None
    close.assert_awaited_once_with(code=4003)
