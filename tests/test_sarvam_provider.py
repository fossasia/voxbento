from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import websockets

from portal.transcription.constants import ALLOWED_MODELS, ProviderEnum, sarvam_language_code, speech_language_supported
from portal.transcription.providers import sarvam
from portal.transcription.providers.base import ProviderConfig
from portal.transcription.providers.sarvam import SarvamProvider, _CaptionFeed, _LinkHealth

API_KEY = "sarvam-test-secret"
MODEL = "saaras:v3-realtime"


def _audio_process(audio: bytes = b"", *, eof: bool = True) -> SimpleNamespace:
    """A stand-in ffmpeg process whose stdout holds this audio."""
    reader = asyncio.StreamReader()
    reader.feed_data(audio)
    if eof:
        reader.feed_eof()
    return SimpleNamespace(returncode=None, stdout=reader)


class FakeSarvam:
    """A local WebSocket server that plays one scripted handler per connection."""

    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.connections = []

    async def __call__(self, ws):
        """Play the next scripted session on this connection."""
        session = SimpleNamespace(ws=ws, request=ws.request, received=[])
        self.connections.append(session)
        await self.sessions.pop(0)(session)

    async def __aenter__(self):
        self.server = await websockets.serve(self, "127.0.0.1", 0)
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/stt"

    async def __aexit__(self, *_exc):
        self.server.close()
        await self.server.wait_closed()


async def _until_connected(fake, count):
    """Wait until the mock server has accepted this many connections."""
    while len(fake.connections) < count:
        await asyncio.sleep(0.01)


async def _read_until_end(session):
    """Record what the provider sends until it ends the session."""
    async for raw in session.ws:
        message = json.loads(raw)
        session.received.append(message)
        if message["event"] == "end":
            return


async def _run(url, monkeypatch, audio, language="hi", timeout=5.0):
    """Run the provider against the mock server and return what it broadcast."""
    monkeypatch.setattr(sarvam, "SARVAM_WS_URL", url)
    monkeypatch.setattr(sarvam, "BACKOFF_BASE_SECS", 0.01)
    captions: list[dict] = []

    async def broadcast(_booth_id, payload):
        captions.append(payload)

    process = _audio_process(audio)
    await asyncio.wait_for(
        SarvamProvider().run_stream(process, language, MODEL, ProviderConfig(API_KEY), broadcast, "conf-1-hi"), timeout
    )
    return captions


def _texts(captions, status):
    """The caption texts with this status."""
    return [c["text"] for c in captions if c["status"] == status]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("hi", "hi-IN"), ("or", "or-IN"), ("ta", "ta-IN"), ("floor", "auto"), ("", "auto"), (None, "auto"), ("de", None)],
)
def test_language_mapping(stored, expected):
    assert sarvam_language_code(stored) == expected


def test_unsupported_language_only_limits_sarvam():
    assert not speech_language_supported("sarvam", "de")
    assert speech_language_supported("sarvam", "hi")
    assert speech_language_supported("deepgram", "de")


def test_sarvam_is_registered():
    from portal.transcription.worker import PROVIDERS

    assert MODEL in ALLOWED_MODELS[ProviderEnum.SARVAM]
    assert isinstance(PROVIDERS["sarvam"], SarvamProvider)


@pytest.mark.anyio
async def test_streams_audio_and_maps_partial_and_final(monkeypatch):
    """Audio goes out in whole frames and transcripts come back as captions."""
    frame = bytes(range(256)) * 13  # 3328 bytes: one whole 3200-byte frame and a 128-byte tail.

    async def session(s):
        await s.ws.send(json.dumps({"event": "session.begin", "request_id": "r"}))
        await s.ws.send(json.dumps({"event": "transcript.partial", "utterance_idx": 0, "text": "नमस्ते"}))
        await _read_until_end(s)
        await s.ws.send(json.dumps({"event": "transcript.final", "utterance_idx": 0, "text": "नमस्ते दुनिया"}))
        await s.ws.close()

    fake = FakeSarvam(session)
    async with fake as url:
        captions = await _run(url, monkeypatch, frame)

    assert _texts(captions, "partial") == ["नमस्ते"]
    assert _texts(captions, "final") == ["नमस्ते दुनिया"]

    connection = fake.connections[0]
    query = parse_qs(urlparse(connection.request.path).query)
    assert query == {
        "model": [MODEL],
        "language_code": ["hi-IN"],
        "encoding": ["linear16"],
        "sample_rate": ["16000"],
    }
    assert connection.request.headers["API-Subscription-Key"] == API_KEY
    audio = [m for m in connection.received if m["event"] == "audio_input"]
    assert [len(base64.b64decode(m["audio"])) for m in audio] == [3200, 128]
    assert b"".join(base64.b64decode(m["audio"]) for m in audio) == frame
    assert connection.received[-1] == {"event": "end"}


@pytest.mark.anyio
async def test_floor_language_uses_auto_detect(monkeypatch):
    """An empty language asks Sarvam to detect it."""
    async def session(s):
        await _read_until_end(s)

    fake = FakeSarvam(session)
    async with fake as url:
        await _run(url, monkeypatch, b"\0" * 3200, language="")

    assert parse_qs(urlparse(fake.connections[0].request.path).query)["language_code"] == ["auto"]


@pytest.mark.anyio
async def test_unsupported_language_never_connects(monkeypatch):
    fake = FakeSarvam()
    async with fake as url:
        assert await _run(url, monkeypatch, b"", language="de") == []
    assert fake.connections == []


@pytest.mark.anyio
async def test_reconnects_after_a_drop_and_publishes_the_unfinished_caption(monkeypatch):
    """A dropped session reconnects without losing the caption in progress."""
    held = asyncio.Event()

    async def dropped(s):
        await s.ws.send(json.dumps({"event": "transcript.partial", "utterance_idx": 0, "text": "आधा वाक्य"}))
        await s.ws.close(code=1011)

    async def recovered(s):
        await s.ws.send(json.dumps({"event": "transcript.final", "utterance_idx": 0, "text": "पूरा वाक्य"}))
        held.set()
        await _read_until_end(s)

    monkeypatch.setattr(sarvam, "SARVAM_WS_URL", "")
    monkeypatch.setattr(sarvam, "BACKOFF_BASE_SECS", 0.01)
    captions: list[dict] = []

    async def broadcast(_booth_id, payload):
        captions.append(payload)

    process = _audio_process(eof=False)
    fake = FakeSarvam(dropped, recovered)
    async with fake as url:
        monkeypatch.setattr(sarvam, "SARVAM_WS_URL", url)
        stream = asyncio.create_task(
            SarvamProvider().run_stream(process, "hi", MODEL, ProviderConfig(API_KEY), broadcast, "conf-1-hi")
        )
        await asyncio.wait_for(held.wait(), 5)
        await asyncio.sleep(0.05)
        process.stdout.feed_eof()
        await asyncio.wait_for(stream, 5)

    assert len(fake.connections) == 2
    assert _texts(captions, "final") == ["आधा वाक्य", "पूरा वाक्य"]


@pytest.mark.anyio
async def test_fatal_error_reconnects_and_never_logs_the_key_or_provider_text(monkeypatch, caplog):
    """A fatal error logs its code and status, never the key."""
    async def rejected(s):
        await s.ws.send(
            json.dumps(
                {"event": "error", "is_fatal": True, "code": "auth", "status_code": 403, "message": f"bad {API_KEY}"}
            )
        )
        await asyncio.sleep(1)

    async def recovered(s):
        await _read_until_end(s)

    monkeypatch.setattr(sarvam, "SARVAM_WS_URL", "")
    monkeypatch.setattr(sarvam, "BACKOFF_BASE_SECS", 0.01)

    async def broadcast(_booth_id, _payload):
        return None

    process = _audio_process(eof=False)
    fake = FakeSarvam(rejected, recovered)
    with caplog.at_level(logging.WARNING, logger=sarvam.__name__):
        async with fake as url:
            monkeypatch.setattr(sarvam, "SARVAM_WS_URL", url)
            stream = asyncio.create_task(
                SarvamProvider().run_stream(process, "hi", MODEL, ProviderConfig(API_KEY), broadcast, "conf-1-hi")
            )
            try:
                await asyncio.wait_for(_until_connected(fake, 2), 5)
            except TimeoutError:
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)
                raise
            process.stdout.feed_eof()
            await asyncio.wait_for(stream, 5)

    assert "code=auth status=403" in caplog.text
    assert API_KEY not in caplog.text


def test_backoff_doubles_to_a_cap_and_logs_once_per_outage(caplog):
    """Delays stop growing at the cap, and one outage logs one line."""
    link = _LinkHealth("conf-1-hi")
    with caplog.at_level(logging.WARNING, logger=sarvam.__name__):
        delays = [link.failed("boom") for _ in range(7)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert caplog.text.count("Sarvam session lost") == 1
    assert caplog.text.count("sarvam_stream_failed") == 1

    link.stable()
    assert link.failed("boom") == 1.0


@pytest.mark.anyio
async def test_text_published_by_forced_finalization_is_not_repeated():
    """Later transcripts of a force-finalized utterance publish only the new words."""
    calls: list[tuple[str, str]] = []

    class Aggregator:
        states = {"b": SimpleNamespace(current_utterance="")}

        async def handle_partial(self, _booth, text):
            calls.append(("partial", text))
            self.states["b"].current_utterance = "" if text == "long start" else text

        async def handle_final(self, _booth, text):
            calls.append(("final", text))

        async def handle_clear(self, _booth):
            calls.append(("clear", ""))

    feed = _CaptionFeed(Aggregator(), "b")
    await feed.handle({"event": "transcript.partial", "text": "long start"})
    await feed.handle({"event": "transcript.partial", "text": "long start and more"})
    await feed.handle({"event": "transcript.final", "text": "long start and more words"})
    await feed.handle({"event": "transcript.final", "text": "next utterance"})

    assert calls == [
        ("partial", "long start"),
        ("partial", "and more"),
        ("final", "and more words"),
        ("final", "next utterance"),
    ]


def test_room_setup_rejects_a_language_sarvam_cannot_transcribe():
    """Guided setup names the unsupported language."""
    from starlette.datastructures import FormData

    from portal.room_setup import Engine, EngineCatalog, parse_plan

    catalog = EngineCatalog(speech=(Engine("sarvam", MODEL, "Sarvam", "note"),), translation=(), voice=(), locked=())
    form = {"display_name": "Hall", "spoken_language": "de", "captions": "on", "speech_engine": "sarvam"}

    _plan, errors = parse_plan(FormData(form), catalog)
    assert any("Sarvam AI can't make captions in German" in error for error in errors)

    _plan, errors = parse_plan(FormData({**form, "spoken_language": "hi"}), catalog)
    assert errors == ()


def test_engine_offered_only_once_the_event_has_a_key():
    """Sarvam stays locked until the event has its key."""
    from portal.crypto import encrypt_val
    from portal.models import Event
    from portal.room_setup import engine_catalog

    event = Event(slug="e", display_name="E", transcription_api_enabled=True)
    assert engine_catalog(event).find("speech", "sarvam") is None
    event.encrypted_sarvam_api_key = encrypt_val("k")
    assert engine_catalog(event).find("speech", "sarvam").model == MODEL


def test_api_room_update_rejects_an_unsupported_language():
    from fastapi import HTTPException

    from portal.models import Room
    from portal.routers.api_v1 import _apply_floor_settings

    room = Room(floor_transcription_provider="deepgram", floor_language_code="de")
    with pytest.raises(HTTPException) as rejected:
        _apply_floor_settings(room, {"transcription_provider": ProviderEnum.SARVAM})
    assert rejected.value.status_code == 422

    _apply_floor_settings(room, {"transcription_provider": ProviderEnum.SARVAM, "source_language": "hi"})
    assert room.floor_language_code == "hi"
    assert room.floor_transcription_model == MODEL


def test_api_room_update_ignores_an_old_service_when_captions_are_off():
    from portal.models import Room
    from portal.routers.api_v1 import _apply_floor_settings

    room = Room(floor_transcription_enabled=False, floor_transcription_provider="sarvam", floor_language_code="de")
    _apply_floor_settings(room, {"name": "Renamed"})
    assert room.display_name == "Renamed"


def test_pipeline_reports_an_unsupported_language():
    from portal.crypto import encrypt_val
    from portal.models import Event, Room
    from portal.program_ingest import pipeline_problem

    event = Event(transcription_api_enabled=True, encrypted_sarvam_api_key=encrypt_val("k"))
    room = Room(floor_transcription_enabled=True, floor_transcription_provider="sarvam", floor_language_code="de")
    assert "can't make captions in German" in pipeline_problem(room, event)
    room.floor_language_code = "hi"
    assert pipeline_problem(room, event) is None
    room.floor_language_code = None
    assert pipeline_problem(room, event) is None


def test_backoff_attempts_stop_growing_at_the_cap():
    """The exponent can't overflow however long an outage lasts."""
    link = _LinkHealth("conf-1-hi")
    for _ in range(2000):
        assert link.failed("boom") <= sarvam.BACKOFF_MAX_SECS
    assert link.attempts < 10


def _recording_feed():
    """A feed whose aggregator records what it is asked to publish."""
    calls: list[tuple[str, str]] = []

    class Aggregator:
        states = {"b": SimpleNamespace(current_utterance="pending")}

        async def handle_partial(self, _booth, text):
            calls.append(("partial", text))

        async def handle_final(self, _booth, text):
            calls.append(("final", text))

        async def handle_clear(self, _booth):
            calls.append(("clear", ""))

    return _CaptionFeed(Aggregator(), "b"), calls


@pytest.mark.anyio
async def test_malformed_messages_are_skipped_without_ending_the_session(caplog):
    """Non-objects, bad JSON and non-string text publish nothing, and later messages still arrive."""
    feed, calls = _recording_feed()

    async def frames():
        yield "[1, 2]"
        yield "not json"
        yield json.dumps({"event": "transcript.final", "text": {"oops": 1}})
        yield json.dumps({"event": "transcript.final", "text": "ठीक है"})

    with caplog.at_level(logging.WARNING, logger=sarvam.__name__):
        await sarvam._receive(frames(), feed)

    assert calls == [("final", "ठीक है")]
    assert caplog.text.count("malformed") == 2


@pytest.mark.anyio
async def test_stopping_the_worker_publishes_the_unfinished_caption(monkeypatch):
    """Cancelling the worker still publishes what was said."""
    spoke = asyncio.Event()

    async def session(s):
        await s.ws.send(json.dumps({"event": "transcript.partial", "utterance_idx": 0, "text": "अधूरा वाक्य"}))
        spoke.set()
        await asyncio.sleep(30)

    captions: list[dict] = []

    async def broadcast(_booth_id, payload):
        captions.append(payload)

    fake = FakeSarvam(session)
    async with fake as url:
        monkeypatch.setattr(sarvam, "SARVAM_WS_URL", url)
        stream = asyncio.create_task(
            SarvamProvider().run_stream(
                _audio_process(eof=False), "hi", MODEL, ProviderConfig(API_KEY), broadcast, "conf-1-hi"
            )
        )
        await asyncio.wait_for(spoke.wait(), 5)
        await asyncio.sleep(0.05)
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)

    assert _texts(captions, "final") == ["अधूरा वाक्य"]


@pytest.mark.anyio
async def test_a_late_final_transcript_is_reported_and_does_not_hold_the_worker(monkeypatch, caplog):
    """The wait after the audio ends is bounded, and running out of time is logged."""

    async def session(s):
        await _read_until_end(s)
        await asyncio.sleep(30)

    monkeypatch.setattr(sarvam, "FINAL_WAIT_SECS", 0.05)
    fake = FakeSarvam(session)
    with caplog.at_level(logging.WARNING, logger=sarvam.__name__):
        async with fake as url:
            await _run(url, monkeypatch, b"\0" * 3200)

    assert "did not arrive in time" in caplog.text


@pytest.mark.anyio
async def test_failure_after_the_audio_ended_is_logged(monkeypatch, caplog):
    """A connection failure during the final wait is reported."""
    async def session(s):
        await _read_until_end(s)
        await s.ws.close(code=1011)

    fake = FakeSarvam(session)
    with caplog.at_level(logging.WARNING, logger=sarvam.__name__):
        async with fake as url:
            await _run(url, monkeypatch, b"\0" * 3200)

    assert "Sarvam session lost: connection closed code=1011" in caplog.text
    assert len(fake.connections) == 1


def test_room_ai_settings_reject_an_unsupported_language():
    from fastapi import HTTPException
    from starlette.datastructures import FormData

    from portal.models import Room
    from portal.room_setup import Engine, EngineCatalog
    from portal.routers.admin import _apply_ai_settings

    catalog = EngineCatalog(speech=(Engine("sarvam", MODEL, "Sarvam", "note"),), translation=(), voice=(), locked=())
    room = Room(floor_transcription_provider="deepgram", floor_language_code="de")
    form = FormData({"floor_transcription_provider": "sarvam"})

    with pytest.raises(HTTPException) as rejected:
        _apply_ai_settings(room, form, catalog)
    assert rejected.value.status_code == 400
    assert "can't make captions in German" in rejected.value.detail
    assert room.floor_transcription_provider == "deepgram"

    room.floor_language_code = "hi"
    _apply_ai_settings(room, form, catalog)
    assert room.floor_transcription_provider == "sarvam"
    assert room.floor_transcription_model == MODEL
