"""The shared glossary lookup that cloud caption providers use for their own limits."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from portal.database import configure, dispose, get_session, init_db
from portal.models import AIVocabularyEntry, Event, Room
from portal.transcription.providers import base
from portal.translations.vocabulary import fit_speech_terms


def _by_length(term: str) -> int:
    return len(term)


LIMITS = {"max_terms": 3, "budget": 20, "cost": _by_length}


def test_fit_speech_terms_defaults_are_deepgram_limits():
    # Deepgram's budget is 450 tokens at about three characters a token: two of these fill 400.
    assert fit_speech_terms(["x" * 600, "y" * 600, "z" * 600, "VoxBento"]) == ["x" * 600, "y" * 600, "VoxBento"]


def test_fit_speech_terms_uses_the_given_limits():
    assert fit_speech_terms(["a" * 15, "b" * 10, "c" * 5], **LIMITS) == ["a" * 15, "c" * 5]
    assert fit_speech_terms(["a", "b", "c", "d"], **LIMITS) == ["a", "b", "c"]


@pytest.mark.anyio
async def test_a_failed_slow_or_missing_lookup_gives_no_terms(monkeypatch):
    async def broken(*_):
        raise OperationalError("select", {}, Exception("database is locked"))

    monkeypatch.setattr(base, "_read_glossary_terms", broken)
    assert await base.load_glossary_terms(1, **LIMITS) == []

    async def slow(*_):
        await asyncio.sleep(1)
        return ["late"]

    monkeypatch.setattr(base, "_read_glossary_terms", slow)
    monkeypatch.setattr(base, "GLOSSARY_LOOKUP_TIMEOUT", 0.05)
    assert await base.load_glossary_terms(1, **LIMITS) == []
    assert await base.load_glossary_terms(None, **LIMITS) == []


@pytest.mark.anyio
async def test_the_lookup_reads_the_room_within_the_limits_and_respects_the_switch():
    configure("sqlite+aiosqlite://")
    await init_db()
    try:
        async with get_session() as session:
            event = Event(slug="gl-terms", display_name="Glossary")
            session.add(event)
            await session.flush()
            room = Room(event_id=event.id, display_name="Main Hall")
            session.add(room)
            await session.flush()
            for priority, term in enumerate(["Jitsi", "MediaMTX", "Eventyay", "VoxBento"]):
                session.add(
                    AIVocabularyEntry(
                        event_id=event.id,
                        room_id=room.id,
                        source_term=term,
                        target_language="all",
                        target_term=term,
                        priority=priority,
                    )
                )
            room_id = room.id

        # Highest priority first, then cut by the provider's 20-character budget.
        assert await base.load_glossary_terms(room_id, **LIMITS) == ["VoxBento", "Eventyay"]

        async with get_session() as session:
            (await session.get(Room, room_id)).floor_ai_vocabulary_enabled = False
        assert await base.load_glossary_terms(room_id, **LIMITS) == []
        assert await base.load_glossary_terms(999_999, **LIMITS) == []
    finally:
        await dispose()
