from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from portal.database import get_session
from portal.models import AIVocabularyEntry, DBBooth, Event, Room
from portal.transcription.aggregator import CaptionAggregator
from portal.transcription.providers import deepgram as stt
from portal.translations.vocabulary import MAX_KEYTERM_TOKENS, speech_keyterms
from portal.tts.providers import deepgram as tts


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# ── Speech to text ───────────────────────────────────────────────────────


def test_nova3_url_carries_keyterms_and_language():
    url = stt.build_listen_url("nova-3", "th", ["VoxBento", "Mission Control"])
    query = _query(url)
    assert query["model"] == ["nova-3"]
    assert query["language"] == ["th"]
    # One parameter per term, each encoded, so a phrase stays one term.
    assert query["keyterm"] == ["VoxBento", "Mission Control"]
    assert query["endpointing"] == [str(stt.ENDPOINTING_MS)]
    assert query["utterance_end_ms"] == [str(stt.UTTERANCE_END_MS)]


def test_nova2_url_boosts_keywords_instead_of_keyterms():
    # Nova-2 rejects the keyterm parameter, so a glossary must not break its connection.
    query = _query(stt.build_listen_url("nova-2", "en", ["VoxBento", "Mission Control"]))
    assert "keyterm" not in query
    assert query["keywords"] == [f"VoxBento:{stt.KEYWORD_BOOST}", f"Mission Control:{stt.KEYWORD_BOOST}"]


@pytest.mark.parametrize("model", ["nova-3", "nova-2"])
def test_no_terms_adds_no_glossary_parameters(model):
    query = _query(stt.build_listen_url(model, "en"))
    assert "keyterm" not in query and "keywords" not in query


def test_terms_are_url_encoded():
    url = stt.build_listen_url("nova-3", "en", ["R&D", "C++ / Rust", "ไทย"])
    assert "R%26D" in url and "C%2B%2B" in url
    assert _query(url)["keyterm"] == ["R&D", "C++ / Rust", "ไทย"]


@pytest.mark.parametrize("language", ["", "floor"])
def test_floor_language_is_left_to_deepgram(language):
    assert "language" not in _query(stt.build_listen_url("nova-3", language))


def test_supports_keyterms_only_on_nova3():
    assert stt.supports_keyterms("nova-3")
    assert not stt.supports_keyterms("nova-2")


@pytest.fixture
def recorder():
    messages: list[dict] = []

    async def broadcast(booth_id, message):
        messages.append(message)

    return CaptionAggregator(broadcast), messages


def _result(text: str, *, is_final: bool = False, speech_final: bool = False) -> dict:
    return {
        "type": "Results",
        "is_final": is_final,
        "speech_final": speech_final,
        "channel": {"alternatives": [{"transcript": text}]},
    }


def _finals(messages: list[dict]) -> list[str]:
    return [m["text"] for m in messages if m.get("status") == "final"]


@pytest.mark.anyio
async def test_mid_sentence_finals_wait_for_the_end_of_the_phrase(recorder):
    aggregator, messages = recorder
    booth = "conf-1-floor"
    await stt.handle_message(aggregator, booth, _result("Welcome to"))
    await stt.handle_message(aggregator, booth, _result("Welcome to the summit", is_final=True))
    await stt.handle_message(aggregator, booth, _result("where we"))
    # Interim words are shown after the settled ones, but nothing reaches translation yet.
    assert messages[-1] == {
        "type": "caption",
        "status": "partial",
        "text": "Welcome to the summit where we",
        "segment_id": messages[-1]["segment_id"],
    }
    assert _finals(messages) == []

    await stt.handle_message(aggregator, booth, _result("where we launch", is_final=True, speech_final=True))
    assert _finals(messages) == ["Welcome to the summit where we launch"]


@pytest.mark.anyio
async def test_utterance_end_closes_a_phrase_endpointing_missed(recorder):
    aggregator, messages = recorder
    booth = "conf-1-floor"
    await stt.handle_message(aggregator, booth, _result("Thank you all", is_final=True))
    await stt.handle_message(aggregator, booth, {"type": "UtteranceEnd", "last_word_end": 3.1})
    assert _finals(messages) == ["Thank you all"]


@pytest.mark.anyio
async def test_complete_sentences_are_published_without_waiting(recorder):
    aggregator, messages = recorder
    booth = "conf-1-floor"
    await stt.handle_message(aggregator, booth, _result("Our keynote begins at ten. Please take", is_final=True))
    assert _finals(messages) == ["Our keynote begins at ten."]
    await stt.handle_message(aggregator, booth, _result("your seats.", is_final=True, speech_final=True))
    assert _finals(messages) == ["Our keynote begins at ten.", "Please take your seats."]


@pytest.mark.anyio
async def test_messages_without_a_transcript_are_ignored(recorder):
    aggregator, messages = recorder
    await stt.handle_message(aggregator, "b", {"type": "Metadata", "request_id": "x"})
    await stt.handle_message(aggregator, "b", {"type": "Results", "channel": {"alternatives": []}})
    assert messages == []


@pytest.mark.anyio
async def test_a_failed_or_slow_glossary_lookup_gives_no_terms(monkeypatch):
    from sqlalchemy.exc import OperationalError

    async def broken(room_id):
        raise OperationalError("select", {}, Exception("database is locked"))

    monkeypatch.setattr(stt, "_read_keyterms", broken)
    assert await stt.load_keyterms(1) == []

    async def slow(room_id):
        await asyncio.sleep(1)
        return ["late"]

    monkeypatch.setattr(stt, "_read_keyterms", slow)
    monkeypatch.setattr(stt, "GLOSSARY_LOOKUP_TIMEOUT", 0.05)
    assert await stt.load_keyterms(1) == []
    assert await stt.load_keyterms(None) == []


@pytest.mark.anyio
async def test_speech_keyterms_cover_the_room_and_respect_the_budget():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    try:
        async with get_session() as session:
            event = Event(slug="kt-scope", display_name="Keyterms")
            session.add(event)
            await session.flush()
            room = Room(event_id=event.id, display_name="Main Hall")
            other = Room(event_id=event.id, display_name="Side Hall")
            session.add_all([room, other])
            await session.flush()
            booth = DBBooth(event_id=event.id, room_id=room.id, language_code="th", language_name="Thai")
            session.add(booth)
            await session.flush()

            def entry(term: str, **scope) -> AIVocabularyEntry:
                return AIVocabularyEntry(
                    event_id=event.id, source_term=term, target_language="th", target_term=term, **scope
                )

            session.add_all(
                [
                    entry("Event Wide"),
                    entry("Room Term", room_id=room.id, priority=50),
                    entry("Booth Term", room_id=room.id, booth_id=booth.id),
                    entry("Other Room", room_id=other.id),
                    # The same source term for another language is one keyterm, not two.
                    AIVocabularyEntry(
                        event_id=event.id, source_term="event  wide", target_language="ja", target_term="x"
                    ),
                ]
            )
            await session.flush()

            terms = await speech_keyterms(session, room.id)
            assert terms[0] == "Room Term"  # Highest priority first.
            assert sorted(terms) == ["Booth Term", "Event Wide", "Room Term"]
            assert await speech_keyterms(session, 999_999) == []

            # Newlines and control characters would break the request; the term survives as one line.
            session.add(entry("Multi\nLine\x00 Term", room_id=room.id, priority=99))
            await session.flush()
            assert (await speech_keyterms(session, room.id))[0] == "Multi Line Term"

            # The room's glossary switch turns recognition hints off too.
            room.floor_ai_vocabulary_enabled = False
            await session.flush()
            assert await speech_keyterms(session, room.id) == []
            room.floor_ai_vocabulary_enabled = True
            await session.flush()

            session.add_all(entry(f"Long product name number {n:03}") for n in range(200))
            session.add(entry("Must Keep", priority=95))
            await session.flush()
            terms = await speech_keyterms(session, room.id)
            assert sum(-(-len(t) // 3) for t in terms) <= MAX_KEYTERM_TOKENS
            assert len(terms) <= 100
            # Trimming drops the least important terms, never the high-priority ones.
            assert terms[:2] == ["Multi Line Term", "Must Keep"]
            assert "Long product name number 199" not in terms
    finally:
        await dispose()


# ── Text to speech ───────────────────────────────────────────────────────


class _FakeSpeakSocket:
    """Deepgram /v1/speak as it behaves: audio, then "Flushed", and the socket stays open."""

    def __init__(self, replies: list):
        self.sent: list[dict] = []
        self._replies: asyncio.Queue = asyncio.Queue()
        self._pending = replies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw: str):
        message = json.loads(raw)
        self.sent.append(message)
        if message["type"] == "Flush":
            for reply in self._pending:
                await self._replies.put(reply)

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Never closes on its own, like the real server after a flush.
        return await self._replies.get()


async def _one(text: str):
    yield text


@pytest.mark.anyio
async def test_tts_returns_as_soon_as_deepgram_confirms_the_flush(monkeypatch):
    socket = _FakeSpeakSocket([b"\x01\x02", b"\x03\x04", json.dumps({"type": "Flushed", "sequence_id": 0})])
    monkeypatch.setattr(tts.websockets, "connect", lambda *a, **k: socket)
    audio: list[bytes] = []

    async def on_audio(chunk: bytes):
        audio.append(chunk)

    started = time.monotonic()
    await tts.DeepgramTTSProvider("key").synthesize_stream(
        text_chunks=_one("Hello there. How are you"), language_code="en", voice="", on_audio=on_audio
    )

    assert time.monotonic() - started < 1.0, "must not wait for the flush timeout"
    assert audio == [b"\x01\x02", b"\x03\x04"]
    assert [m["type"] for m in socket.sent] == ["Speak", "Speak", "Flush", "Close"]


@pytest.mark.anyio
async def test_tts_stops_on_a_deepgram_error(monkeypatch):
    socket = _FakeSpeakSocket([json.dumps({"type": "Error", "description": "bad model"})])
    monkeypatch.setattr(tts.websockets, "connect", lambda *a, **k: socket)
    monkeypatch.setattr(tts, "FLUSH_TIMEOUT_SECONDS", 0.5)
    audio: list[bytes] = []

    async def on_audio(chunk: bytes):
        audio.append(chunk)

    started = time.monotonic()
    await tts.DeepgramTTSProvider("key").synthesize_stream(
        text_chunks=_one("Hi."), language_code="en", voice="", on_audio=on_audio
    )
    assert time.monotonic() - started < 0.4
    assert audio == []
