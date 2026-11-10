"""Glossary terms for speech recognition (#1164)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from portal.database import get_session
from portal.models import AIVocabularyEntry, DBBooth, Event, Room
from portal.transcription import vocabulary
from portal.transcription.providers.base import ProviderConfig
from portal.transcription.providers.local import LocalProvider
from portal.transcription.vocabulary import MAX_TERMS_CHARS, clean_term, load_vocabulary_terms, select_terms


@pytest.fixture
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def entry(term: str, *, id: int = 0, priority: int = 0, room_id=None, booth_id=None, language="all"):
    return AIVocabularyEntry(
        id=id,
        event_id=1,
        room_id=room_id,
        booth_id=booth_id,
        source_term=term,
        target_language=language,
        target_term=term,
        priority=priority,
    )


# ── select_terms: pure ───────────────────────────────────────────────────


def test_clean_term_is_one_plain_line():
    assert clean_term("  Media\nMTX \t") == "Media MTX"
    assert clean_term("a\x00b\x07c") == "a b c"
    assert clean_term("Smith, John") == "Smith John"
    assert clean_term(",,") == ""


def test_select_terms_puts_high_priority_first_then_booth_over_room_over_event():
    entries = [
        entry("event-low", id=1, priority=5),
        entry("room", id=2, priority=5, room_id=7),
        entry("booth", id=3, priority=5, room_id=7, booth_id=9),
        entry("event-high", id=4, priority=95),
        entry("event-mid", id=5, priority=50),
    ]

    assert select_terms(entries, room_id=7, booth_id=9) == ("event-high", "booth", "room", "event-mid", "event-low")


def test_select_terms_keeps_a_term_once_whatever_its_case_or_target_language():
    entries = [
        entry("MediaMTX", id=1, language="de"),
        entry("mediamtx", id=2, language="fr"),
        entry("MEDIAMTX", id=3),
    ]

    assert len(select_terms(entries, room_id=None, booth_id=None)) == 1


def test_select_terms_booth_spelling_beats_a_high_priority_event_spelling():
    entries = [
        entry("Mediamtx", id=1, priority=95),
        entry("MediaMTX", id=2, priority=5, room_id=7, booth_id=9),
    ]

    assert select_terms(entries, room_id=7, booth_id=9) == ("MediaMTX",)


def test_select_terms_drops_terms_that_are_empty_after_cleaning():
    entries = [entry("  ", id=1), entry(",", id=2), entry("Eventyay", id=3)]

    assert select_terms(entries, room_id=None, booth_id=None) == ("Eventyay",)


def test_select_terms_stays_within_the_length_cap_once_joined():
    entries = [entry("x" * 90 + str(i), id=i, priority=1000 - i) for i in range(50)]

    terms = select_terms(entries, room_id=None, booth_id=None)

    assert 0 < len(terms) < 50
    assert len(", ".join(terms)) <= MAX_TERMS_CHARS
    # The most important terms are the ones that survive.
    assert terms[0].endswith("0") and terms[0].startswith("x")


def test_select_terms_with_nothing_gives_no_terms():
    assert select_terms([], room_id=None, booth_id=None) == ()


# ── load_vocabulary_terms: the database ──────────────────────────────────


async def seed_event_with_glossary():
    async with get_session() as session:
        event = Event(slug="glossary-event", display_name="Glossary Event")
        other_event = Event(slug="other-event", display_name="Other Event")
        session.add_all([event, other_event])
        await session.flush()
        room = Room(event_id=event.id, display_name="Main Hall")
        other_room = Room(event_id=event.id, display_name="Side Room")
        session.add_all([room, other_room])
        await session.flush()
        fr = DBBooth(event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
        de = DBBooth(event_id=event.id, room_id=room.id, language_code="de", language_name="German")
        session.add_all([fr, de])
        await session.flush()
        session.add_all(
            [
                AIVocabularyEntry(event_id=event.id, source_term="Eventyay", target_term="Eventyay", priority=50),
                AIVocabularyEntry(event_id=event.id, room_id=room.id, source_term="MediaMTX", target_term="x"),
                AIVocabularyEntry(
                    event_id=event.id, room_id=room.id, booth_id=fr.id, source_term="Cabine", target_term="x"
                ),
                AIVocabularyEntry(
                    event_id=event.id, room_id=room.id, booth_id=de.id, source_term="Kabine", target_term="x"
                ),
                AIVocabularyEntry(event_id=event.id, room_id=other_room.id, source_term="SideRoom", target_term="x"),
                AIVocabularyEntry(event_id=other_event.id, source_term="Elsewhere", target_term="x"),
            ]
        )
        return SimpleNamespace(event=event, room=room, other_room=other_room)


@pytest.mark.anyio
async def test_a_booth_gets_event_room_and_its_own_terms_only(setup_db):
    seeded = await seed_event_with_glossary()

    terms = await load_vocabulary_terms("glossary-event", seeded.room.id, "fr")

    assert set(terms) == {"Eventyay", "MediaMTX", "Cabine"}
    # Booth entries rank above room entries, and the priority-50 event entry ranks last or first by priority only.
    assert terms.index("Cabine") < terms.index("MediaMTX")


@pytest.mark.anyio
async def test_the_scan_limit_drops_event_rows_before_a_booth_row(setup_db, monkeypatch):
    seeded = await seed_event_with_glossary()
    async with get_session() as session:
        booth_id = await session.scalar(
            select(DBBooth.id).where(DBBooth.room_id == seeded.room.id, DBBooth.language_code == "fr")
        )
        session.add_all(
            [
                AIVocabularyEntry(event_id=seeded.event.id, source_term=f"Filler{i}", target_term="x", priority=50)
                for i in range(5)
            ]
            + [
                AIVocabularyEntry(
                    event_id=seeded.event.id,
                    room_id=seeded.room.id,
                    booth_id=booth_id,
                    source_term="BoothLow",
                    target_term="x",
                    priority=5,
                )
            ]
        )
    monkeypatch.setattr(vocabulary, "MAX_ENTRIES_SCANNED", 3)

    terms = await load_vocabulary_terms("glossary-event", seeded.room.id, "fr")

    assert "BoothLow" in terms
    assert "Cabine" in terms


@pytest.mark.anyio
async def test_the_floor_has_no_booth_so_it_gets_event_and_room_terms(setup_db):
    seeded = await seed_event_with_glossary()

    terms = await load_vocabulary_terms("glossary-event", seeded.room.id, "floor")

    assert set(terms) == {"Eventyay", "MediaMTX"}


@pytest.mark.anyio
async def test_switching_the_glossary_off_gives_no_terms(setup_db):
    seeded = await seed_event_with_glossary()
    async with get_session() as session:
        room = await session.get(Room, seeded.room.id)
        room.floor_ai_vocabulary_enabled = False

    assert await load_vocabulary_terms("glossary-event", seeded.room.id, "fr") == ()


@pytest.mark.anyio
async def test_missing_room_or_a_room_from_another_event_gives_no_terms(setup_db):
    seeded = await seed_event_with_glossary()

    assert await load_vocabulary_terms("glossary-event", None, "fr") == ()
    assert await load_vocabulary_terms("glossary-event", 999999, "fr") == ()
    assert await load_vocabulary_terms("other-event", seeded.room.id, "fr") == ()
    assert await load_vocabulary_terms("no-such-event", seeded.room.id, "fr") == ()


@pytest.mark.anyio
async def test_a_failed_lookup_means_no_terms_not_a_failed_worker(monkeypatch):
    async def broken(*_args):
        raise OperationalError("select", {}, Exception("database is locked"))

    monkeypatch.setattr(vocabulary, "_lookup_terms", broken)

    assert await load_vocabulary_terms("glossary-event", 1, "fr") == ()


@pytest.mark.anyio
async def test_a_slow_lookup_times_out_into_no_terms(monkeypatch):
    async def slow(*_args):
        await asyncio.sleep(5)

    monkeypatch.setattr(vocabulary, "_lookup_terms", slow)
    monkeypatch.setattr(vocabulary, "VOCABULARY_LOOKUP_TIMEOUT", 0.01)

    assert await load_vocabulary_terms("glossary-event", 1, "fr") == ()


@pytest.mark.anyio
async def test_other_errors_are_not_hidden(monkeypatch):
    async def bug(*_args):
        raise TypeError("a bug")

    monkeypatch.setattr(vocabulary, "_lookup_terms", bug)

    with pytest.raises(TypeError):
        await load_vocabulary_terms("glossary-event", 1, "fr")


# ── local Whisper ────────────────────────────────────────────────────────


class FakeModel:
    def __init__(self):
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return [], None


def run_inference(monkeypatch, *args):
    model = FakeModel()
    monkeypatch.setattr("portal.transcription.providers.local.get_model", lambda _size: model)
    LocalProvider()._run_inference(np.zeros(16000, dtype=np.float32), "en", "base", None, *args)
    return model.calls[0]


def test_whisper_gets_the_terms_as_hotwords(monkeypatch):
    call = run_inference(monkeypatch, ("MediaMTX", "Eventyay"))

    assert call["hotwords"] == "MediaMTX, Eventyay"
    assert call["condition_on_previous_text"] is False


def test_without_terms_whisper_is_called_exactly_as_before(monkeypatch):
    without = run_inference(monkeypatch)
    empty = run_inference(monkeypatch, ())

    assert "hotwords" not in without
    assert without == empty
    assert without == {
        "beam_size": 5,
        "vad_filter": True,
        "language": "en",
        "word_timestamps": True,
        "compression_ratio_threshold": 2.4,
        "no_speech_threshold": 0.6,
        "log_prob_threshold": -1.0,
        "condition_on_previous_text": False,
    }


@pytest.mark.anyio
async def test_process_chunk_hands_the_config_terms_to_whisper(monkeypatch):
    seen = {}

    def fake_inference(self, audio, language, model, booth_state, terms=()):
        seen["terms"] = terms
        return ""

    monkeypatch.setattr(LocalProvider, "_run_inference", fake_inference)

    await LocalProvider().process_chunk(b"\x00" * 3200, "en", "base", ProviderConfig(api_key=None, terms=("MediaMTX",)))
    assert seen["terms"] == ("MediaMTX",)

    await LocalProvider().process_chunk(b"\x00" * 3200, "en", "base", ProviderConfig(api_key=None))
    assert seen["terms"] == ()
