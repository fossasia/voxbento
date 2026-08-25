import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from portal.database import get_session
from portal.models import DBBooth, Event, Room, RoomTranslationLanguage, TranscriptSegment
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
        )
        s.add(room)
        await s.flush()

        # Add fr and es languages
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="fr", language_name="French", enabled=True))
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="es", language_name="Spanish", enabled=True))
        # source language (en) as a target to test bypass
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="en", language_name="English", enabled=True))

        segment = TranscriptSegment(room_id=room.id, text="Hello world", language_code="en")
        s.add(segment)
        await s.flush()

        return {"room": room, "event": event, "segment": segment}


@pytest.mark.anyio
async def test_language_independence(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)

    # We will mock _call_llm and synthesize.
    # We want French to be slow and Spanish to be fast.

    async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name):
        if lang_name == "French":
            await asyncio.sleep(0.2)
            return "Bonjour le monde"
        elif lang_name == "Spanish":
            return "Hola mundo"
        return "Unknown"

    async def fake_synthesize(room_id, text, lang_code):
        return b"fake_audio"

    with patch.object(worker, "_call_llm", new=fake_call_llm):
        with patch("portal.tts.worker.synthesize", new=fake_synthesize):
            with patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True):
                with patch(
                    "portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock
                ) as mock_bundle:
                    # Start the pipeline
                    await worker.handle_translation(
                        room_id=db_data["room"].id,
                        segment_id=db_data["segment"].id,
                        text="Hello world",
                        booth_id_str="floor",
                        uuid_segment_id="1234-uuid",
                        seq=1,
                    )

                # We expect 5 broadcasts: Spanish (2: text, audio), French (2: text, audio), and English (1 bypass)
                assert mock_bundle.call_count == 5

                calls = mock_bundle.call_args_list
                lang_order = [call.args[1] for call in calls]

                # Fast things should finish first. English (bypass) is instant. Spanish is instant.
                # French takes 0.2s.
                assert "fr" == lang_order[-1]  # French must be last

                # Check Spanish bundle
                es_call = next(c for c in calls if c.args[1] == "es")
                assert es_call.args[7] == "Hola mundo"  # translation
                assert es_call.args[8] is None  # error is None

                # Check French bundle
                fr_call = next(c for c in calls if c.args[1] == "fr")
                assert fr_call.args[7] == "Bonjour le monde"
                assert fr_call.args[8] is None


@pytest.mark.anyio
async def test_pipeline_failure_degrades_gracefully(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)

    # Simulate LLM failure for Spanish, and TTS timeout for French
    async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name):
        if lang_name == "Spanish":
            return None  # Simulate failure
        return "Bonjour le monde"

    async def fake_synthesize(room_id, text, lang_code):
        await asyncio.sleep(5)  # Simulate timeout
        return b"fake_audio"

    with patch.object(worker, "_call_llm", new=fake_call_llm):
        with patch("portal.tts.worker.synthesize", new=fake_synthesize):
            with patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True):
                with patch(
                    "portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock
                ) as mock_bundle:
                    # Force dynamic timeout to be very short so it times out instantly
                    with patch("portal.translations.worker.max", return_value=0.1):
                        await worker.handle_translation(
                            room_id=db_data["room"].id,
                            segment_id=db_data["segment"].id,
                            text="Hello world",
                            booth_id_str="floor",
                            uuid_segment_id="1234-uuid",
                            seq=1,
                        )

                    assert mock_bundle.call_count == 4

                    calls = mock_bundle.call_args_list

                    # Check Spanish bundle (pipeline_failed)
                    es_call = next(c for c in calls if c.args[1] == "es")
                    assert es_call.args[3] == b""  # no audio
                    assert es_call.args[8] == "pipeline_failed"

                    # Check French bundle (tts_timeout)
                    fr_calls = [c for c in calls if c.args[1] == "fr"]
                    assert len(fr_calls) == 2
                    fr_call = fr_calls[-1]
                    assert fr_call.args[3] == b""  # no audio
                    assert fr_call.args[7] == "Bonjour le monde"  # text still there
                    assert fr_call.args[8] == "tts_timeout"


@pytest.mark.anyio
async def test_source_language_bypass(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)

    with patch(
        "portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock
    ) as mock_bundle:
        await worker.handle_translation(
            room_id=db_data["room"].id,
            segment_id=db_data["segment"].id,
            text="Hello world",
            booth_id_str="floor",
            uuid_segment_id="1234-uuid",
            seq=1,
        )

        # English should be bypassed instantly with empty audio and text==text
        en_call = next(c for c in mock_bundle.call_args_list if c.args[1] == "en")
        assert en_call.args[3] == b""  # no audio
        assert en_call.args[6] == "Hello world"  # original text
        assert en_call.args[7] == "Hello world"  # translation == original text
        assert en_call.args[8] is None  # no error
