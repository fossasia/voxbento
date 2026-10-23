from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from portal.database import get_session
from portal.models import (
    AIVocabularyEntry,
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


async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
    if lang_name == "French":
        await asyncio.sleep(0.2)
        return "Bonjour le monde"
    elif lang_name == "Spanish":
        return "Hola mundo"
    return "Unknown"


@pytest.mark.anyio
async def test_language_independence(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)
    synthesized_text = {}

    async def fake_synthesize(room_id, text, lang_code):
        synthesized_text[lang_code] = text
        return b"fake_audio"

    # French is slow and Spanish is fast.
    with patch.object(worker, "_call_llm", new=fake_call_llm):
        mock_bundle, _ = await _run(worker, db_data, synthesize=fake_synthesize)

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

    # TTS consumes the exact contextual translation broadcast to caption listeners;
    # it does not perform a second, context-free translation.
    assert synthesized_text == {
        "es": "Hola mundo",
        "fr": "Bonjour le monde",
    }


@pytest.mark.anyio
async def test_vocabulary_lookup_failure_still_translates(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)
    seen_vocabulary = {}

    async with get_session() as s:
        room = await s.get(Room, db_data["room"].id)
        room.floor_ai_vocabulary_enabled = True

    async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
        seen_vocabulary[lang_name] = kwargs.get("vocabulary_entries")
        return f"translated {lang_name}"

    async def boom(*args, **kwargs):
        raise RuntimeError("glossary database is down")

    async def fake_synthesize(room_id, text, lang_code):
        return b"fake_audio"

    with patch.object(worker, "_call_llm", new=fake_call_llm):
        with patch("portal.translations.worker.resolve_vocabulary_entries", new=boom):
            with patch("portal.tts.worker.synthesize", new=fake_synthesize):
                with patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True):
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

    es_call = next(c for c in mock_bundle.call_args_list if c.args[0] == _ai_booth(db_data, "es"))
    assert es_call.args[5] == "translated Spanish"
    assert es_call.args[6] is None
    assert seen_vocabulary["Spanish"] == []


@pytest.mark.anyio
async def test_vocabulary_lookup_timeout_still_translates(db_data, mock_broadcast):
    from portal.translations import worker as worker_module

    worker = TranslationWorker(mock_broadcast)
    seen_vocabulary = {}

    async with get_session() as s:
        room = await s.get(Room, db_data["room"].id)
        room.floor_ai_vocabulary_enabled = True

    async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
        seen_vocabulary[lang_name] = kwargs.get("vocabulary_entries")
        return f"translated {lang_name}"

    async def stalls(*args, **kwargs):
        await asyncio.sleep(30)

    async def fake_synthesize(room_id, text, lang_code):
        return b"fake_audio"

    with patch.object(worker_module, "VOCABULARY_LOOKUP_TIMEOUT", 0.05):
        with patch.object(worker, "_call_llm", new=fake_call_llm):
            with patch("portal.translations.worker.resolve_vocabulary_entries", new=stalls):
                with patch("portal.tts.worker.synthesize", new=fake_synthesize):
                    with patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True):
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

    es_call = next(c for c in mock_bundle.call_args_list if c.args[0] == _ai_booth(db_data, "es"))
    assert es_call.args[5] == "translated Spanish"
    assert es_call.args[6] is None
    assert seen_vocabulary["Spanish"] == []


@pytest.mark.anyio
async def test_captions_and_tts_share_one_contextual_translation(db_data, mock_broadcast):
    """The glossary-aware translation is produced once and reused for TTS.

    Verifies there is no second, context-free translation on the TTS path.
    """
    worker = TranslationWorker(mock_broadcast)
    llm_calls = []
    synthesized = {}

    async with get_session() as s:
        room = await s.get(Room, db_data["room"].id)
        room.floor_ai_vocabulary_enabled = True
        room.floor_ai_interpreter_persona = "A conference interpreter."
        room.floor_ai_interpretation_style = "Formal register."
        s.add(
            AIVocabularyEntry(
                event_id=db_data["event"].id,
                room_id=room.id,
                source_term="Voxbento",
                target_language="all",
                target_term="Voxbento",
                priority=100,
            )
        )

    async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
        llm_calls.append(
            {
                "lang": lang_name,
                "text": text,
                "persona": kwargs.get("persona"),
                "style": kwargs.get("style"),
                "vocabulary": [e.source_term for e in kwargs.get("vocabulary_entries") or []],
            }
        )
        return f"Willkommen bei Voxbento ({lang_name})"

    async def fake_synthesize(room_id, text, lang_code):
        synthesized[lang_code] = text
        return b"fake_audio"

    with patch.object(worker, "_call_llm", new=fake_call_llm):
        with patch("portal.tts.worker.synthesize", new=fake_synthesize):
            with patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True):
                with patch(
                    "portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock
                ) as mock_bundle:
                    await worker.handle_translation(
                        room_id=db_data["room"].id,
                        segment_id=db_data["segment"].id,
                        text="Welcome to Voxbento",
                        booth_id_str="floor",
                        uuid_segment_id="1234-uuid",
                        seq=1,
                    )

    # One translation per target language, not two (captions + TTS would be two).
    assert sorted(call["lang"] for call in llm_calls) == ["French", "Spanish"]

    # The configuration reached that single call.
    for call in llm_calls:
        assert call["persona"] == "A conference interpreter."
        assert call["style"] == "Formal register."
        assert call["vocabulary"] == ["Voxbento"]

    # What listeners read and what listeners hear are the same string.
    for lang_code, lang_name in (("es", "Spanish"), ("fr", "French")):
        caption = next(
            c for c in mock_bundle.call_args_list if c.args[0] == _ai_booth(db_data, lang_code) and c.args[5]
        )
        assert caption.args[5] == f"Willkommen bei Voxbento ({lang_name})"
        assert synthesized[lang_code] == caption.args[5]


@pytest.mark.anyio
async def test_glossary_lookup_does_not_hold_an_inference_slot(db_data, mock_broadcast):
    """A slow lookup must not occupy one of the two per-language inference slots."""
    from portal.translations import worker as worker_module

    worker = TranslationWorker(mock_broadcast)
    held_during_lookup = []

    async with get_session() as s:
        room = await s.get(Room, db_data["room"].id)
        room.floor_ai_vocabulary_enabled = True

    async def slow_lookup(*args, **kwargs):
        # Record how many inference slots are taken while the lookup is in flight.
        sem = worker_module.LANGUAGE_SEMAPHORES.get("es")
        held_during_lookup.append(0 if sem is None else 2 - sem._value)
        await asyncio.sleep(0.2)
        return []

    async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
        return f"translated {lang_name}"

    async def fake_synthesize(room_id, text, lang_code):
        return b"fake_audio"

    with patch.object(worker, "_call_llm", new=fake_call_llm):
        with patch("portal.translations.worker.resolve_vocabulary_entries", new=slow_lookup):
            with patch("portal.tts.worker.synthesize", new=fake_synthesize):
                with patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True):
                    with patch(
                        "portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock
                    ):
                        await worker.handle_translation(
                            room_id=db_data["room"].id,
                            segment_id=db_data["segment"].id,
                            text="Hello world",
                            booth_id_str="floor",
                            uuid_segment_id="1234-uuid",
                            seq=1,
                        )

    assert held_during_lookup, "the lookup never ran"
    assert max(held_during_lookup) == 0, (
        f"glossary lookup held {max(held_during_lookup)} inference slot(s); it must resolve before acquiring one"
    )


@pytest.mark.anyio
async def test_pipeline_failure_degrades_gracefully(db_data, mock_broadcast):
    worker = TranslationWorker(mock_broadcast)

    # Simulate LLM failure for Spanish, and TTS timeout for French
    async def failing_call_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
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
    # Its text channel gets the original text as-is, tagged with the segment it belongs to.
    en_caption = next(c for c in mock_captions.call_args_list if c.args[0] == f"pipeline-test-{db_data['room'].id}-en")
    assert en_caption.args[1] == {
        "type": "translated_caption",
        "status": "final",
        "text": "Hello world",
        "segment_id": "1234-uuid",
        "seq": 1,
    }


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


@pytest.mark.anyio
async def test_queue_counter_restored_when_glossary_lookup_cancelled(db_data, mock_broadcast):
    """Cancelling a task during glossary lookup must not leak the per-language queue slot."""
    import asyncio

    from portal.translations import worker as worker_module

    worker = TranslationWorker(mock_broadcast)
    lang = "zz-cancel"
    queue_key = f"floor:{lang}"
    worker_module.LANGUAGE_QUEUES[queue_key] = 0

    async with get_session() as s:
        room = await s.get(Room, db_data["room"].id)
        room.floor_ai_vocabulary_enabled = True
        event = await s.get(Event, db_data["event"].id)

    started = asyncio.Event()

    async def hanging_lookup(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
        return "done"

    async def fake_synthesize(room_id, text, lang_code):
        return b"audio"

    with patch("portal.translations.worker.resolve_vocabulary_entries", new=hanging_lookup):
        task = asyncio.create_task(
            worker._translate_and_broadcast(
                event, room, "local", "nllb", "k", lang, "Lang", "en", "English",
                db_data["segment"].id, "Hello", "floor", "uuid", 1,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert worker_module.LANGUAGE_QUEUES[queue_key] == 0

    # A subsequent translation for the same language is still accepted.
    with patch.object(worker, "_call_llm", new=fake_call_llm):
        with patch("portal.tts.worker.synthesize", new=fake_synthesize):
            with patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True):
                with patch(
                    "portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock
                ) as bundle:
                    room.floor_ai_vocabulary_enabled = False
                    await worker._translate_and_broadcast(
                        event, room, "local", "nllb", "k", lang, "Lang", "en", "English",
                        db_data["segment"].id, "Hello", "floor", "uuid2", 2,
                        tts_booth_id=_ai_booth(db_data, lang),
                    )
    assert bundle.call_count >= 1
    assert worker_module.LANGUAGE_QUEUES[queue_key] == 0
