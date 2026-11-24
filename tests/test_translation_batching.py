"""Multi-target inference and the production worker's batch dispatch (#1190)."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from portal.database import configure, dispose, get_session, init_db
from portal.models import AIVocabularyEntry, Event, Room
from portal.translations import worker as worker_module
from portal.translations.engine import EngineKind, Language, TranslationContext, TranslationEngine
from portal.translations.providers import local
from portal.translations.providers.local import LocalProvider
from portal.translations.scheduler import TranslationScheduler
from portal.translations.vocabulary import VocabularyEntryInput
from portal.translations.worker import TranslationWorker, _ResolvedTranslation

NLLB = "nllb-200-distilled-600M"
MADLAD = "madlad400-3b-mt"
EN = Language("en", "English")
TARGETS = [Language("fr", "French"), Language("de", "German")]
pytestmark = pytest.mark.anyio


class Tokenizer:
    def encode(self, text):
        return ["eng_Latn", *text.split()]

    def convert_ids_to_tokens(self, ids):
        return list(ids)

    def convert_tokens_to_ids(self, tokens):
        return list(tokens)

    def decode(self, ids, **kwargs):
        return " ".join(ids)


class Translator:
    def __init__(self):
        self.calls = []

    def translate_batch(self, sources, **options):
        self.calls.append((sources, options))
        prefixes = options.get("target_prefix", [[] for _ in sources])
        return [SimpleNamespace(hypotheses=[[*prefix, *source[1:]]]) for source, prefix in zip(sources, prefixes)]


@pytest.fixture
def model(monkeypatch):
    translator = Translator()
    monkeypatch.setattr(local, "get_model_and_tokenizer", lambda name: (translator, Tokenizer()))
    monkeypatch.setattr(local.settings, "local_translation_models", f"{NLLB},{MADLAD}")
    return translator


@pytest.mark.parametrize("name", [NLLB, MADLAD])
async def test_one_native_call_for_the_entire_grid(model, name):
    rows = await LocalProvider().translate_batch(EN, TARGETS, ["Hello", "Goodbye"], TranslationContext(), model=name)
    assert len(model.calls) == 1
    sources, options = model.calls[0]
    assert len(sources) == 4
    if name == NLLB:
        assert rows == [["Hello", "Hello"], ["Goodbye", "Goodbye"]]
        assert options["target_prefix"] == [["fra_Latn"], ["deu_Latn"]] * 2
        assert all(source[0] == "eng_Latn" for source in sources)
    else:
        assert rows == [["<2fr> Hello", "<2de> Hello"], ["<2fr> Goodbye", "<2de> Goodbye"]]
        assert "target_prefix" not in options


async def test_invalid_target_keeps_its_column_and_blank_is_not_failure(model):
    rows = await LocalProvider().translate_batch(
        EN, [TARGETS[0], Language("xx", "Unknown"), TARGETS[1]], ["Hello", " "], TranslationContext(), model=NLLB
    )
    assert rows == [["Hello", None, "Hello"], ["", None, ""]]
    assert len(model.calls) == 1
    assert len(model.calls[0][0]) == 2


async def test_glossary_retry_batches_only_cells_with_lost_placeholders(model, monkeypatch):
    original = model.translate_batch

    def drop_one(sources, **options):
        results = original(sources, **options)
        if len(model.calls) == 1:
            results[0].hypotheses = [["fra_Latn", "lost"]]
        return results

    monkeypatch.setattr(model, "translate_batch", drop_one)
    context = TranslationContext(
        vocabulary={
            "fr": [VocabularyEntryInput("booth", "fr", "cabine")],
            "de": [VocabularyEntryInput("booth", "de", "Kabine")],
        }
    )
    rows = await LocalProvider().translate_batch(EN, TARGETS, ["The booth is open"], context, model=NLLB)
    assert rows == [["The booth is open", "The Kabine is open"]]
    assert len(model.calls) == 2
    assert len(model.calls[0][0]) == 2 and len(model.calls[1][0]) == 1
    assert "[1]" not in model.calls[1][0][0]
    assert model.calls[1][1]["target_prefix"] == [["fra_Latn"]]


async def test_native_error_returns_failed_cells_and_releases_model_reference(model, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private transcript")

    monkeypatch.setattr(model, "translate_batch", fail)
    assert await LocalProvider().translate_batch(EN, TARGETS, ["Hello"], TranslationContext(), model=NLLB) == [
        [None, None]
    ]
    assert local._active_translations_per_model.get(NLLB, 0) == 0


class Engine(TranslationEngine):
    kind = EngineKind.CT2_SEQ2SEQ
    max_concurrency = 1

    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows

    async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
        self.calls.append((source, targets, texts, context))
        return (
            self.rows if self.rows is not None else [[f"{text}:{target.code}" for target in targets] for text in texts]
        )


@pytest.fixture
def worker_env(monkeypatch):
    engine = Engine()
    monkeypatch.setitem(worker_module.ENGINES, "local", engine)
    monkeypatch.setattr(worker_module, "SCHEDULERS", {})
    monkeypatch.setattr(worker_module, "_inference_timeout", lambda *args: 1)
    bundle = AsyncMock()
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", bundle)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.has_listeners", lambda *args: False)
    monkeypatch.setattr("portal.websockets.manager.listener_manager.has_listeners", lambda *args: True)
    monkeypatch.setattr("portal.websockets.manager.listener_manager.broadcast", AsyncMock())
    room = SimpleNamespace(
        id=7, floor_ai_vocabulary_enabled=False, floor_ai_interpreter_persona=None, floor_ai_interpretation_style=None
    )
    langs = [SimpleNamespace(language_code=t.code, language_name=t.name) for t in [EN, *TARGETS]]
    resolved = _ResolvedTranslation(
        SimpleNamespace(id=1, slug="event"), room, "local", NLLB, None, langs, "en", "English"
    )
    worker = TranslationWorker(MagicMock())
    delivery = AsyncMock()

    async def deliver(*args):
        await delivery(*args[:-1])
        args[-1]()

    monkeypatch.setattr(worker, "_deliver_translation", deliver)
    return worker, resolved, engine, bundle, delivery


async def test_segment_dispatches_one_call_and_preserves_target_order(worker_env):
    worker, job, engine, bundle, delivery = worker_env
    await worker._process_completed_sentence(job, 10, "Hello", "source", "uuid", 1)
    assert len(engine.calls) == 1
    assert engine.calls[0][:3] == (EN, TARGETS, ["Hello"])
    assert [call.args[0] for call in delivery.call_args_list] == ["Hello:fr", "Hello:de"]
    assert bundle.await_args.args[1] == "en"  # Source language bypassed inference.
    assert worker_module.scheduler_for("local", NLLB, 1).idle()


async def test_no_listener_no_call(worker_env, monkeypatch):
    worker, job, engine, _, _ = worker_env
    monkeypatch.setattr("portal.websockets.manager.listener_manager.has_listeners", lambda *args: False)
    await worker._process_completed_sentence(job, 10, "Hello", "source")
    assert engine.calls == []


async def test_batch_cancellation_holds_slot_until_native_work_ends(worker_env, monkeypatch):
    worker, job, engine, _, _ = worker_env
    started, release = threading.Event(), threading.Event()

    def blocking():
        started.set()
        release.wait(3)
        return [["Bonjour", "Hallo"]]

    async def translate(*args, **kwargs):
        return await local._in_thread(blocking)

    monkeypatch.setattr(engine, "translate_batch", translate)
    task = asyncio.create_task(worker._process_completed_sentence(job, 10, "Hello", "source"))
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        scheduler = worker_module.scheduler_for("local", NLLB, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert scheduler.running == 1
    finally:
        release.set()
    for _ in range(100):
        if scheduler.idle():
            break
        await asyncio.sleep(0.001)
    assert scheduler.running == 0 and scheduler.idle()


async def test_full_segment_queue_drops_each_target_without_calling_engine(worker_env, monkeypatch):
    worker, job, engine, bundle, _ = worker_env
    scheduler = TranslationScheduler(1, queue_limit=1)
    blocker = scheduler.admit(job.room.id, "source:batch")
    monkeypatch.setitem(worker_module.SCHEDULERS, ("local", NLLB), scheduler)
    try:
        await worker._process_completed_sentence(job, 10, "Hello", "source")
        assert not engine.calls
        assert [call.args[1] for call in bundle.await_args_list if call.args[-1] == "pipeline_failed"] == ["fr", "de"]
    finally:
        blocker.close()
    assert scheduler.idle()


@pytest.mark.parametrize("reason", ["queue_full", "downloading", "batch_error"])
async def test_status_notices_reach_other_languages_while_one_connection_stalls(worker_env, monkeypatch, reason):
    worker, job, engine, bundle, _ = worker_env
    entered, healthy, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    blocker = None
    if reason == "queue_full":
        scheduler = TranslationScheduler(1, queue_limit=1)
        blocker = scheduler.admit(job.room.id, "source:batch")
        monkeypatch.setitem(worker_module.SCHEDULERS, ("local", NLLB), scheduler)
    elif reason == "downloading":
        monkeypatch.setattr(worker_module, "_inference_timeout", lambda *args: None)
    else:
        engine.rows = [["Only one target"]]

    async def broadcast(*args, **kwargs):
        if args[1] == "fr":
            entered.set()
            await release.wait()
            raise RuntimeError("connection closed")
        if args[1] == "de":
            healthy.set()

    bundle.side_effect = broadcast
    task = asyncio.create_task(worker._process_completed_sentence(job, 10, "Hello", "source"))
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        await asyncio.wait_for(healthy.wait(), 0.5)
        assert not task.done()
    finally:
        release.set()
        await task
        if blocker is not None:
            blocker.close()
    assert [call.args[1] for call in bundle.await_args_list].count("de") == 1
    notices = [call for call in bundle.await_args_list if call.args[1] in {"fr", "de"}]
    assert all(
        call.args[8] == ("model_downloading" if reason == "downloading" else "pipeline_failed") for call in notices
    )
    assert worker_module.scheduler_for("local", NLLB, 1).idle()


async def test_batch_rechecks_each_listener_after_waiting_for_an_engine_slot(worker_env, monkeypatch):
    worker, job, engine, _, _ = worker_env
    scheduler = TranslationScheduler(1)
    blocker = scheduler.admit(job.room.id, "other")
    await blocker.__aenter__()
    monkeypatch.setitem(worker_module.SCHEDULERS, ("local", NLLB), scheduler)
    listeners = {"fr", "de"}
    monkeypatch.setattr(
        "portal.websockets.manager.listener_manager.has_listeners",
        lambda booth: booth.rsplit("-", 1)[-1] in listeners,
    )
    task = asyncio.create_task(worker._process_completed_sentence(job, 10, "Hello", "source"))
    try:
        while scheduler.waiting() == 0:
            await asyncio.sleep(0)
        listeners.remove("fr")
    finally:
        blocker.close()
    await asyncio.wait_for(task, 1)
    assert [target.code for target in engine.calls[0][1]] == ["de"]
    assert scheduler.idle()


@pytest.mark.parametrize("rows", [[[None, "Hallo"]], [["", "Hallo"]]])
async def test_batch_keeps_failed_or_empty_result_in_its_own_column(worker_env, rows):
    worker, job, engine, _, delivery = worker_env
    engine.rows = rows
    await worker._process_completed_sentence(job, 10, "Hello", "source")
    assert [call.args[0] for call in delivery.call_args_list] == rows[0]


async def test_invalid_engine_shape_fails_targets_without_misattributing_text(worker_env):
    worker, job, engine, bundle, delivery = worker_env
    engine.rows = [["Only one column"]]
    await worker._process_completed_sentence(job, 10, "Hello", "source")
    delivery.assert_not_called()
    assert [call.args[1] for call in bundle.await_args_list if call.args[-1] == "pipeline_failed"] == ["fr", "de"]
    assert worker_module.scheduler_for("local", NLLB, 1).idle()


async def test_one_failed_delivery_does_not_fail_a_successful_sibling(worker_env, monkeypatch):
    worker, job, _, bundle, delivery = worker_env

    async def deliver(*args):
        if args[2] == "fr":
            raise RuntimeError("save failed")
        await delivery(*args[:-1])
        args[-1]()

    monkeypatch.setattr(worker, "_deliver_translation", deliver)
    await worker._process_completed_sentence(job, 10, "Hello", "source")
    assert [call.args[2] for call in delivery.call_args_list] == ["de"]
    assert [call.args[1] for call in bundle.await_args_list if call.args[-1] == "pipeline_failed"] == ["fr"]
    assert worker_module.scheduler_for("local", NLLB, 1).idle()


async def test_a_cloud_engine_is_not_batched_for_a_model_name_matching_the_local_registry(worker_env, monkeypatch):
    worker, job, engine, _, _ = worker_env
    engine.kind = EngineKind.LLM_API
    one = AsyncMock()
    monkeypatch.setattr(worker, "_translate_and_broadcast", one)
    await worker._process_completed_sentence(job, 10, "Hello", "source")
    assert not engine.calls and one.await_count == 2


@pytest.mark.parametrize("texts,targets,expected", [([], TARGETS, []), (["Hello"], [], [[]])])
async def test_empty_grid_does_not_load_a_model(model, texts, targets, expected):
    rows = await LocalProvider().translate_batch(EN, targets, texts, TranslationContext(), model=NLLB)
    assert rows == expected and not model.calls


async def test_one_slow_glossary_lookup_cannot_multiply_the_batch_lookup_budget(worker_env, monkeypatch):
    worker, job, engine, _, _ = worker_env
    job.room.floor_ai_vocabulary_enabled = True
    entry = VocabularyEntryInput("booth", "fr", "cabine")

    @asynccontextmanager
    async def session():
        yield object()

    async def resolve(session, **kwargs):
        await asyncio.sleep(0.06)
        if kwargs["target_language"] == "fr":
            return [entry]
        return [VocabularyEntryInput("booth", "de", "Kabine")]

    monkeypatch.setattr(worker_module, "get_session", session)
    monkeypatch.setattr(worker_module, "resolve_vocabulary_entries", resolve)
    monkeypatch.setattr(worker_module, "VOCABULARY_LOOKUP_TIMEOUT", 0.1)
    await asyncio.wait_for(worker._process_completed_sentence(job, 10, "Hello", "source"), 1)
    assert len(engine.calls) == 1
    assert engine.calls[0][3].vocabulary_for(TARGETS[0]) == (entry,)
    assert engine.calls[0][3].vocabulary_for(TARGETS[1]) == ()


async def test_failed_notification_does_not_fail_siblings_or_wait_for_their_audio(worker_env, monkeypatch):
    worker, job, _, bundle, delivery = worker_env
    notified, speaking, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def deliver(*args):
        if args[2] == "fr":
            raise RuntimeError("caption delivery failed")
        args[-1]()
        speaking.set()
        await release.wait()
        await delivery(*args[:-1])

    async def broadcast(*args, **kwargs):
        if args[1] == "fr":
            notified.set()
            raise RuntimeError("notification unavailable")

    monkeypatch.setattr(worker, "_deliver_translation", deliver)
    bundle.side_effect = broadcast
    task = asyncio.create_task(worker._process_completed_sentence(job, 10, "Hello", "source"))
    try:
        await asyncio.wait_for(speaking.wait(), 1)
        await asyncio.wait_for(notified.wait(), 1)
        assert not task.done()
    finally:
        release.set()
        await task
    assert [call.args[1] for call in bundle.await_args_list if call.args[-1] == "pipeline_failed"] == ["fr"]
    assert [call.args[2] for call in delivery.await_args_list] == ["de"]
    assert worker_module.scheduler_for("local", NLLB, 1).idle()


async def test_glossary_snapshot_survives_a_later_lookup_rolling_back(worker_env, monkeypatch):
    worker, job, engine, _, _ = worker_env
    configure("sqlite+aiosqlite://")
    await init_db()
    try:
        async with get_session() as session:
            session.add(Event(id=1, slug="snapshot", display_name="Snapshot"))
            session.add(Room(id=7, event_id=1, display_name="Room"))
            session.add(
                AIVocabularyEntry(
                    event_id=1, room_id=7, source_term="booth", target_language="fr", target_term="cabine"
                )
            )
        job.room.floor_ai_vocabulary_enabled = True

        async def resolve(session, **kwargs):
            if kwargs["target_language"] == "fr":
                return list(await session.scalars(select(AIVocabularyEntry)))
            await asyncio.Event().wait()

        original = engine.translate_batch
        seen = []

        async def translate(source, targets, texts, context, **kwargs):
            seen.extend(entry.source_term for entry in context.vocabulary_for(targets[0]))
            return await original(source, targets, texts, context, **kwargs)

        monkeypatch.setattr(worker_module, "resolve_vocabulary_entries", resolve)
        monkeypatch.setattr(worker_module, "VOCABULARY_LOOKUP_TIMEOUT", 0.03)
        monkeypatch.setattr(engine, "translate_batch", translate)
        await worker._process_completed_sentence(job, 10, "Hello", "source")
        assert seen == ["booth"] and len(engine.calls) == 1
        assert engine.calls[0][3].vocabulary_for(TARGETS[0]) == (VocabularyEntryInput("booth", "fr", "cabine"),)
    finally:
        await dispose()


async def test_batch_replaces_the_tokenizers_default_with_the_requested_source(model):
    await LocalProvider().translate_batch(Language("el", "Greek"), TARGETS, ["Hello"], TranslationContext(), model=NLLB)
    sources, options = model.calls[0]
    assert all(source[0] == "ell_Grek" for source in sources)
    assert options["target_prefix"] == [["fra_Latn"], ["deu_Latn"]]


@pytest.mark.parametrize("fails", [False, True])
async def test_batch_holds_a_model_reference_until_native_inference_finishes(model, monkeypatch, fails):
    started, release = threading.Event(), threading.Event()
    original = model.translate_batch
    held = []

    def translate(*args, **kwargs):
        held.append(local._active_translations_per_model.get(NLLB, 0))
        started.set()
        release.wait(3)
        if fails:
            raise RuntimeError("native inference failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(model, "translate_batch", translate)
    task = asyncio.create_task(
        LocalProvider().translate_batch(EN, TARGETS, ["Hello"], TranslationContext(), model=NLLB)
    )
    try:
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set() and held == [1]
        assert local._active_translations_per_model[NLLB] == 1
    finally:
        release.set()
        result = await task
    assert result == ([[None, None]] if fails else [["Hello", "Hello"]])
    assert local._active_translations_per_model.get(NLLB, 0) == 0
