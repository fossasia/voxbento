"""Where finished captions are handed to the YouTube caption track (#1087, plan 006, step 5)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from portal import youtube_captions
from portal.database import get_session
from portal.models import Event, Room, RoomTranslationLanguage, TranscriptSegment
from portal.transcription.aggregator import CaptionAggregator
from portal.transcription.timing import SegmentTiming, segment_timing
from portal.translations.worker import TranslationWorker

CAPTION_URL = "https://upload.youtube.com/closedcaption?cid=SECRETCID123"


@pytest.fixture(autouse=True)
def clean_state():
    stores = (
        youtube_captions.targets,
        youtube_captions.health,
        youtube_captions.clock_offset_ms,
        youtube_captions._last_seq,
    )
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


@pytest.fixture
def offered(monkeypatch):
    """Every youtube_captions.offer call, as (room_id, language, text, received_at_ms)."""
    calls: list[tuple] = []
    monkeypatch.setattr(youtube_captions, "offer", lambda *args: calls.append(args))
    return calls


@pytest.fixture
def no_database_save(monkeypatch):
    """publish_final saves the segment and translates in a background task; these tests only watch the hand-off."""
    monkeypatch.setattr("portal.database.save_transcript_segment", AsyncMock(return_value=None))


@pytest.mark.anyio
async def test_a_floor_final_is_offered_to_youtube(offered, no_database_save):
    aggregator = CaptionAggregator(AsyncMock(), room_id=7)
    await aggregator.publish_final("conf-7-floor", "hello world", "seg-1", 1)
    assert offered == [(7, "floor", "hello world", None)]


@pytest.mark.anyio
async def test_a_floor_final_carries_the_time_the_audio_was_received(offered, no_database_save):
    aggregator = CaptionAggregator(AsyncMock(), room_id=7)
    token = segment_timing.set(SegmentTiming(1_800_000_123_000, 0))
    try:
        await aggregator.publish_final("conf-7-floor", "hello", "seg-1", 1)
    finally:
        segment_timing.reset(token)
    assert offered == [(7, "floor", "hello", 1_800_000_123_000)]


@pytest.mark.anyio
async def test_an_interpreter_booth_final_is_not_offered(offered, no_database_save):
    aggregator = CaptionAggregator(AsyncMock(), room_id=7)
    await aggregator.publish_final("conf-7-fr", "bonjour", "seg-1", 1)
    assert offered == []


@pytest.mark.anyio
async def test_a_booth_without_a_room_is_not_offered(offered):
    aggregator = CaptionAggregator(AsyncMock(), room_id=None)
    await aggregator.publish_final("conf-7-floor", "hello", "seg-1", 1)
    assert offered == []


@pytest.mark.anyio
async def test_the_caption_still_broadcasts_when_youtube_posting_fails(no_database_save, monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    # offer() is the only YouTube call on this path and it catches its own errors; prove the real one is safe.
    monkeypatch.setattr(youtube_captions, "track_task", broken)
    # Never let this test reach the network: the sender task offer() starts does nothing here.
    monkeypatch.setattr(youtube_captions, "_sender", lambda room_id: asyncio.sleep(0))
    await youtube_captions.activate(7, "floor", CAPTION_URL)
    callback = AsyncMock()
    await CaptionAggregator(callback, room_id=7).publish_final("conf-7-floor", "hello", "seg-1", 1)
    assert callback.await_count == 1
    assert callback.await_args.args[1]["text"] == "hello"


@pytest.fixture
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def room_with_languages(setup_db):
    async with get_session() as s:
        event = Event(slug="yt-hooks", display_name="Hooks")
        s.add(event)
        await s.flush()
        room = Room(
            event_id=event.id,
            display_name="Main Hall",
            floor_language_code="en",
            floor_translation_enabled=True,
            floor_translation_provider="local",
            floor_translation_model="test",
        )
        s.add(room)
        await s.flush()
        for code, name in (("fr", "French"), ("es", "Spanish"), ("en", "English")):
            s.add(RoomTranslationLanguage(room_id=room.id, language_code=code, language_name=name, enabled=True))
        segment = TranscriptSegment(room_id=room.id, text="Hello world", language_code="en")
        s.add(segment)
        await s.flush()
        return {"room": room, "event": event, "segment": segment}


async def translate(data, booth_id_str: str) -> list[str]:
    """Run the translation worker for the segment; returns the language names that were actually translated."""
    worker = TranslationWorker(AsyncMock())
    translated: list[str] = []
    synth = data["synth"] = AsyncMock(return_value=b"audio")

    async def fake_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
        translated.append(lang_name)
        return f"[{lang_name}] {text}"

    with (
        patch.object(worker, "_call_llm", new=fake_llm),
        patch("portal.tts.worker.synthesize", new=synth),
        patch("portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock) as bundle,
    ):
        data["bundle"] = bundle
        await worker.handle_translation(
            room_id=data["room"].id,
            segment_id=data["segment"].id,
            text="Hello world",
            booth_id_str=booth_id_str,
            uuid_segment_id="uuid-1",
            seq=1,
        )
        await asyncio.sleep(0.05)
    return translated


@pytest.mark.anyio
async def test_the_language_posted_to_youtube_is_translated_without_listeners(room_with_languages, offered):
    data = room_with_languages
    await youtube_captions.activate(data["room"].id, "fr", CAPTION_URL)
    booth = f"{data['event'].slug}-{data['room'].id}-floor"
    assert await translate(data, booth) == ["French"]  # Spanish has no listeners and no YouTube track
    assert offered == [(data["room"].id, "fr", "[French] Hello world", None)]


@pytest.mark.anyio
async def test_without_a_youtube_track_unwatched_languages_are_still_skipped(room_with_languages, offered):
    data = room_with_languages
    booth = f"{data['event'].slug}-{data['room'].id}-floor"
    assert await translate(data, booth) == []
    assert offered == []


@pytest.mark.anyio
async def test_a_translation_for_an_interpreter_booth_is_not_offered(room_with_languages, offered):
    data = room_with_languages
    await youtube_captions.activate(data["room"].id, "fr", CAPTION_URL)
    booth = f"{data['event'].slug}-{data['room'].id}-fr"
    await translate(data, booth)
    assert offered == []


@pytest.mark.anyio
async def test_an_interpreter_booth_is_not_translated_just_for_the_youtube_track(room_with_languages, offered):
    data = room_with_languages
    await youtube_captions.activate(data["room"].id, "fr", CAPTION_URL)
    booth = f"{data['event'].slug}-{data['room'].id}-fr"
    assert await translate(data, booth) == []  # nothing is posted for a booth, so nothing is paid for


@pytest.mark.anyio
async def test_a_language_only_youtube_needs_gets_no_speech_synthesis(room_with_languages, offered):
    data = room_with_languages
    await youtube_captions.activate(data["room"].id, "fr", CAPTION_URL)
    booth = f"{data['event'].slug}-{data['room'].id}-floor"
    assert await translate(data, booth) == ["French"]
    data["synth"].assert_not_awaited()
    # The text bundle says no speech follows, so a listener who joins now does not wait for one.
    (text_bundle,) = [call for call in data["bundle"].await_args_list if call.args[1] == "fr"]
    assert text_bundle.kwargs["audio_pending"] is False


@pytest.mark.anyio
async def test_a_language_with_a_tts_listener_still_gets_speech(room_with_languages, offered):
    data = room_with_languages
    booth = f"{data['event'].slug}-{data['room'].id}-floor"
    with patch("portal.websockets.manager.TTSConnectionManager.has_listeners", return_value=True):
        assert sorted(await translate(data, booth)) == ["French", "Spanish"]
    assert data["synth"].await_count == 2
    # Each text bundle says speech follows.
    text_bundles = [
        call
        for call in data["bundle"].await_args_list
        if call.args[1] in ("fr", "es") and "audio_pending" in call.kwargs
    ]
    assert [call.kwargs["audio_pending"] for call in text_bundles] == [True, True]


@pytest.mark.anyio
async def test_the_floor_language_is_offered_to_a_youtube_track_set_to_that_code(room_with_languages, offered):
    data = room_with_languages
    await youtube_captions.activate(data["room"].id, "en", CAPTION_URL)  # the floor language itself is "en"
    booth = f"{data['event'].slug}-{data['room'].id}-floor"
    await translate(data, booth)
    assert offered == [(data["room"].id, "en", "Hello world", None)]
