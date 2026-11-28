"""The event glossary reaches ElevenLabs realtime captions as keyterms (#1169)."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import websockets

from portal.database import get_session
from portal.live_demo import AudioPipe
from portal.models import AIVocabularyEntry, Event, Room
from portal.transcription.providers import elevenlabs as stt
from portal.transcription.providers.base import ProviderConfig
from portal.translations.vocabulary import speech_keyterms


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _keyterm_params_length(terms: list[str]) -> int:
    return len(stt.build_realtime_url("scribe_v2_realtime", "en", terms)) - len(
        stt.build_realtime_url("scribe_v2_realtime", "en")
    )


def _rejection(status: int):
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    return InvalidStatus(Response(status, "Bad Request", Headers(), b""))


# ── URL and limits ───────────────────────────────────────────────────────


@pytest.mark.parametrize("language", ["en", "", "zh-TW"])
def test_no_terms_gives_the_same_url_as_before(language):
    before = (
        "wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v2_realtime"
        f"&language_code={language}&audio_format=pcm_16000&commit_strategy=vad"
    )
    assert stt.build_realtime_url("scribe_v2_realtime", language) == before


def test_terms_are_repeated_and_url_encoded():
    url = stt.build_realtime_url("scribe_v2_realtime", "th", ["VoxBento", "Mission Control", "R&D", "ไทย"])
    query = _query(url)
    # One parameter per term, each encoded, so a phrase or an "&" stays one term.
    assert query["keyterms"] == ["VoxBento", "Mission Control", "R&D", "ไทย"]
    assert "R%26D" in url
    assert query["model_id"] == ["scribe_v2_realtime"] and query["language_code"] == ["th"]


def test_a_latin_term_costs_its_length():
    assert stt.keyterm_cost("VoxBento") == 8
    assert stt.keyterm_cost("Mission Control") == 15


def test_a_non_latin_term_costs_more_than_its_length():
    # Each Thai or Chinese character is 9 characters once percent-encoded.
    assert stt.keyterm_cost("語" * 100) > 100


def test_fit_keyterms_keeps_elevenlabs_count_limit():
    terms = stt.fit_keyterms([f"term{n:03}" for n in range(150)])
    assert len(terms) == stt.MAX_KEYTERMS
    assert terms[0] == "term000" and terms[-1] == "term099"


def test_fit_keyterms_keeps_the_character_limit_and_skips_only_what_does_not_fit():
    long_terms = [("x" * 299) + str(n) for n in range(4)]
    terms = stt.fit_keyterms([*long_terms, "VoxBento", "Eventyay"])
    assert sum(len(t) for t in terms) <= stt.MAX_KEYTERM_CHARS
    # The fourth long term does not fit; the short ones after it still do.
    assert terms == [*long_terms[:3], "VoxBento", "Eventyay"]


@pytest.mark.parametrize("character", ["語", "ไ", "\U0001f600"])
def test_fit_keyterms_keeps_a_non_latin_glossary_inside_the_url_budget(character):
    # 1,000 characters of these would pass ElevenLabs' character limit but make a request line
    # the server refuses with HTTP 400.
    terms = stt.fit_keyterms([character * 99 + str(n) for n in range(10)])
    assert terms
    assert sum(len(t) for t in terms) <= stt.MAX_KEYTERM_CHARS
    assert _keyterm_params_length(terms) <= stt.MAX_KEYTERM_URL_CHARS


def test_fit_keyterms_cleans_and_deduplicates():
    assert stt.fit_keyterms(["Vox\nBento", "VOX  bento", "", "  ", "Event\x00yay"]) == ["Vox Bento", "Event yay"]


# ── Stream ───────────────────────────────────────────────────────────────


class _Socket:
    """A realtime session that opens with ``first`` and never sends a transcript."""

    def __init__(self, first: dict):
        self.first = first
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return json.dumps(self.first)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Future()


STARTED = {"message_type": "session_started"}
REFUSED = {
    "message_type": "invalid_request",
    "error": "Number of keyterms cannot exceed 100. You provided 101 keyterms.",
}


def _pipe() -> AudioPipe:
    pipe = AudioPipe()
    pipe.write(b"\x01\x01" * 3000)
    pipe.close()
    return pipe


async def _run(monkeypatch, sockets, config: ProviderConfig | None = None, room_id: int | None = 7) -> list[str]:
    """Run one stream; ``sockets`` are what each connection attempt gets, in order."""
    urls: list[str] = []
    attempts = iter(sockets)

    def connect(url, **kwargs):
        urls.append(url)
        outcome = next(attempts)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(stt, "FINAL_TRANSCRIPT_TIMEOUT_SECONDS", 0.01)

    async def broadcast(*_):
        pass

    await asyncio.wait_for(
        stt.ElevenLabsProvider().run_stream(
            _pipe(), "en", "scribe_v2_realtime", config or ProviderConfig("key"), broadcast, "booth", room_id
        ),
        2,
    )
    return urls


def _room_terms(monkeypatch, terms: list[str]) -> list[int | None]:
    looked_up: list[int | None] = []

    async def load(room_id):
        looked_up.append(room_id)
        return terms

    monkeypatch.setattr(stt, "load_keyterms", load)
    return looked_up


@pytest.mark.anyio
async def test_the_room_glossary_is_sent_as_keyterms(monkeypatch):
    looked_up = _room_terms(monkeypatch, ["VoxBento", "Eventyay"])
    socket = _Socket(STARTED)
    urls = await _run(monkeypatch, [socket])
    assert looked_up == [7]
    assert _query(urls[0])["keyterms"] == ["VoxBento", "Eventyay"]
    # The session ran as before: audio, then the closing commit.
    assert socket.sent[-1]["commit"] is True


@pytest.mark.anyio
async def test_no_glossary_gives_the_same_url_as_before(monkeypatch):
    _room_terms(monkeypatch, [])
    urls = await _run(monkeypatch, [_Socket(STARTED)])
    assert urls == [stt.build_realtime_url("scribe_v2_realtime", "en")]


@pytest.mark.anyio
async def test_terms_given_in_the_config_are_fitted_and_used_without_a_room_lookup(monkeypatch):
    looked_up = _room_terms(monkeypatch, ["Must not be used"])
    config = ProviderConfig("key", keyterms=tuple(f"hint{n:03}" for n in range(120)))
    urls = await _run(monkeypatch, [_Socket(STARTED)], config, room_id=None)
    assert looked_up == []
    assert _query(urls[0])["keyterms"] == [f"hint{n:03}" for n in range(stt.MAX_KEYTERMS)]


@pytest.mark.anyio
async def test_refused_terms_reconnect_without_them(monkeypatch):
    _room_terms(monkeypatch, ["VoxBento"])
    socket = _Socket(STARTED)
    urls = await _run(monkeypatch, [_Socket(REFUSED), socket])
    assert len(urls) == 2
    assert "keyterms" in _query(urls[0])
    assert urls[1] == stt.build_realtime_url("scribe_v2_realtime", "en")
    # Captions still ran on the second connection.
    assert socket.sent and socket.sent[-1]["commit"] is True


@pytest.mark.anyio
async def test_a_refused_session_without_terms_still_ends_the_stream(monkeypatch):
    _room_terms(monkeypatch, [])
    urls = await _run(monkeypatch, [_Socket(REFUSED)])
    assert len(urls) == 1


@pytest.mark.anyio
async def test_an_auth_error_with_terms_still_ends_the_stream(monkeypatch):
    # Only invalid_request can be about the terms; a bad key is not retried without them.
    _room_terms(monkeypatch, ["VoxBento"])
    urls = await _run(monkeypatch, [_Socket({"message_type": "auth_error", "error": "Invalid API key"})])
    assert len(urls) == 1


@pytest.mark.anyio
async def test_a_handshake_refused_with_400_reconnects_without_terms(monkeypatch, caplog):
    _room_terms(monkeypatch, ["VoxBento"])
    urls = await _run(monkeypatch, [_rejection(400), _Socket(STARTED)])
    assert "keyterms" in _query(urls[0])
    assert urls[1] == stt.build_realtime_url("scribe_v2_realtime", "en")
    # Not counted as a failed connection, so it reconnects at once instead of after the 2 s delay.
    assert "connection failed" not in caplog.text
    assert "refused the glossary terms (HTTP 400)" in caplog.text


@pytest.mark.anyio
async def test_a_handshake_refused_with_400_without_terms_is_an_ordinary_failure(monkeypatch):
    _room_terms(monkeypatch, [])
    sleeps: list[float] = []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(stt.asyncio, "sleep", no_sleep)
    urls = await _run(monkeypatch, [_rejection(400), _Socket(STARTED)])
    assert urls[0] == urls[1]
    assert sleeps == [2]


# ── Glossary lookup ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_room_lookup_uses_elevenlabs_limits_and_the_glossary_switch():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    try:
        async with get_session() as session:
            event = Event(slug="el-scope", display_name="ElevenLabs")
            session.add(event)
            await session.flush()
            room = Room(event_id=event.id, display_name="Main Hall")
            session.add(room)
            await session.flush()

            def entry(term: str, priority: int = 0) -> AIVocabularyEntry:
                return AIVocabularyEntry(
                    event_id=event.id,
                    room_id=room.id,
                    source_term=term,
                    target_language="th",
                    target_term=term,
                    priority=priority,
                )

            session.add(entry("Must Keep", priority=95))
            session.add_all(entry(f"Product {n:03}") for n in range(150))
            room_id = room.id

        terms = await stt.load_keyterms(room_id)
        assert terms[0] == "Must Keep"
        # 1,000 characters hold "Must Keep" and 90 of the 11-character terms; Deepgram's budget takes 100.
        assert len(terms) == 91 and sum(len(t) for t in terms) <= stt.MAX_KEYTERM_CHARS
        async with get_session() as session:
            assert len(await speech_keyterms(session, room_id)) == 100

        async with get_session() as session:
            (await session.get(Room, room_id)).floor_ai_vocabulary_enabled = False
        assert await stt.load_keyterms(room_id) == []
    finally:
        await dispose()


class _DroppedSocket(_Socket):
    """A session the server ends without warning, as a network drop does."""

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.anyio
async def test_a_dropped_connection_reconnects_with_the_same_terms(monkeypatch):
    _room_terms(monkeypatch, ["VoxBento", "Eventyay"])
    real_sleep = asyncio.sleep

    async def no_delay(_delay):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    pipe = AudioPipe()
    pipe.write(b"\x01\x01" * 3000)
    urls: list[str] = []
    second = _Socket(STARTED)

    def connect(url, **kwargs):
        urls.append(url)
        if len(urls) == 1:
            return _DroppedSocket(STARTED)
        pipe.close()  # the audio ends during the second session
        return second

    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(stt, "FINAL_TRANSCRIPT_TIMEOUT_SECONDS", 0.01)

    async def broadcast(*_):
        pass

    await asyncio.wait_for(
        stt.ElevenLabsProvider().run_stream(pipe, "en", "scribe_v2_realtime", ProviderConfig("key"), broadcast, "b", 7),
        2,
    )
    assert len(urls) == 2 and urls[0] == urls[1]
    assert _query(urls[1])["keyterms"] == ["VoxBento", "Eventyay"]
    assert second.sent[-1]["commit"] is True
