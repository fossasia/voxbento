from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.models import FLOOR_SOURCE_JITSI_BOT, FLOOR_SOURCE_PROGRAM_INGEST
from portal.program_ingest import publish_auth
from portal.program_ingest.credentials import apply_issued_secret, issue_ingest_secret
from portal.program_ingest.publish_auth import (
    AuthFailureLimiter,
    DenyReason,
    MediaMTXAuthRequest,
    authorize_publish,
    parse_floor_path,
)

HOOK_PATH = "/internal/mediamtx/auth"
HOOK_KEY = "test-hook-key"
HOOK = f"{HOOK_PATH}?key={HOOK_KEY}"


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    publish_auth.failure_limiter.reset()
    with patch.object(publish_auth.settings, "mediamtx_auth_hook_secret", HOOK_KEY):
        yield
    publish_auth.failure_limiter.reset()
    await dispose()


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _make_room(slug: str, mode: str = FLOOR_SOURCE_PROGRAM_INGEST, expires_in_days: int | None = None):
    from portal.database import create_event, create_room, get_session

    issued = issue_ingest_secret(expires_in_days=expires_in_days)
    async with get_session() as s:
        ev = await create_event(s, slug=slug, display_name=slug)
        room = await create_room(s, event_id=ev.id, display_name="Main")
        room.floor_source_mode = mode
        apply_issued_secret(room, issued)
        await s.flush()
        return ev.slug, room.id, issued.secret


def _req(
    path: str,
    *,
    token: str = "",
    password: str = "",
    protocol: str = "webrtc",
    action: str = "publish",
    ip="203.0.113.7",
):
    return MediaMTXAuthRequest(
        action=action, path=path, protocol=protocol, token=token, password=password, ip=ip, query=""
    )


@pytest.mark.anyio
async def test_parse_floor_path():
    fp = parse_floor_path("pycon/14/floor")
    assert fp is not None and fp.event_slug == "pycon" and fp.room_id == 14
    assert parse_floor_path("/pycon/14/floor/") is not None
    for bad in ("pycon/14/en", "pycon/floor", "pycon/-1/floor", "Py Con/1/floor", "a/1/floor/x"):
        assert parse_floor_path(bad) is None


@pytest.mark.anyio
async def test_valid_credential_publishes_only_to_its_room():
    slug_a, room_a, secret_a = await _make_room("ev-a")
    slug_b, room_b, _secret_b = await _make_room("ev-b")

    ok = await authorize_publish(_req(f"{slug_a}/{room_a}/floor", token=secret_a))
    assert ok.allowed and ok.room_id == room_a

    cross_room = await authorize_publish(_req(f"{slug_b}/{room_b}/floor", token=secret_a))
    assert not cross_room.allowed
    assert cross_room.reason == DenyReason.INVALID_CREDENTIAL


@pytest.mark.anyio
async def test_password_field_is_accepted_as_credential():
    slug, room_id, secret = await _make_room("ev-pw")
    decision = await authorize_publish(_req(f"{slug}/{room_id}/floor", password=secret))
    assert decision.allowed


@pytest.mark.anyio
async def test_slug_mismatch_is_unknown_room():
    _slug, room_id, secret = await _make_room("ev-real")
    decision = await authorize_publish(_req(f"ev-other/{room_id}/floor", token=secret))
    assert not decision.allowed
    assert decision.reason == DenyReason.UNKNOWN_ROOM


@pytest.mark.anyio
async def test_missing_invalid_and_expired_credentials_rejected():
    slug, room_id, secret = await _make_room("ev-x")
    path = f"{slug}/{room_id}/floor"
    assert not (await authorize_publish(_req(path))).allowed
    assert not (await authorize_publish(_req(path, token=secret + "x"))).allowed

    from portal.database import get_room_by_id, get_session
    from portal.models import utc_now

    async with get_session() as s:
        room = await get_room_by_id(s, room_id)
        room.program_ingest_secret_expires_at = utc_now()
    assert not (await authorize_publish(_req(path, token=secret))).allowed


@pytest.mark.anyio
async def test_revoked_credential_rejected():
    from portal.database import get_room_by_id, get_session
    from portal.program_ingest.credentials import revoke_ingest_secret

    slug, room_id, secret = await _make_room("ev-rev")
    async with get_session() as s:
        revoke_ingest_secret(await get_room_by_id(s, room_id))
    assert not (await authorize_publish(_req(f"{slug}/{room_id}/floor", token=secret))).allowed


@pytest.mark.anyio
async def test_disallowed_protocol_rejected_even_with_valid_secret():
    slug, room_id, secret = await _make_room("ev-proto")
    decision = await authorize_publish(_req(f"{slug}/{room_id}/floor", password=secret, protocol="rtmp"))
    assert not decision.allowed
    assert decision.reason == DenyReason.PROTOCOL_NOT_ALLOWED


@pytest.mark.anyio
async def test_floor_bot_refused_when_program_ingest_owns_room():
    slug, room_id, _secret = await _make_room("ev-own")
    decision = await authorize_publish(_req(f"{slug}/{room_id}/floor", protocol="rtsp"))
    assert not decision.allowed
    assert decision.reason == DenyReason.FLOOR_BOT_CONFLICT


@pytest.mark.anyio
async def test_bot_mode_allows_only_floor_bot_rtsp():
    slug, room_id, secret = await _make_room("ev-bot", mode=FLOOR_SOURCE_JITSI_BOT)
    path = f"{slug}/{room_id}/floor"
    assert (await authorize_publish(_req(path, protocol="rtsp"))).allowed
    decision = await authorize_publish(_req(path, token=secret))
    assert not decision.allowed
    assert decision.reason == DenyReason.BOT_MODE_EXTERNAL_PUBLISH


@pytest.mark.anyio
async def test_non_floor_paths_and_reads_keep_existing_behaviour():
    assert (await authorize_publish(_req("ev/1/en"))).allowed
    assert (await authorize_publish(_req("ev/1/floor", action="read"))).allowed


@pytest.mark.anyio
async def test_capacity_check_rejects_new_feed():
    slug, room_id, secret = await _make_room("ev-cap")
    decision = await authorize_publish(
        _req(f"{slug}/{room_id}/floor", token=secret), can_accept_new_feed=lambda _rid: False
    )
    assert not decision.allowed
    assert decision.reason == DenyReason.CAPACITY


@pytest.mark.anyio
async def test_repeated_failures_rate_limit_the_ip():
    slug, room_id, secret = await _make_room("ev-rl")
    path = f"{slug}/{room_id}/floor"
    limit = publish_auth.failure_limiter.max_failures
    for _ in range(limit):
        assert not (await authorize_publish(_req(path, token="vbi_guess", ip="198.51.100.9"))).allowed
    blocked = await authorize_publish(_req(path, token=secret, ip="198.51.100.9"))
    assert not blocked.allowed
    assert blocked.reason == DenyReason.RATE_LIMITED
    assert (await authorize_publish(_req(path, token=secret, ip="198.51.100.10"))).allowed


@pytest.mark.anyio
async def test_failure_limiter_window_expires():
    limiter = AuthFailureLimiter(max_failures=2, window_seconds=10)
    limiter.record_failure("ip", now=0)
    limiter.record_failure("ip", now=1)
    assert limiter.is_blocked("ip", now=2)
    assert not limiter.is_blocked("ip", now=12)


@pytest.mark.anyio
async def test_hook_returns_uniform_401_and_200(client):
    slug, room_id, secret = await _make_room("ev-hook")
    body = {"action": "publish", "path": f"{slug}/{room_id}/floor", "protocol": "webrtc", "ip": "203.0.113.1"}

    r = await client.post(HOOK, json={**body, "token": secret})
    assert r.status_code == 200

    unknown = await client.post(HOOK, json={**body, "path": "nope/999/floor", "token": secret})
    wrong = await client.post(HOOK, json={**body, "token": "vbi_wrong"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


@pytest.mark.anyio
async def test_hook_requires_shared_key_when_configured(client):
    body = {"action": "publish", "path": "ev/1/en", "protocol": "webrtc"}
    assert (await client.post(HOOK_PATH, json=body)).status_code == 401
    assert (await client.post(f"{HOOK_PATH}?key=wrong", json=body)).status_code == 401
    assert (await client.post(HOOK, json=body)).status_code == 200


@pytest.mark.anyio
async def test_secret_never_logged(client, caplog):
    slug, room_id, secret = await _make_room("ev-log")
    body = {"action": "publish", "path": f"{slug}/{room_id}/floor", "protocol": "webrtc", "ip": "203.0.113.2"}
    with caplog.at_level(logging.DEBUG):
        await client.post(HOOK, json={**body, "token": secret, "query": f"pass={secret}"})
        await client.post(HOOK, json={**body, "token": secret[:-1]})
    assert secret not in caplog.text
    assert secret[:-1] not in caplog.text


@pytest.mark.anyio
async def test_access_log_redacts_hook_key():
    from fastapi_app import _UvicornTokenRedactor

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("172.18.0.3:5000", "POST", "/internal/mediamtx/auth?key=hook-secret-value", "1.1", 200),
        exc_info=None,
    )
    _UvicornTokenRedactor().filter(record)
    assert "hook-secret-value" not in record.getMessage()


@pytest.mark.anyio
@pytest.mark.parametrize(("debug", "expected"), [(False, 401), (True, 200)])
async def test_hook_without_secret_is_open_only_in_debug(client, debug, expected):
    body = {"action": "publish", "path": "ev/1/en", "protocol": "webrtc", "ip": "198.51.100.1"}
    with (
        patch.object(publish_auth.settings, "mediamtx_auth_hook_secret", ""),
        patch.object(publish_auth.settings, "debug", debug),
    ):
        assert (await client.post(HOOK_PATH, json=body)).status_code == expected


@pytest.mark.anyio
async def test_missing_hook_secret_refuses_production_startup():
    from portal.config import Settings

    strong = "a-strong-random-secret-0123456789abcdef"
    with pytest.raises(RuntimeError, match="MEDIAMTX_AUTH_HOOK_SECRET"):
        Settings(debug=False, secret_key=strong, mediamtx_auth_hook_secret="").validate_production_secrets()
    Settings(debug=False, secret_key=strong, mediamtx_auth_hook_secret="k" * 32).validate_production_secrets()
    Settings(debug=True, secret_key=strong, mediamtx_auth_hook_secret="").validate_production_secrets()
