"""Regression coverage for overlay authorization and the complete channel catalog."""

from __future__ import annotations

import re
import types

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fastapi_app import app
from portal import join_code_limit
from portal.auth import create_user_token, decode_token
from portal.booth_identity import ISO_639_1_CODES
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
    set_event_membership,
)
from portal.models import BoothTranslationLanguage, Event, Room, RoomTranslationLanguage, User
from portal.roles import EVENT_OWNER, INTERPRETER, ROOM_COORDINATOR


@pytest.fixture(autouse=True)
async def database():
    configure("sqlite+aiosqlite://")
    await init_db()
    join_code_limit._join_code_attempts.clear()
    yield
    join_code_limit._join_code_attempts.clear()
    await dispose()


@pytest.fixture
async def channel():
    async with get_session() as s:
        event = await create_event(s, slug="overlay-test", display_name="Overlay test")
        event.listener_join_code = "ROOM42"
        room = await create_room(s, event_id=event.id, display_name="Main hall")
        booth = await create_booth(s, event_id=event.id, room_id=room.id, language_code="en", language_name="English")
    return event, room, booth


def client():
    return AsyncClient(transport=ASGITransport(app, client=("testclient", 5000)), base_url="http://testserver")


@pytest.mark.anyio
@pytest.mark.parametrize("role", [EVENT_OWNER, ROOM_COORDINATOR, INTERPRETER, None])
async def test_caption_builder_requires_ownership_of_this_event(channel, role):
    event, _, _ = channel
    async with get_session() as s:
        user = await create_user(s, email="builder@example.com", display_name="Builder")
        other = await create_event(s, slug="other-event", display_name="Other event")
        await set_event_membership(s, user_id=user.id, event_id=other.id, role=EVENT_OWNER)
        if role:
            await set_event_membership(s, user_id=user.id, event_id=event.id, role=role)
    async with client() as c:
        c.cookies.set("user_token", create_user_token(user_id=user.id, email=user.email))
        response = await c.get(f"/workspace/events/{event.id}/caption-overlays/")
        assert response.status_code == (200 if role == EVENT_OWNER else 403)
        if role == EVENT_OWNER:
            assert f"/workspace/events/{event.id}/" in response.text
            legacy = await c.get(f"/admin/events/{event.id}/caption-overlays/")
            assert legacy.status_code == 307
            assert legacy.headers["location"] == f"/workspace/events/{event.id}/caption-overlays/"
        else:
            assert "ROOM42" not in response.text
        c.cookies.clear()
        anonymous = await c.get(f"/workspace/events/{event.id}/caption-overlays/")
        assert anonymous.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize("missing", ["code", "channels", "event"])
async def test_caption_builder_empty_states_and_missing_event(channel, missing):
    event, _, booth = channel
    async with get_session() as s:
        admin = await create_user(s, email="builder-admin@example.com", display_name="Admin", is_admin=True)
        if missing == "code":
            stored = await s.get(Event, event.id)
            stored.listener_join_code = None
        elif missing == "channels":
            stored = await s.get(type(booth), booth.id)
            await s.delete(stored)
    async with client() as c:
        c.cookies.set("user_token", create_user_token(user_id=admin.id, email=admin.email, is_admin=True))
        event_id = event.id if missing != "event" else event.id + 1000
        response = await c.get(f"/workspace/events/{event_id}/caption-overlays/")
        assert response.status_code == (404 if missing == "event" else 200)
        assert "overlay-preview-frame" not in response.text
        if missing == "code":
            assert "Go to listener access" in response.text
        elif missing == "channels":
            assert "Open rooms &amp; booths" in response.text


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["active", "inactive", "deleted"])
async def test_overlay_and_refresh_validate_live_account(channel, state):
    event, room, booth = channel
    async with get_session() as s:
        user = await create_user(s, email="listener@example.com", display_name="Listener", password_hash="x")
    cookie = create_user_token(user_id=user.id, email=user.email)
    async with get_session() as s:
        stored = await s.get(User, user.id)
        if state == "inactive":
            stored.is_active = False
        elif state == "deleted":
            await s.delete(stored)
    path = f"/overlay/{event.slug}/{room.id}/{booth.language_code}"
    async with client() as c:
        c.cookies.set("user_token", cookie)
        for suffix in ("", "/session"):
            response = await c.get(path + suffix)
            assert response.status_code == (200 if state == "active" else 403)
            if state != "active":
                assert "data-token" not in response.text
                assert '"token"' not in response.text
        # Possession of the event code is an independent authorization method.
        assert (await c.get(path + "/session?code=ROOM42")).status_code == 200


@pytest.mark.anyio
async def test_session_rechecks_code_and_channel_and_issues_fresh_token(channel):
    event, room, _ = channel
    path = f"/overlay/{event.slug}/{room.id}/en"
    async with client() as c:
        page = await c.get(path + "?code=ROOM42")
        old = re.search(r'data-token="([^"]+)"', page.text).group(1)
        response = await c.get(path + "/session?code=ROOM42")
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        new = response.json()["token"]
        assert new != old
        assert decode_token(new)["event_slug"] == event.slug
        assert response.json()["booth_id"] == f"{event.slug}-{room.id}-en"
        async with get_session() as s:
            ev = await s.get(Event, event.id)
            ev.listener_join_code = "ROTATED"
        assert (await c.get(path + "/session?code=ROOM42")).status_code == 403
        assert (await c.get(path + "/session?code=ROTATED")).status_code == 200
        assert (await c.get(f"/overlay/{event.slug}/{room.id}/zz/session?code=ROTATED")).status_code == 404


@pytest.mark.anyio
async def test_shared_budget_across_http_and_websocket(channel):
    event, room, _ = channel
    bid = f"{event.slug}-{room.id}-en"
    tc = TestClient(app)
    async with client() as c:
        for _ in range(5):
            assert (await c.get(f"/overlay/{event.slug}/{room.id}/en?code=WRONG")).status_code == 403
        for _ in range(5):
            with pytest.raises(WebSocketDisconnect) as exc:
                with tc.websocket_connect(f"/ws/captions/{bid}?code=WRONG") as ws:
                    ws.receive_text()
            assert exc.value.code == 4001
        for path in (
            f"/overlay/{event.slug}/{room.id}/en",
            f"/overlay/{event.slug}/{room.id}/en/session",
            f"/listener/{event.slug}",
        ):
            assert (await c.get(path + "?code=ROOM42")).status_code == 429
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect(f"/ws/captions/{bid}?code=ROOM42") as ws:
                ws.receive_text()
        assert exc.value.code == 1008
        async with get_session() as s:
            user = await create_user(s, email="active@example.com", display_name="Active", password_hash="x")
        cookie = create_user_token(user_id=user.id, email=user.email)
        c.cookies.set("user_token", cookie)
        assert (await c.get(f"/overlay/{event.slug}/{room.id}/en/session")).status_code == 200
        with tc.websocket_connect(f"/ws/captions/{bid}", cookies={"user_token": cookie}) as ws:
            ws.close()


@pytest.mark.anyio
async def test_budget_does_not_compare_blocked_guesses_and_bounds_storage(monkeypatch):
    for _ in range(10):
        assert not join_code_limit.verify_join_code("client", "bad", "correct")

    def unexpected_comparison(*args):
        pytest.fail("Compared a guess after the attempt budget was exhausted")

    with monkeypatch.context() as m:
        m.setattr(join_code_limit.secrets, "compare_digest", unexpected_comparison)
        with pytest.raises(join_code_limit.JoinCodeThrottled):
            join_code_limit.verify_join_code("client", "correct", "correct")
    # A full table evicts the oldest window instead of locking out new clients.
    monkeypatch.setattr(join_code_limit, "MAX_CLIENTS", 1)
    assert join_code_limit.verify_join_code("second-client", "correct", "correct")
    assert not join_code_limit.verify_join_code("second-client", "bad", "correct")
    assert list(join_code_limit._join_code_attempts) == ["second-client"]


@pytest.mark.anyio
async def test_correct_code_does_not_refill_budget_for_other_events():
    for _ in range(9):
        assert not join_code_limit.verify_join_code("192.0.2.1", "guess", "victim-code")
    assert join_code_limit.verify_join_code("192.0.2.1", "own-code", "own-code")
    assert not join_code_limit.verify_join_code("192.0.2.1", "guess", "victim-code")
    with pytest.raises(join_code_limit.JoinCodeThrottled):
        join_code_limit.verify_join_code("192.0.2.1", "guess", "victim-code")


@pytest.mark.anyio
async def test_budget_groups_ipv6_clients_by_64_prefix():
    for i in range(10):
        assert not join_code_limit.verify_join_code(f"2001:db8:1:2::{i + 1:x}", "bad", "correct")
    with pytest.raises(join_code_limit.JoinCodeThrottled):
        join_code_limit.verify_join_code("2001:db8:1:2:ffff::1", "correct", "correct")
    assert join_code_limit.verify_join_code("2001:db8:1:3::1", "correct", "correct")
    assert not join_code_limit.verify_join_code("::ffff:192.0.2.1", "bad", "correct")
    assert list(join_code_limit._join_code_attempts) == ["2001:db8:1:2::/64", "192.0.2.1"]


@pytest.mark.anyio
async def test_budget_purges_expired_windows(monkeypatch):
    assert not join_code_limit.verify_join_code("192.0.2.1", "bad", "correct")
    now = join_code_limit.time.monotonic()
    monkeypatch.setattr(join_code_limit, "time", types.SimpleNamespace(monotonic=lambda: now + 61))
    assert not join_code_limit.verify_join_code("192.0.2.2", "bad", "correct")
    assert list(join_code_limit._join_code_attempts) == ["192.0.2.2"]


@pytest.mark.anyio
async def test_every_supported_language_and_translation_only_channels(channel):
    event, room, booth = channel
    async with get_session() as s:
        db_room = await s.get(Room, room.id)
        db_room.floor_transcription_enabled = True
        db_room.floor_transcription_provider = "local"
        db_room.floor_translation_enabled = True
        for code in sorted(ISO_639_1_CODES):
            s.add(
                RoomTranslationLanguage(
                    room_id=room.id, language_code=code, language_name=f"Language {code}", enabled=True
                )
            )
        # A booth translation target in another room must also be discoverable.
        other = await create_room(s, event_id=event.id, display_name="Second hall")
        source = await create_booth(s, event_id=event.id, room_id=other.id, language_code="en", language_name="English")
        source.translation_enabled = True
        s.add(BoothTranslationLanguage(booth_id=source.id, language_code="ar", language_name="Arabic", enabled=True))
        admin = await create_user(s, email="owner@example.com", display_name="Owner", password_hash="x", is_admin=True)
    async with client() as c:
        c.cookies.set("user_token", create_user_token(user_id=admin.id, email=admin.email, is_admin=True))
        builder = await c.get(f"/workspace/events/{event.id}/caption-overlays/")
        assert builder.status_code == 200
        for code in sorted(ISO_639_1_CODES) + ["floor"]:
            assert f"value='{room.id}/{code}'" in builder.text
            response = await c.get(f"/overlay/{event.slug}/{room.id}/{code}/session?code=ROOM42")
            assert response.status_code == 200, code
            assert response.json()["booth_id"] == f"{event.slug}-{room.id}-{code}"
        assert f"value='{other.id}/ar'" in builder.text
        assert (await c.get(f"/overlay/{event.slug}/{other.id}/ar?code=ROOM42")).status_code == 200
        room_page = await c.get(f"/workspace/events/{event.id}/rooms/{other.id}/")
        assert f"/overlay/{event.slug}/{other.id}/ar?code=ROOM42" in room_page.text
        # Disabling a translation-only language removes it from access and selectors.
        from sqlalchemy import select

        async with get_session() as s:
            lang = await s.scalar(
                select(BoothTranslationLanguage).where(BoothTranslationLanguage.booth_id == source.id)
            )
            lang.enabled = False
        assert (await c.get(f"/overlay/{event.slug}/{other.id}/ar/session?code=ROOM42")).status_code == 404


@pytest.mark.anyio
async def test_caption_query_tokens_validate_account_and_participant_scope(channel):
    from portal.auth import create_participant_token
    from portal.roles import INTERPRETER

    event, room, booth = channel
    bid = f"{event.slug}-{room.id}-en"
    async with get_session() as s:
        user = await create_user(
            s, email="former-admin@example.com", display_name="Former admin", password_hash="x", is_admin=True
        )
        user.is_active = False
    token = create_user_token(user_id=user.id, email=user.email, is_admin=True)
    tc = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect(f"/ws/captions/{bid}?token={token}") as ws:
            ws.receive_text()
    assert exc.value.code == 4001
    invite = create_participant_token(
        booth_id=booth.id, role=INTERPRETER, event_slug=event.slug, room_id=room.id, language_code="en"
    )
    with tc.websocket_connect(f"/ws/captions/{bid}?token={invite}") as ws:
        ws.close()
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect(f"/ws/captions/{event.slug}-{room.id}-ar?token={invite}") as ws:
            ws.receive_text()
    assert exc.value.code == 4003
