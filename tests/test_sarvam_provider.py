from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from portal.crypto import decrypt_val, encrypt_val
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    dispose,
    get_session,
    init_db,
)
from portal.models import DBBooth, Event, Room
from portal.transcription.constants import ALLOWED_MODELS, ProviderEnum
from portal.transcription.providers.base import ProviderConfig, get_api_key
from portal.transcription.providers.sarvam import (
    DEFAULT_MODEL,
    SARVAM_LANGUAGE_MAP,
    SARVAM_WS_URL,
    SarvamProvider,
    get_sarvam_language,
    is_sarvam_supported_language,
)
from portal.transcription.worker import PROVIDERS


class FakeProcess:
    """Mock ffmpeg subprocess providing controlled stdout chunks."""

    def __init__(self, chunks: list[bytes], delay: float = 0.0):
        self._chunks = list(chunks)
        self._delay = delay
        self.returncode = None
        self.stdout = self

    async def read(self, size: int) -> bytes:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._chunks:
            return self._chunks.pop(0)
        self.returncode = 0
        return b""


class MockWebSocket:
    """Mock websockets client connection recording sent messages and yielding test events."""

    def __init__(self, incoming_messages: list[str] | None = None, raise_on_connect: Exception | None = None):
        self.incoming_messages = list(incoming_messages or [])
        self.raise_on_connect = raise_on_connect
        self.sent_messages: list[str] = []
        self.closed = False
        self._close_event = asyncio.Event()

    async def __aenter__(self):
        if self.raise_on_connect:
            raise self.raise_on_connect
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.closed = True
        self._close_event.set()

    async def send(self, msg: str):
        self.sent_messages.append(msg)

    async def recv(self) -> str:
        if self.incoming_messages:
            return self.incoming_messages.pop(0)
        await self._close_event.wait()
        return ""

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.incoming_messages:
            return self.incoming_messages.pop(0)
        # Keep waiting until the connection is closed or task cancelled
        await self._close_event.wait()
        raise StopAsyncIteration


@pytest.fixture
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


# ── 1. Provider Registration and Model Configuration ──────────────────────────


def test_sarvam_provider_registration():
    assert ProviderEnum.SARVAM == "sarvam"
    assert "sarvam" in PROVIDERS
    assert isinstance(PROVIDERS["sarvam"], SarvamProvider)
    assert ProviderEnum.SARVAM in ALLOWED_MODELS
    assert "saaras:v3-realtime" in ALLOWED_MODELS[ProviderEnum.SARVAM]


# ── 2. Language Mapping and Auto-Detection ─────────────────────────────────────


def test_sarvam_language_mapping_supported():
    assert get_sarvam_language("hi") == "hi-IN"
    assert get_sarvam_language("ta") == "ta-IN"
    assert get_sarvam_language("te") == "te-IN"
    assert get_sarvam_language("kn") == "kn-IN"
    assert get_sarvam_language("ml") == "ml-IN"
    assert get_sarvam_language("bn") == "bn-IN"
    assert get_sarvam_language("mr") == "mr-IN"
    assert get_sarvam_language("gu") == "gu-IN"
    assert get_sarvam_language("pa") == "pa-IN"
    assert get_sarvam_language("or") == "or-IN"
    assert get_sarvam_language("od") == "or-IN"
    assert get_sarvam_language("en") == "en-IN"
    assert get_sarvam_language("as") == "as-IN"
    assert get_sarvam_language("ur") == "ur-IN"


def test_sarvam_language_mapping_floor_and_empty():
    assert get_sarvam_language("floor") == "auto"
    assert get_sarvam_language("") == "auto"
    assert get_sarvam_language(None) == "auto"
    assert is_sarvam_supported_language("floor") is True
    assert is_sarvam_supported_language("") is True
    assert is_sarvam_supported_language(None) is True


# ── 3. Unsupported Language Rejection ──────────────────────────────────────────


def test_sarvam_language_mapping_unsupported():
    for code in ("fr", "es", "de", "ja", "zh", "ru", "ar"):
        assert is_sarvam_supported_language(code) is False
        with pytest.raises(ValueError, match="not supported"):
            get_sarvam_language(code)


# ── 4 & 5. WebSocket URL, Query Parameters, and Headers ────────────────────────


@pytest.mark.anyio
async def test_sarvam_run_stream_url_parameters_and_headers():
    provider = SarvamProvider()
    fake_proc = FakeProcess([b"audio-data-1"])
    mock_ws = MockWebSocket()

    connect_calls = []

    def mock_connect(url, additional_headers=None):
        connect_calls.append((url, additional_headers))
        return mock_ws

    broadcast = AsyncMock()

    with patch("websockets.connect", side_effect=mock_connect):
        await provider.run_stream(
            process=fake_proc,
            language_code="hi",
            model_variant="saaras:v3-realtime",
            config=ProviderConfig(api_key="sarvam-secret-key-123"),
            broadcast_callback=broadcast,
            booth_id="pycon-1-hi",
            prompt="Machine Learning, PyCon India",
        )

    assert len(connect_calls) == 1
    url, headers = connect_calls[0]
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "wss"
    assert parsed.netloc == "api.sarvam.ai"
    assert parsed.path == "/speech-to-text-realtime/ws"

    query = urllib.parse.parse_qs(parsed.query)
    assert query["model"] == ["saaras:v3-realtime"]
    assert query["language_code"] == ["hi-IN"]
    assert query["stream_type"] == ["balanced"]
    assert query["mode"] == ["transcribe"]
    assert query["endpointing"] == ["vad"]
    assert query["prompt"] == ["Machine Learning, PyCon India"]

    assert headers == {"api-subscription-key": "sarvam-secret-key-123"}


# ── 6. Audio Frames Sent in Base64 audio_input Format ──────────────────────────


@pytest.mark.anyio
async def test_sarvam_audio_input_base64_format():
    provider = SarvamProvider()
    raw_audio = b"\x01\x02\x03\x04" * 100
    fake_proc = FakeProcess([raw_audio])
    mock_ws = MockWebSocket()

    with patch("websockets.connect", return_value=mock_ws):
        await provider.run_stream(
            process=fake_proc,
            language_code="hi",
            model_variant="saaras:v3-realtime",
            config=ProviderConfig(api_key="fake-key"),
            broadcast_callback=AsyncMock(),
            booth_id="booth-1",
        )

    assert len(mock_ws.sent_messages) >= 1
    msg = json.loads(mock_ws.sent_messages[0])
    assert msg["event"] == "audio_input"
    assert base64.b64decode(msg["audio"]) == raw_audio


# ── 7 & 8. Partial and Final Transcript Handling ───────────────────────────────


@pytest.mark.anyio
async def test_sarvam_partial_and_final_transcript_events():
    provider = SarvamProvider()
    fake_proc = FakeProcess([b"audio-1", b"audio-2"])

    incoming = [
        json.dumps({"event": "session.begin", "request_id": "req-001"}),
        json.dumps({"event": "transcript.partial", "transcript": "Namaste"}),
        json.dumps({"event": "transcript.partial", "transcript": "Namaste dosto"}),
        json.dumps({"event": "transcript.final", "transcript": "Namaste dosto, swagat hai."}),
    ]
    mock_ws = MockWebSocket(incoming_messages=incoming)
    broadcast_events = []

    async def mock_broadcast(booth_id, payload):
        broadcast_events.append(payload)

    with patch("websockets.connect", return_value=mock_ws):
        await provider.run_stream(
            process=fake_proc,
            language_code="hi",
            model_variant="saaras:v3-realtime",
            config=ProviderConfig(api_key="fake-key"),
            broadcast_callback=mock_broadcast,
            booth_id="booth-1",
        )

    # Verify that partials and final were received and broadcast
    partials = [e for e in broadcast_events if isinstance(e, dict) and e.get("status") == "partial"]
    finals = [e for e in broadcast_events if isinstance(e, dict) and e.get("status") == "final"]

    assert len(partials) >= 2
    assert partials[0]["text"] == "Namaste"
    assert partials[1]["text"] == "Namaste dosto"

    assert len(finals) == 1
    assert finals[0]["text"] == "Namaste dosto, swagat hai."


# ── 9. Empty Final Clears Interim Caption State ────────────────────────────────


@pytest.mark.anyio
async def test_sarvam_empty_final_clears_interim_state():
    provider = SarvamProvider()
    fake_proc = FakeProcess([b"audio-1"])

    incoming = [
        json.dumps({"event": "transcript.final", "transcript": ""}),
    ]
    mock_ws = MockWebSocket(incoming_messages=incoming)
    broadcast_events = []

    async def mock_broadcast(booth_id, payload):
        broadcast_events.append(payload)

    with patch("websockets.connect", return_value=mock_ws):
        await provider.run_stream(
            process=fake_proc,
            language_code="hi",
            model_variant="saaras:v3-realtime",
            config=ProviderConfig(api_key="fake-key"),
            broadcast_callback=mock_broadcast,
            booth_id="booth-1",
        )

    clears = [e for e in broadcast_events if isinstance(e, dict) and e.get("status") == "clear"]
    assert len(clears) == 1


# ── 10. Keepalive Ping When Audio is Idle ──────────────────────────────────────


@pytest.mark.anyio
async def test_sarvam_keepalive_ping_on_idle():
    from portal.transcription.providers import sarvam

    provider = SarvamProvider()

    class IdleProcess:
        def __init__(self):
            self.returncode = None
            self.stdout = self
            self.calls = 0

        async def read(self, size: int) -> bytes:
            self.calls += 1
            if self.calls == 1:
                # Sleep long enough to trigger the keepalive ping
                await asyncio.sleep(0.12)
                return b"some-audio"
            self.returncode = 0
            return b""

    proc = IdleProcess()
    mock_ws = MockWebSocket()

    with (
        patch("websockets.connect", return_value=mock_ws),
        patch.object(sarvam, "KEEPALIVE_TIMEOUT_SECONDS", 0.05),
    ):
        await provider.run_stream(
            process=proc,
            language_code="hi",
            model_variant="saaras:v3-realtime",
            config=ProviderConfig(api_key="fake-key"),
            broadcast_callback=AsyncMock(),
            booth_id="booth-1",
        )

    sent = [json.loads(m) for m in mock_ws.sent_messages]
    events = [m.get("event") for m in sent]
    assert "ping" in events


# ── 11. Authentication Error Handling (HTTP 401 / 403) ─────────────────────────


@pytest.mark.anyio
async def test_sarvam_auth_failure_stops_without_infinite_retry():
    provider = SarvamProvider()
    fake_proc = FakeProcess([b"audio"])

    # Simulate HTTP 401 Unauthorized from WebSocket handshake
    class MockAuthError(Exception):
        def __init__(self):
            self.status_code = 401

    attempts = 0

    def mock_connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise MockAuthError()

    with patch("websockets.connect", side_effect=mock_connect):
        await provider.run_stream(
            process=fake_proc,
            language_code="hi",
            model_variant="saaras:v3-realtime",
            config=ProviderConfig(api_key="bad-key"),
            broadcast_callback=AsyncMock(),
            booth_id="booth-1",
        )

    # Must exit immediately on 401 rather than retrying 5 times
    assert attempts == 1


# ── 12. Fatal Server Error Event Stops Worker ──────────────────────────────────


@pytest.mark.anyio
async def test_sarvam_fatal_error_event_stops_worker():
    provider = SarvamProvider()
    fake_proc = FakeProcess([b"audio-1", b"audio-2"])

    incoming = [
        json.dumps({"event": "error", "message": "Invalid model configuration", "is_fatal": True}),
    ]
    mock_ws = MockWebSocket(incoming_messages=incoming)

    with patch("websockets.connect", return_value=mock_ws):
        await provider.run_stream(
            process=fake_proc,
            language_code="hi",
            model_variant="saaras:v3-realtime",
            config=ProviderConfig(api_key="fake-key"),
            broadcast_callback=AsyncMock(),
            booth_id="booth-1",
        )

    assert mock_ws.closed is True


# ── 13 & 14. API Key Storage, Encryption, and Clearing ─────────────────────────


@pytest.mark.anyio
async def test_sarvam_api_key_encryption_persistence_and_clearing(setup_db):
    raw_key = "sarvam_live_secret_key_98765"

    async with get_session() as session:
        event = await create_event(session, slug="sarvam-test", display_name="Sarvam Test")
        event_id = event.id
        assert event.encrypted_sarvam_api_key is None
        assert get_api_key(event, ProviderEnum.SARVAM) is None

        # Encrypt and store key
        event.encrypted_sarvam_api_key = encrypt_val(raw_key)

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        stored_ciphertext = event.encrypted_sarvam_api_key
        assert stored_ciphertext is not None
        assert stored_ciphertext != raw_key
        assert decrypt_val(stored_ciphertext) == raw_key
        assert get_api_key(event, ProviderEnum.SARVAM) == raw_key

        # Clear key
        event.encrypted_sarvam_api_key = None

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.encrypted_sarvam_api_key is None
        assert get_api_key(event, ProviderEnum.SARVAM) is None


# ── 15. Missing API Key Behavior ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_sarvam_missing_api_key_exits_early():
    provider = SarvamProvider()
    fake_proc = FakeProcess([b"audio"])
    connect_called = False

    def mock_connect(*args, **kwargs):
        nonlocal connect_called
        connect_called = True
        return MockWebSocket()

    with patch("websockets.connect", side_effect=mock_connect):
        await provider.run_stream(
            process=fake_proc,
            language_code="hi",
            model_variant="saaras:v3-realtime",
            config=ProviderConfig(api_key=None),
            broadcast_callback=AsyncMock(),
            booth_id="booth-1",
        )

    assert connect_called is False


# ── 16. Credentials Never Logged ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_sarvam_credentials_never_appear_in_logs(caplog):
    provider = SarvamProvider()
    secret_key = "sensitive-sarvam-key-never-log"
    fake_proc = FakeProcess([b"audio-1"])
    mock_ws = MockWebSocket(
        incoming_messages=[json.dumps({"event": "error", "message": "bad request", "is_fatal": True})]
    )

    with caplog.at_level(logging.DEBUG):
        with patch("websockets.connect", return_value=mock_ws):
            await provider.run_stream(
                process=fake_proc,
                language_code="hi",
                model_variant="saaras:v3-realtime",
                config=ProviderConfig(api_key=secret_key),
                broadcast_callback=AsyncMock(),
                booth_id="booth-secret",
            )

    for record in caplog.records:
        assert secret_key not in record.message
        assert secret_key not in str(record.args)


# ── 17. Admin API Settings Form (Save and Clear) ──────────────────────────────


@pytest.mark.anyio
async def test_sarvam_admin_api_settings_post(setup_db):
    from starlette.requests import Request

    from portal.routers.admin import admin_event_api_settings_post

    async with get_session() as session:
        event = await create_event(session, slug="sarvam-admin-test", display_name="Sarvam Admin Test")
        event_id = event.id

    dummy_request = Request(
        {"type": "http", "method": "POST", "url": "http://test/admin/events/1/api-settings", "headers": []}
    )

    # Set key (explicitly pass clear_sarvam_api_key=False since FastAPI Form defaults are objects)
    await admin_event_api_settings_post(
        request=dummy_request,
        event_id=event_id,
        transcription_api_enabled=True,
        sarvam_api_key="sarvam-admin-key-abc",
        clear_sarvam_api_key=False,
    )

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.encrypted_sarvam_api_key is not None
        assert decrypt_val(event.encrypted_sarvam_api_key) == "sarvam-admin-key-abc"

    # Clear key
    await admin_event_api_settings_post(
        request=dummy_request,
        event_id=event_id,
        transcription_api_enabled=True,
        clear_sarvam_api_key=True,
    )

    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.encrypted_sarvam_api_key is None


# ── 18. Admin Booth and Room Language Validation ──────────────────────────────


@pytest.mark.anyio
async def test_sarvam_booth_and_room_language_validation(setup_db):
    from fastapi import HTTPException
    from starlette.requests import Request

    from portal.routers.admin import admin_edit_room, admin_transcription_settings

    async with get_session() as session:
        event = await create_event(
            session,
            slug="sarvam-room-test",
            display_name="Sarvam Room Test",
        )
        event.transcription_api_enabled = True
        event.encrypted_sarvam_api_key = encrypt_val("key-123")
        room = await create_room(session, event_id=event.id, display_name="Main Room")
        booth_hi = await create_booth(
            session, event_id=event.id, room_id=room.id, language_code="hi", language_name="Hindi"
        )
        booth_fr = await create_booth(
            session, event_id=event.id, room_id=room.id, language_code="fr", language_name="French"
        )
        event_id = event.id
        room_id = room.id
        booth_hi_id = booth_hi.id
        booth_fr_id = booth_fr.id

    dummy_request = Request({"type": "http", "method": "POST", "url": "http://test", "headers": []})

    # Supported booth language (Hindi) -> OK
    await admin_transcription_settings(
        request=dummy_request,
        event_id=event_id,
        room_id=room_id,
        booth_id=booth_hi_id,
        transcription_enabled=True,
        transcription_provider="sarvam",
        transcription_model="saaras:v3-realtime",
    )

    # Unsupported booth language (French) -> 400 Bad Request
    with pytest.raises(HTTPException) as exc_info:
        await admin_transcription_settings(
            request=dummy_request,
            event_id=event_id,
            room_id=room_id,
            booth_id=booth_fr_id,
            transcription_enabled=True,
            transcription_provider="sarvam",
            transcription_model="saaras:v3-realtime",
        )
    assert exc_info.value.status_code == 400
    assert "not supported by Sarvam AI" in exc_info.value.detail

    from starlette.datastructures import FormData

    # Room floor update with unsupported language (fr) -> 400 Bad Request
    room_fr_form_request = Request(
        {
            "type": "http",
            "method": "POST",
            "url": "http://test",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        }
    )
    room_fr_form_request._form = FormData(
        [
            ("form_section", "transcription"),
            ("floor_transcription_enabled", "on"),
            ("floor_transcription_provider", "sarvam"),
            ("floor_transcription_model", "saaras:v3-realtime"),
            ("floor_language_code", "fr"),
        ]
    )
    with pytest.raises(HTTPException) as exc_info:
        await admin_edit_room(request=room_fr_form_request, event_id=event_id, room_id=room_id)
    assert exc_info.value.status_code == 400
    assert "not supported by Sarvam AI" in exc_info.value.detail

    # Room floor update with supported language (hi) -> OK
    room_hi_form_request = Request(
        {
            "type": "http",
            "method": "POST",
            "url": "http://test",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        }
    )
    room_hi_form_request._form = FormData(
        [
            ("form_section", "transcription"),
            ("floor_transcription_enabled", "on"),
            ("floor_transcription_provider", "sarvam"),
            ("floor_transcription_model", "saaras:v3-realtime"),
            ("floor_language_code", "hi"),
        ]
    )
    await admin_edit_room(request=room_hi_form_request, event_id=event_id, room_id=room_id)

    async with get_session() as session:
        updated_room = await session.get(Room, room_id)
        assert updated_room.floor_transcription_provider == "sarvam"
        assert updated_room.floor_language_code == "hi"
