"""The TranslationEngine interface (#1184): its contract, the provider adapter, NLLB and the worker's dispatch."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import pickle
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from portal.translations import worker as worker_module
from portal.translations.constants import OPENAI_COMPATIBLE_ENDPOINTS, TRANSLATION_MODELS, TranslationProviderEnum
from portal.translations.engine import (
    EngineKind,
    Language,
    TranslationContext,
    TranslationEngine,
    TranslationEngineError,
    translate_each,
)
from portal.translations.providers.base import ProviderEngine, TranslationProvider
from portal.translations.providers.local import LocalProvider
from portal.translations.worker import ENGINES, TranslationWorker

pytestmark = pytest.mark.anyio

ENGLISH = Language("en", "English")
FRENCH = Language("fr", "French")
GERMAN = Language("de", "German")


class FakeEngine(TranslationEngine):
    """Records each call and answers ``"<text>→<target code>"``, or whatever ``rows`` is set to."""

    kind = EngineKind.MT_API

    def __init__(self, rows=None):
        self.rows = rows
        self.calls = []

    async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
        self.calls.append(
            SimpleNamespace(
                source=source, targets=list(targets), texts=list(texts), context=context, model=model, api_key=api_key
            )
        )
        if self.rows is not None:
            return self.rows
        return [[f"{text}→{target.code}" for target in targets] for text in texts]


def glossary_entry(source_term: str, target_language: str, target_term: str) -> SimpleNamespace:
    return SimpleNamespace(
        source_term=source_term,
        target_language=target_language,
        target_term=target_term,
        case_sensitive=False,
        match_type="phrase",
        priority=0,
    )


# --- The interface -------------------------------------------------------------------------


def test_an_engine_must_implement_translate_batch():
    class Incomplete(TranslationEngine):
        kind = EngineKind.MT_API

    with pytest.raises(TypeError, match="translate_batch"):
        Incomplete()


def test_an_engine_must_say_what_kind_it_is():
    class Kindless(TranslationEngine):
        async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
            return []

    with pytest.raises(TypeError, match="kind"):
        Kindless()


async def test_translate_batch_returns_a_row_per_text_and_a_column_per_target():
    rows = await FakeEngine().translate_batch(
        ENGLISH, [FRENCH, GERMAN], ["Hello", "Bye"], TranslationContext(), model="m"
    )

    assert rows == [["Hello→fr", "Hello→de"], ["Bye→fr", "Bye→de"]]


async def test_translate_text_is_translate_batch_for_one_text_and_one_target():
    engine = FakeEngine()
    context = TranslationContext(persona="A formal interpreter")

    result = await engine.translate_text(ENGLISH, FRENCH, "Hello", context, model="m", api_key="key")

    assert result == "Hello→fr"
    (call,) = engine.calls
    assert (call.source, call.targets, call.texts) == (ENGLISH, [FRENCH], ["Hello"])
    assert (call.context, call.model, call.api_key) == (context, "m", "key")


@pytest.mark.parametrize("failed", [None, ""])
async def test_translate_text_passes_a_failed_or_empty_translation_through(failed):
    result = await FakeEngine(rows=[[failed]]).translate_text(ENGLISH, FRENCH, "Hello", TranslationContext(), model="m")

    assert result == failed


@pytest.mark.parametrize("rows", [[], [[]], [["a", "b"]], [["a"], ["b"]]])
async def test_translate_text_rejects_rows_of_the_wrong_shape(rows):
    with pytest.raises(TranslationEngineError):
        await FakeEngine(rows=rows).translate_text(ENGLISH, FRENCH, "Hello", TranslationContext(), model="m")


def test_context_gives_each_target_its_own_glossary():
    french = [glossary_entry("booth", "fr", "cabine")]
    context = TranslationContext(vocabulary={"fr": french})

    assert context.vocabulary_for(FRENCH) == tuple(french)
    assert context.vocabulary_for(GERMAN) == ()
    assert TranslationContext().vocabulary_for(FRENCH) == ()


def test_a_context_cannot_be_changed_once_built():
    french = [glossary_entry("booth", "fr", "cabine")]
    given = {"fr": french}
    context = TranslationContext(vocabulary=given)

    with pytest.raises(TypeError):
        context.vocabulary["de"] = []
    given["de"] = []
    # The lists are copied too: changing the caller's list later must not reach the context.
    french.append(glossary_entry("mic", "fr", "micro"))
    with pytest.raises(AttributeError):
        context.vocabulary_for(FRENCH).append(glossary_entry("mic", "fr", "micro"))

    assert dict(context.vocabulary) == {"fr": (french[0],)}
    assert context == TranslationContext(vocabulary={"fr": french[:1]})


def test_a_context_can_be_pickled_copied_and_turned_into_a_dict():
    """A remote backend may serialise the context, so the read-only view must not block it."""
    entry = glossary_entry("booth", "fr", "cabine")
    context = TranslationContext(persona="Formal", style="Concise", vocabulary={"fr": [entry]})

    assert pickle.loads(pickle.dumps(context)) == context
    copied = copy.deepcopy(context)
    assert copied == context
    with pytest.raises(TypeError):
        copied.vocabulary["de"] = ()
    assert dataclasses.asdict(context) == {"persona": "Formal", "style": "Concise", "vocabulary": {"fr": (entry,)}}


# --- translate_each ------------------------------------------------------------------------


async def test_translate_each_keeps_the_translations_that_worked():
    async def translate_one(text, target):
        if target is GERMAN:
            raise RuntimeError("German is down")
        return f"{text}→{target.code}"

    rows = await translate_each(translate_one, [FRENCH, GERMAN], ["Hello", "Bye"])

    assert rows == [["Hello→fr", None], ["Bye→fr", None]]


async def test_translate_each_logs_which_language_and_text_failed_but_not_the_text(caplog):
    async def translate_one(text, target):
        if (text, target) == ("Bye", GERMAN):
            raise RuntimeError("German is down")
        return "ok"

    with caplog.at_level("ERROR", logger="portal.translations.engine"):
        await translate_each(translate_one, [FRENCH, GERMAN], ["Hello", "Bye"])

    (record,) = caplog.records
    assert "'de'" in record.getMessage() and "text 2 of 2" in record.getMessage()
    assert "German is down" in record.getMessage()
    assert "Bye" not in record.getMessage()


async def test_translate_each_raises_when_nothing_was_translated():
    async def translate_one(text, target):
        raise RuntimeError(f"{target.code} is down")

    with pytest.raises(RuntimeError, match="fr is down"):
        await translate_each(translate_one, [FRENCH, GERMAN], ["Hello"])


async def test_translate_each_logs_every_error_when_all_calls_fail(caplog):
    async def translate_one(text, target):
        raise RuntimeError(f"{target.code} is down")

    with caplog.at_level("ERROR", logger="portal.translations.engine"):
        with pytest.raises(RuntimeError, match="fr is down"):
            await translate_each(translate_one, [FRENCH, GERMAN], ["Hello"])

    assert [record.getMessage() for record in caplog.records if "de is down" in record.getMessage()], (
        "the error that is not re-raised is still logged"
    )


async def test_translate_each_returns_none_without_raising_when_only_some_calls_errored():
    """A backend's own None (an unsupported language) is an answer, not an error."""

    async def translate_one(text, target):
        if target.code == "de":
            raise RuntimeError("down")
        return None

    assert await translate_each(translate_one, [FRENCH, GERMAN], ["Hello"]) == [[None, None]]


async def test_translate_each_passes_a_cancellation_on_instead_of_reporting_a_failed_translation(caplog):
    async def translate_one(text, target):
        if target.code == "de":
            raise asyncio.CancelledError
        return f"{text}-{target.code}"

    with caplog.at_level("ERROR", logger="portal.translations.engine"):
        with pytest.raises(asyncio.CancelledError):
            await translate_each(translate_one, [FRENCH, GERMAN], ["Hello"])

    assert not caplog.records


async def test_translate_each_without_targets_or_texts_calls_nothing():
    translate_one = AsyncMock()

    assert await translate_each(translate_one, [], ["Hello", "Bye"]) == [[], []]
    assert await translate_each(translate_one, [FRENCH], []) == []
    translate_one.assert_not_called()


# --- ProviderEngine ------------------------------------------------------------------------


async def test_provider_engine_passes_each_target_its_languages_and_glossary():
    provider = MagicMock(spec=TranslationProvider)
    provider.translate = AsyncMock(side_effect=lambda **kwargs: f"{kwargs['text']}→{kwargs['target_lang_code']}")
    french = [glossary_entry("booth", "fr", "cabine")]
    engine = ProviderEngine("groq", provider, EngineKind.OPENAI_COMPATIBLE_LLM)

    rows = await engine.translate_batch(
        ENGLISH,
        [FRENCH, GERMAN],
        ["Hello"],
        TranslationContext(persona="Formal", style="Concise", vocabulary={"fr": french}),
        model="openai/gpt-oss-20b",
        api_key="gsk-test",
    )

    assert rows == [["Hello→fr", "Hello→de"]]
    shared = {
        "provider_name": "groq",
        "text": "Hello",
        "source_lang_name": "English",
        "model": "openai/gpt-oss-20b",
        "api_key": "gsk-test",
        "persona": "Formal",
        "style": "Concise",
    }
    assert [call.kwargs for call in provider.translate.await_args_list] == [
        {**shared, "target_lang_name": "French", "target_lang_code": "fr", "vocabulary_entries": tuple(french)},
        {**shared, "target_lang_name": "German", "target_lang_code": "de", "vocabulary_entries": ()},
    ]


async def test_provider_engine_raises_what_a_single_failed_call_raised():
    provider = MagicMock(spec=TranslationProvider)
    provider.translate = AsyncMock(side_effect=ValueError("bad response"))
    engine = ProviderEngine("gemini", provider, EngineKind.LLM_API)

    with pytest.raises(ValueError, match="bad response"):
        await engine.translate_text(ENGLISH, FRENCH, "Hello", TranslationContext(), model="gemini-2.5-flash")


# --- NLLB ----------------------------------------------------------------------------------


def test_the_local_provider_is_a_seq2seq_engine():
    provider = LocalProvider()

    assert isinstance(provider, TranslationEngine)
    assert provider.kind is EngineKind.CT2_SEQ2SEQ


async def test_nllb_translates_each_target_with_its_own_language_token(monkeypatch):
    calls = []

    def fake_inference(self, text, source_token, target_token, model_size):
        calls.append((text, source_token, target_token, model_size))
        return f"{text}→{target_token}"

    monkeypatch.setattr(LocalProvider, "_run_inference", fake_inference)

    rows = await LocalProvider().translate_batch(
        ENGLISH, [FRENCH, GERMAN], ["Hello"], TranslationContext(), model="nllb-200-distilled-600M"
    )

    assert rows == [["Hello→fra_Latn", "Hello→deu_Latn"]]
    assert sorted(calls) == [
        ("Hello", "eng_Latn", "deu_Latn", "nllb-200-distilled-600M"),
        ("Hello", "eng_Latn", "fra_Latn", "nllb-200-distilled-600M"),
    ]


async def test_nllb_marks_a_language_it_lacks_without_losing_the_others(monkeypatch):
    monkeypatch.setattr(LocalProvider, "_run_inference", lambda self, text, source, target, model: "Bonjour")

    rows = await LocalProvider().translate_batch(
        ENGLISH,
        [Language("xx", "UnknownLanguage"), FRENCH],
        ["Hello"],
        TranslationContext(),
        model="nllb-200-distilled-600M",
    )

    assert rows == [[None, "Bonjour"]]


async def test_nllb_applies_each_targets_glossary(monkeypatch):
    seen = []

    def fake_inference(self, text, source_token, target_token, model_size):
        seen.append((target_token, text))
        return {"The [1] is open": "La [1] est ouverte", "The booth is open": "Die Kabine ist offen"}[text]

    monkeypatch.setattr(LocalProvider, "_run_inference", fake_inference)

    rows = await LocalProvider().translate_batch(
        ENGLISH,
        [FRENCH, GERMAN],
        ["The booth is open"],
        TranslationContext(vocabulary={"fr": [glossary_entry("booth", "fr", "cabine")]}),
        model="nllb-200-distilled-600M",
    )

    assert rows == [["La cabine est ouverte", "Die Kabine ist offen"]]
    assert sorted(seen) == [("deu_Latn", "The booth is open"), ("fra_Latn", "The [1] is open")]


# --- The worker's dispatch -----------------------------------------------------------------


def test_every_selectable_provider_has_an_engine():
    # The self-hosted provider's models come from the operator's settings, so it has no fixed model list.
    assert set(ENGINES) == {provider.value for provider in TranslationProviderEnum}
    assert set(TRANSLATION_MODELS) <= set(ENGINES)
    assert all(isinstance(engine, TranslationEngine) for engine in ENGINES.values())


def test_engines_keep_the_provider_name_that_picks_their_endpoint():
    for name in [*OPENAI_COMPATIBLE_ENDPOINTS, TranslationProviderEnum.SELF_HOSTED.value]:
        engine = ENGINES[name]
        assert isinstance(engine, ProviderEngine)
        assert (engine.provider_name, engine.kind) == (name, EngineKind.OPENAI_COMPATIBLE_LLM)
    assert ENGINES["gemini"].kind is ENGINES["anthropic"].kind is EngineKind.LLM_API
    assert isinstance(ENGINES["local"], LocalProvider)


async def test_the_worker_translates_through_the_engine_interface(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setitem(worker_module.ENGINES, "fake", engine)
    entries = [glossary_entry("booth", "fr", "cabine")]

    result = await TranslationWorker(MagicMock())._call_llm(
        "fake",
        "fake-model",
        "key",
        "Hello",
        "French",
        "English",
        target_lang_code="fr",
        source_language_code="en",
        persona="Formal",
        style="Concise",
        vocabulary_entries=entries,
    )

    assert result == "Hello→fr"
    (call,) = engine.calls
    assert (call.source, call.targets, call.texts) == (ENGLISH, [FRENCH], ["Hello"])
    assert (call.model, call.api_key) == ("fake-model", "key")
    assert call.context == TranslationContext(persona="Formal", style="Concise", vocabulary={"fr": entries})


async def test_the_worker_accepts_a_source_language_known_only_by_name(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setitem(worker_module.ENGINES, "fake", engine)

    await TranslationWorker(MagicMock())._call_llm("fake", "fake-model", None, "Hello", "French", "English")

    (call,) = engine.calls
    assert (call.source, call.targets) == (Language("", "English"), [Language("", "French")])
    assert call.context == TranslationContext()


async def test_the_worker_returns_none_for_a_provider_without_an_engine():
    result = await TranslationWorker(MagicMock())._call_llm("no-such-provider", "m", None, "Hello", "French")

    assert result is None


@pytest.mark.parametrize(
    ("answer", "closing_bundle"),
    [
        # The engine could not translate: listeners are told so.
        (None, call(7, "fr", "floor", b"", "uuid-1", 3, "Hello", "", "pipeline_failed")),
        # The engine ran and had nothing to say: the segment is closed without an error.
        ("", call(7, "fr", "floor", b"", "uuid-1", 3, "Hello", "", None, audio_pending=False)),
        ("  \n", call(7, "fr", "floor", b"", "uuid-1", 3, "Hello", "", None, audio_pending=False)),
    ],
    ids=["failed", "empty", "blank"],
)
async def test_the_worker_tells_a_failed_translation_from_an_empty_one(monkeypatch, answer, closing_bundle):
    """Neither is stored, captioned or spoken, but only the failure is reported to listeners as one."""
    monkeypatch.setitem(worker_module.ENGINES, "fake", FakeEngine(rows=[[answer]]))
    bundle = AsyncMock()
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", bundle)
    captions = AsyncMock()
    monkeypatch.setattr("portal.websockets.manager.listener_manager.broadcast", captions)
    synthesize = AsyncMock()
    monkeypatch.setattr("portal.tts.worker.synthesize", synthesize)
    stored = MagicMock(side_effect=AssertionError("nothing may be stored"))
    monkeypatch.setattr(worker_module, "get_session", stored)
    room = SimpleNamespace(
        id=7, floor_ai_vocabulary_enabled=False, floor_ai_interpreter_persona=None, floor_ai_interpretation_style=None
    )

    await TranslationWorker(MagicMock())._translate_and_broadcast(
        SimpleNamespace(id=1), room, "fake", "fake-model", None, "fr", "French", "en", "English",
        42, "Hello", "floor", "uuid-1", 3, "event-7-fr",
    )

    assert bundle.await_args_list == [closing_bundle]
    captions.assert_not_awaited()
    synthesize.assert_not_awaited()
    stored.assert_not_called()
    assert worker_module.scheduler_for("fake").admitted(7, "floor:fr") == 0


async def test_the_worker_returns_none_when_the_engine_could_not_translate(monkeypatch):
    monkeypatch.setitem(worker_module.ENGINES, "fake", FakeEngine(rows=[[None]]))

    result = await TranslationWorker(MagicMock())._call_llm("fake", "m", None, "Hello", "French", "English")

    assert result is None
