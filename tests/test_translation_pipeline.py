from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from portal.database import get_session
from portal.models import (
    BoothTranslationLanguage,
    DBBooth,
    Event,
    Room,
    RoomTranslationLanguage,
    TranscriptSegment,
)
from portal.translations.worker import TranslationWorker


@pytest.fixture
def mock_broadcast():
    return AsyncMock()


@pytest.fixture
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def db_data(setup_db):
    async with get_session() as s:
        event = Event(slug="pipeline-test", display_name="Test Event")
        s.add(event)
        await s.flush()

        room = Room(
            event_id=event.id,
            display_name="Main Hall",
            floor_language_code="en",
            floor_translation_enabled=True,
            floor_translation_provider="local",
            floor_translation_model="test",
            floor_tts_enabled=True,
        )
        s.add(room)
        await s.flush()

        # Add fr and es languages
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="fr", language_name="French", tts_enabled=True))
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="es", language_name="Spanish", tts_enabled=True))
        # source language (en) as a target to test bypass
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="en", language_name="English", tts_enabled=True))

        segment = TranscriptSegment(room_id=room.id, text="Hello world", language_code="en")
        s.add(segment)
        await s.flush()

        return {"room": room, "event": event, "segment": segment}


def _ai_booth(db_data, lang: str) -> str:
    return f"pipeline-test-{db_data['room'].id}-ai-{lang}"


async def _run(worker, db_data, *, segment_id=None, tts_listeners=True, text_listeners=True, synthesize=None):
    """Run the pipeline for one floor segment and return (bundle mock, caption broadcast mock)."""

    async def default_synthesize(room_id, text, lang_code):
        return b"fake_audio"

    with ExitStack() as stack:
        stack.enter_context(patch("portal.tts.worker.synthesize", new=synthesize or default_synthesize))
        stack.enter_context(patch("portal.websockets.manager.tts_manager.has_listeners", return_value=tts_listeners))
        stack.enter_context(
            patch("portal.websockets.manager.listener_manager.has_listeners", return_value=text_listeners)
        )
        bundles = stack.enter_context(
            patch("portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock)
        )
        captions = stack.enter_context(
            patch("portal.websockets.manager.ListenerConnectionManager.broadcast", new_callable=AsyncMock)
        )
        await worker.handle_translation(
            room_id=db_data["room"].id,
            segment_id=segment_id or db_data["segment"].id,
            text="Hello world",
            booth_id_str="floor",
            uuid_segment_id="1234-uuid",
            seq=1,
        )
    return bundles, captions


async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name):
    if lang_name == "French":
        await asyncio.sleep(0.2)
        return "Bonjour le monde"
    elif lang_name == "Spanish":
        return "Hola mundo"
    return "Unknown"


@pytest.mark.anyio
async def test_language_independence(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)

    # French is slow and Spanish is fast.
    with patch.object(worker, "_call_llm", new=fake_call_llm):
        mock_bundle, _ = await _run(worker, db_data)

    # We expect 4 bundles: Spanish (2: text, audio) and French (2: text, audio).
    # The source language (English) has no AI booth, so it gets none.
    assert mock_bundle.call_count == 4

    calls = mock_bundle.call_args_list
    assert {c.args[0] for c in calls} == {_ai_booth(db_data, "es"), _ai_booth(db_data, "fr")}

    # Fast things should finish first. French takes 0.2s.
    assert calls[-1].args[0] == _ai_booth(db_data, "fr")

    # Check Spanish bundle
    es_call = next(c for c in calls if c.args[0] == _ai_booth(db_data, "es"))
    assert es_call.args[5] == "Hola mundo"  # translation
    assert es_call.args[6] is None  # error is None

    # Check French audio bundle
    fr_call = [c for c in calls if c.args[0] == _ai_booth(db_data, "fr")][-1]
    assert fr_call.args[1] == b"fake_audio"
    assert fr_call.args[5] == "Bonjour le monde"
    assert fr_call.args[6] is None


@pytest.mark.anyio
async def test_pipeline_failure_degrades_gracefully(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)

    # Simulate LLM failure for Spanish, and TTS timeout for French
    async def failing_call_llm(provider, model, api_key, text, lang_name, source_lang_name):
        if lang_name == "Spanish":
            return None  # Simulate failure
        return "Bonjour le monde"

    async def slow_synthesize(room_id, text, lang_code):
        await asyncio.sleep(5)  # Simulate timeout
        return b"fake_audio"

    with patch.object(worker, "_call_llm", new=failing_call_llm):
        # Force dynamic timeout to be very short so it times out instantly
        with patch("portal.translations.worker.max", return_value=0.1):
            mock_bundle, _ = await _run(worker, db_data, synthesize=slow_synthesize)

    assert mock_bundle.call_count == 3

    calls = mock_bundle.call_args_list

    # Check Spanish bundle (pipeline_failed)
    es_call = next(c for c in calls if c.args[0] == _ai_booth(db_data, "es"))
    assert es_call.args[1] == b""  # no audio
    assert es_call.args[6] == "pipeline_failed"

    # Check French bundle (tts_timeout)
    fr_calls = [c for c in calls if c.args[0] == _ai_booth(db_data, "fr")]
    assert len(fr_calls) == 2
    fr_call = fr_calls[-1]
    assert fr_call.args[1] == b""  # no audio
    assert fr_call.args[5] == "Bonjour le monde"  # text still there
    assert fr_call.args[6] == "tts_timeout"


@pytest.mark.anyio
async def test_source_language_bypass(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)

    with patch.object(worker, "_call_llm", new=fake_call_llm):
        mock_bundle, mock_captions = await _run(worker, db_data)

    # English is the floor language: no AI booth, no bundle.
    assert not any(c.args[0].endswith("-en") for c in mock_bundle.call_args_list)
    # Its text channel gets the original text as-is.
    en_caption = next(c for c in mock_captions.call_args_list if c.args[0] == f"pipeline-test-{db_data['room'].id}-en")
    assert en_caption.args[1] == {"type": "translated_caption", "status": "final", "text": "Hello world"}


@pytest.mark.anyio
async def test_text_only_listeners_get_captions_without_tts(db_data, mock_broadcast):
    """With nobody on an AI booth, translations go to the caption channel and nothing is synthesized."""
    worker = TranslationWorker(mock_broadcast)
    synthesize = AsyncMock(return_value=b"fake_audio")

    with patch.object(worker, "_call_llm", new=fake_call_llm):
        mock_bundle, mock_captions = await _run(worker, db_data, tts_listeners=False, synthesize=synthesize)

    assert mock_bundle.call_count == 0
    synthesize.assert_not_called()
    room_id = db_data["room"].id
    texts = {c.args[0]: c.args[1]["text"] for c in mock_captions.call_args_list}
    assert texts[f"pipeline-test-{room_id}-es"] == "Hola mundo"
    assert texts[f"pipeline-test-{room_id}-fr"] == "Bonjour le monde"


@pytest.mark.anyio
async def test_language_without_tts_gets_no_ai_audio(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)
    async with get_session() as s:
        await s.execute(
            update(RoomTranslationLanguage)
            .where(RoomTranslationLanguage.room_id == db_data["room"].id, RoomTranslationLanguage.language_code == "es")
            .values(tts_enabled=False)
        )

    with patch.object(worker, "_call_llm", new=fake_call_llm):
        mock_bundle, _ = await _run(worker, db_data)

    assert {c.args[0] for c in mock_bundle.call_args_list} == {_ai_booth(db_data, "fr")}


@pytest.mark.anyio
async def test_booth_segments_never_feed_ai_booths(db_data, mock_broadcast):
    """A human booth's own translations are text only, so they can't collide with the floor's AI audio."""
    worker = TranslationWorker(mock_broadcast)
    async with get_session() as s:
        booth = DBBooth(
            event_id=db_data["event"].id,
            room_id=db_data["room"].id,
            language_code="de",
            language_name="German",
            translation_enabled=True,
            translation_provider="local",
            translation_model="test",
        )
        s.add(booth)
        await s.flush()
        s.add(BoothTranslationLanguage(booth_id=booth.id, language_code="es", language_name="Spanish"))
        segment = TranscriptSegment(room_id=db_data["room"].id, booth_id=booth.id, text="Hallo", language_code="de")
        s.add(segment)
        await s.flush()
        segment_id = segment.id

    with patch.object(worker, "_call_llm", new=fake_call_llm):
        mock_bundle, mock_captions = await _run(worker, db_data, segment_id=segment_id)

    assert mock_bundle.call_count == 0
    assert any(c.args[1].get("text") == "Hola mundo" for c in mock_captions.call_args_list)
